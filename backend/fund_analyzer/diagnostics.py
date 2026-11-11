"""Staff diagnostics: why a scheme's holdings can or cannot be retrieved, stage by stage.

For staff and developers only (management command and an admin-only endpoint). Clients never
see this: their responses carry a plain availability message instead.
"""
from __future__ import annotations

from typing import Any

from . import charlie_alpha, source_health
from .adapters import AMC_SOURCES, classify_error, registry_sources, source_for_amc
from .holdings import (
    _asset_class,
    _holdings_from_link,
    _discover_files,
    _overlap_debug_record,
    _recent_portfolio_months,
)
from .portfolio_store import _amc_source, _read_status, discovery_due, file_scheme_name, stored_links
from .providers import ProviderError, fetch_amfi_latest

STAGE_FOR_STATUS = {
    'DOWNLOAD_BLOCKED': source_health.STAGE_DOWNLOAD,
    'DOWNLOAD_FAILED': source_health.STAGE_DOWNLOAD,
    'INVALID_WORKBOOK': source_health.STAGE_WORKBOOK,
    'SCHEME_NOT_FOUND': source_health.STAGE_MAPPING,
    'AMBIGUOUS_MAPPING': source_health.STAGE_MAPPING,
    'PARSING_FAILED': source_health.STAGE_PARSE,
    'DISCLOSURE_NOT_PUBLISHED': source_health.STAGE_DOWNLOAD,
    'SOURCE_NOT_SUPPORTED': source_health.STAGE_LISTING,
}
CATEGORY_FOR_STATUS = {
    'DOWNLOAD_BLOCKED': source_health.ACCESS_BLOCKED,
    'DOWNLOAD_FAILED': source_health.DOWNLOAD_FAILED,
    'INVALID_WORKBOOK': source_health.INVALID_WORKBOOK,
    'SCHEME_NOT_FOUND': source_health.SCHEME_MAPPING_FAILED,
    'AMBIGUOUS_MAPPING': source_health.SCHEME_AMBIGUOUS,
    'PARSING_FAILED': source_health.HOLDINGS_PARSE_FAILED,
    'DISCLOSURE_NOT_PUBLISHED': source_health.PORTFOLIO_MONTH_UNAVAILABLE,
    'SOURCE_NOT_SUPPORTED': source_health.DISCOVERY_FAILED,
}


def _origin(amc: str) -> str:
    if amc in AMC_SOURCES:
        return 'dedicated adapter'
    if amc in registry_sources():
        return 'verified registry (amc_sources.json)'
    return 'AMFI disclosure directory'


def diagnose_scheme(code: str, *, live: bool = False, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything known about one scheme's holdings retrieval. live=True runs a fresh retrieval."""
    code = str(code)
    records = records if records is not None else fetch_amfi_latest()
    record = next((row for row in records if str(row.get('scheme_code')) == code), None)
    if not record:
        return {'scheme_code': code, 'final_reason': 'Not in the current AMFI scheme list', 'available': False}
    amc = record.get('amc') or ''
    health_key, _ = _amc_source(record)
    try:
        source = source_for_amc(amc)
    except ProviderError:
        source = None
    if source:
        health_key = source['key']
    # A live attempt first, so the health and status shown are the ones it just produced.
    discovery, months = _live_attempt(code, record, source) if live else ({}, [])
    health = source_health.read_health(health_key)
    status = _read_status(record)
    stored = stored_links(record)
    report: dict[str, Any] = {
        'scheme_code': code,
        'scheme_name': record.get('scheme_name'),
        'file_scheme_name': file_scheme_name(record),
        'amc': amc,
        'asset_class': _asset_class(record),
        'source': {
            'origin': _origin(amc) if source else 'none',
            'url': (source or {}).get('url'),
            'discovery_method': (source or {}).get('parser'),
            'access': (source or {}).get('access') or 'public',
            'notes': (source or {}).get('notes'),
        },
        'amc_health': {
            'state': health.get('state') or source_health.NOT_CHECKED,
            'last_checked_at': health.get('last_checked_at'),
            'last_success_at': health.get('last_success_at'),
            'latest_month': health.get('latest_month'),
            'circuit_open_until': health.get('circuit_open_until'),
            'last_failure': health.get('last_failure'),
        },
        'scheme_status': {
            'status': status.get('status') or 'NOT_CHECKED',
            'scope': status.get('scope'),
            'category': status.get('category') or CATEGORY_FOR_STATUS.get(status.get('status') or ''),
            'detail': status.get('detail'),
            'last_checked_at': status.get('last_checked_at'),
            'next_check_at': status.get('next_check_at'),
            'retry_due_now': discovery_due(record),
        },
        'official_lookback_months': sorted(_recent_portfolio_months(), reverse=True),
        'stored_months': [{'month': link['month'], 'third_party': link.get('third_party', False)} for link in stored],
        'backup': {
            'enabled': charlie_alpha.enabled(),
            'note': 'Charlie Alpha is used only when enabled and only after official sources fail, within its own lookback.',
        },
        'discovery': discovery,
        'last_recorded_sources': status.get('sources'),
        'months': months,
    }
    available = bool(stored) or any(month.get('result') == 'ok' for month in report['months'])
    report['available'] = available
    report['final_reason'] = _final_reason(report, available)
    return report


def _live_attempt(code: str, record: dict[str, Any], source: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """List the source's files and read each recent month, newest first, until one verifies."""
    if not source:
        return {}, [{'stage': source_health.STAGE_LISTING, 'result': 'error', 'category': source_health.DISCOVERY_FAILED, 'detail': 'No source is known for this AMC'}]
    name = file_scheme_name(record)
    trail: dict[str, Any] = {}
    verified = {link['month'] for link in stored_links(record) if not link.get('third_party')}
    try:
        links = _discover_files(source, name, amc_name=record.get('amc') or '', cached_months=verified, trail=trail)
    except ProviderError as exc:
        return trail, [{'stage': source_health.STAGE_LISTING, 'result': 'error', 'category': getattr(exc, 'category', None) or source_health.category_for_exception(exc, stage=source_health.STAGE_LISTING), 'detail': str(exc)[:300], 'evidence': getattr(exc, 'evidence', None)}]
    recent = _recent_portfolio_months()
    links = sorted((link for link in links if link.get('month') in recent), key=lambda link: link['month'], reverse=True)
    if not links:
        return trail, [{'stage': source_health.STAGE_LISTING, 'result': 'error', 'category': source_health.PORTFOLIO_MONTH_UNAVAILABLE, 'detail': 'The source lists no file for this scheme in the lookback'}]
    attempts: list[dict[str, Any]] = []
    downloaded: dict[str, bytes] = {}
    workbooks: dict[str, Any] = {}
    for link in links:
        debug = _overlap_debug_record(code, record)
        entry = {'month': link['month'], 'file': link.get('text') or link['url'].rsplit('/', 1)[-1], 'route': link.get('via') or 'amc_page', 'download_host': link['url'].split('/')[2] if '://' in link['url'] else ''}
        try:
            result = _holdings_from_link(code, link['month'], link, source, name, debug, downloaded, workbooks, record)
        except Exception as exc:  # each failure is reported, never raised to the caller
            status = classify_error(exc)
            category = getattr(exc, 'category', None) or CATEGORY_FOR_STATUS.get(status, source_health.HOLDINGS_PARSE_FAILED)
            attempts.append({**entry, 'stage': getattr(exc, 'stage', None) or STAGE_FOR_STATUS.get(status, source_health.STAGE_PARSE), 'result': 'error', 'status': status, 'category': category, 'detail': str(exc)[:300], 'evidence': getattr(exc, 'evidence', None)})
            continue
        attempts.append({
            **entry, 'stage': 'validated', 'result': 'ok', 'holdings': len(result['holdings']), 'reporting_date': result['portfolio_date'],
            'matched': debug.get('archive_member') or debug.get('matched_sheet_name'), 'riskometer': result.get('risk_level'),
        })
        break
    return trail, attempts


def _final_reason(report: dict[str, Any], available: bool) -> str:
    if available:
        months = [month['month'] for month in report['stored_months']] + [month['month'] for month in report['months'] if month.get('result') == 'ok']
        return f"Holdings available (latest verified month {max(months)})"
    failure = next((month for month in reversed(report['months']) if month.get('result') == 'error'), None)
    if failure:
        return f"{failure['category']} at {failure['stage']}: {failure['detail']}"
    health = report['amc_health']
    if health['state'] not in (source_health.WORKING, source_health.NOT_CHECKED) and health.get('last_failure'):
        last = health['last_failure']
        return f"AMC source {health['state']}: {last.get('category')} at {last.get('stage')} — {last.get('detail')}"
    scheme = report['scheme_status']
    if scheme['status'] not in ('NOT_CHECKED', 'HOLDINGS_AVAILABLE'):
        return f"{scheme['category'] or scheme['status']}: {scheme['detail']}"
    return 'Not checked yet; run with --live to try the source now'
