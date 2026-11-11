"""Scheduled AMC source checks, run outside web requests (see check_amc_portfolio_sources).

One listing request per AMC per run (a few for AMCs whose listing is paged by month), circuit
breakers respected, access restrictions never retried. Optional steps verify one real file per
AMC, look for a moved disclosure page in the AMC's own sitemap, and ingest funds people use.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from django.core.cache import cache

from . import source_health
from .adapters import AMC_SOURCES, NO_AUTOMATED_DISCOVERY, registry_sources, source_for_amc
from .holdings import PORTFOLIO_LOOKBACK_MONTHS, _download_cached, _load_page_links, _recent_portfolio_months, _archive_members, _workbook_sheets
from .listings import sitemap_candidates
from .portfolio_store import _portfolio_dir, _read_status, portfolio_key, stored_links
from .providers import ProviderError, fetch_amfi_disclosure_directory, fetch_amfi_latest

logger = logging.getLogger(__name__)


def amfi_directory() -> dict[str, str]:
    try:
        return fetch_amfi_disclosure_directory()
    except ProviderError as exc:
        logger.warning('AMFI disclosure directory unavailable: %s', exc)
        return {}


def known_amcs() -> dict[str, dict[str, Any] | None]:
    """Every AMC with a known source: dedicated, verified registry, then AMFI's directory."""
    amcs: dict[str, dict[str, Any] | None] = {}
    directory = amfi_directory()
    for name in sorted({*AMC_SOURCES, *registry_sources(), *directory}):
        try:
            amcs[name] = source_for_amc(name)
        except ProviderError:
            amcs[name] = None
    return amcs


def _clear_page_cache(amc_key: str) -> None:
    for suffix in ('', ':error'):
        try:
            cache.delete(f'fund-analyzer:portfolio-page:{amc_key}{suffix}')
        except Exception:
            logger.exception('Page cache could not be cleared for %s', amc_key)


def check_source(name: str, source: dict[str, Any] | None, *, force: bool = False, verify_file: bool = False, recover: bool = False) -> dict[str, Any]:
    """Check one AMC's listing now and return what was found. Records health as a side effect."""
    started = time.perf_counter()
    if not source:
        return {'amc': name, 'state': source_health.STATE_DISCOVERY_FAILED, 'detail': 'AMFI lists no disclosure page for this AMC', 'months': []}
    key = source['key']
    if force:
        source_health.reset_circuit(key)
    _clear_page_cache(key)
    result: dict[str, Any] = {'amc': name, 'key': key, 'method': source.get('parser'), 'url': source.get('url'), 'months': []}
    try:
        links = _load_page_links(key, source['url'], source['parser'])
        months = sorted({link['month'] for link in links if link.get('month') in _recent_portfolio_months()}, reverse=True)
        result.update({'months': months, 'files_listed': len(links)})
        if verify_file and links:
            result['file_check'] = _verify_newest_file(key, links)
    except ProviderError as exc:
        result.update({'error': str(exc)[:300], 'category': getattr(exc, 'category', None)})
    health = source_health.read_health(key)
    listed = amfi_directory().get(name)
    if listed and health.get('amfi_listed_url') != listed:
        # The AMC's page as AMFI lists it today, kept beside the page actually used.
        source_health._write_health(key, {**health, 'amfi_listed_url': listed})
        health = source_health.read_health(key)
    if recover and source.get('parser') not in NO_AUTOMATED_DISCOVERY and health.get('state') == source_health.STATE_DISCOVERY_FAILED:
        result['recovery'] = recover_moved_page(name, source)
        health = source_health.read_health(key)
    result.update({
        'state': health.get('state') or source_health.NOT_CHECKED,
        'last_success_at': health.get('last_success_at'),
        'latest_month': health.get('latest_month'),
        'circuit_open_until': health.get('circuit_open_until'),
        'evidence': (health.get('last_failure') or {}).get('evidence') if health.get('state') != source_health.WORKING else None,
        'seconds': round(time.perf_counter() - started, 2),
    })
    return result


def _verify_newest_file(key: str, links: list[dict[str, Any]]) -> dict[str, Any]:
    """Download the newest listed file once and confirm it is a readable workbook or archive."""
    link = max(links, key=lambda item: item['month'])
    try:
        payload = _download_cached(key, link['month'], link['url'])
        members = _archive_members(payload)
        if members is None:
            sheets, _ = _workbook_sheets(payload, link['url'].lower())
            return {'month': link['month'], 'ok': True, 'kind': 'workbook', 'sheets': len(sheets), 'bytes': len(payload)}
        return {'month': link['month'], 'ok': True, 'kind': 'archive', 'workbooks': len(members), 'bytes': len(payload)}
    except Exception as exc:  # reported, not raised: one AMC must not stop the run
        source_health.record_failure(key, stage=source_health.STAGE_WORKBOOK, category=getattr(exc, 'category', None) or source_health.INVALID_WORKBOOK, detail=str(exc))
        return {'month': link['month'], 'ok': False, 'error': str(exc)[:200]}


def recover_moved_page(name: str, source: dict[str, Any]) -> dict[str, Any]:
    """Look for the AMC's moved disclosure page in its own sitemap, and validate each candidate.

    A candidate is accepted only if the same rules find monthly files on it, on the AMC's own domain.
    The validated page is recorded in the AMC's health; source_for_amc uses it in place of AMFI's
    out-of-date link until a developer confirms it in amc_sources.json.
    """
    key = source['key']
    try:
        candidates = sitemap_candidates(source)
    except ProviderError as exc:
        return {'validated_url': None, 'tried': [], 'error': str(exc)[:200]}
    tried = []
    for url in candidates:
        _clear_page_cache(key)
        source_health.reset_circuit(key)
        try:
            links = _load_page_links(key, url, source['parser'])
        except ProviderError as exc:
            tried.append({'url': url, 'ok': False, 'error': str(exc)[:120]})
            continue
        months = sorted({link['month'] for link in links}, reverse=True)
        tried.append({'url': url, 'ok': True, 'months': months[:3]})
        health = source_health.read_health(key)
        source_health._write_health(key, {**health, 'recovered_url': url, 'recovered_at': datetime.now(timezone.utc).isoformat(), 'recovered_from': source.get('url')})
        logger.warning('AMC %s disclosure page moved: validated %s (was %s). Confirm it in amc_sources.json.', name, url, source.get('url'))
        return {'validated_url': url, 'tried': tried}
    return {'validated_url': None, 'tried': tried}


def selected_scheme_codes(records: list[dict[str, Any]]) -> list[str]:
    """Funds people have analysed: any portfolio with a status or a stored month. Newest checks first."""
    by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        by_key.setdefault(portfolio_key(record), record)
    chosen = []
    for key, record in by_key.items():
        if not _portfolio_dir(record).exists():
            continue
        status = _read_status(record)
        if status or stored_links(record):
            chosen.append((status.get('last_checked_at') or '', str(record.get('scheme_code'))))
    return [code for _, code in sorted(chosen, reverse=True)]


def run_checks(*, amc: str | None = None, force: bool = False, verify_files: bool = False, recover: bool = False, ingest: bool = False, max_seconds: float | None = None) -> dict[str, Any]:
    started = time.monotonic()
    amcs = known_amcs()
    if amc:
        amcs = {name: source for name, source in amcs.items() if name == amc}
    results = []
    for name, source in amcs.items():
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            break
        results.append(check_source(name, source, force=force, verify_file=verify_files, recover=recover))
    summary: dict[str, Any] = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'amcs_checked': len(results),
        'by_state': _count(result['state'] for result in results),
        'sources': results,
        'official_lookback_months': PORTFOLIO_LOOKBACK_MONTHS,
    }
    if ingest:
        from .ingestion import run_ingestion

        records = fetch_amfi_latest()
        codes = selected_scheme_codes(records)
        if amc:
            codes = [code for code in codes if next((r for r in records if str(r.get('scheme_code')) == code), {}).get('amc') == amc]
        remaining = None if max_seconds is None else max(0.0, max_seconds - (time.monotonic() - started))
        summary['ingestion'] = run_ingestion(scheme_codes=codes, max_seconds=remaining) if codes else {'due': 0}
    return summary


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def display_state(row: dict[str, Any]) -> str:
    """The labels staff read: Working, Temporarily Unavailable, Access Blocked, Source Changed, Parser Failed, Unsupported."""
    from .adapters import normalised_page

    state = row.get('state')
    if row.get('discovery_method') in NO_AUTOMATED_DISCOVERY and state != source_health.STATE_ACCESS_BLOCKED:
        return 'UNSUPPORTED'
    if state == source_health.STATE_DISCOVERY_FAILED:
        moved = (row.get('last_failure') or {}).get('category') == source_health.SOURCE_URL_MOVED
        listed = row.get('amfi_listed_url')
        differs = bool(listed and row.get('source_url') and normalised_page(listed) != normalised_page(row['source_url']))
        return 'SOURCE_CHANGED' if moved or differs else 'UNSUPPORTED'
    if state == source_health.PARSER_ISSUE:
        return 'PARSER_FAILED'
    return state or source_health.NOT_CHECKED


def health_report() -> dict[str, Any]:
    """Every AMC's recorded health, worst first, for staff."""
    order = {source_health.STATE_ACCESS_BLOCKED: 0, source_health.STATE_DISCOVERY_FAILED: 1, source_health.PARSER_ISSUE: 2, source_health.TEMPORARILY_UNAVAILABLE: 3, source_health.WORKING: 4}
    rows = []
    for health in source_health.all_health():
        failure = health.get('last_failure') or {}
        rows.append({
            'amc': health.get('amc') or health.get('key'),
            'key': health.get('key'),
            'state': health.get('state'),
            'discovery_method': health.get('discovery_method'),
            'source_url': health.get('recovered_url') or health.get('source_url'),
            'latest_month': health.get('latest_month'),
            'last_checked_at': health.get('last_checked_at'),
            'last_success_at': health.get('last_success_at'),
            'circuit_open_until': health.get('circuit_open_until'),
            'last_failure': {key: failure.get(key) for key in ('at', 'stage', 'category', 'detail')} if failure else None,
            'evidence': {key: (failure.get('evidence') or {}).get(key) for key in ('http_status', 'content_type', 'final_url', 'seconds', 'cdn', 'retry_after', 'blocked_page')} if failure else None,
            'counters': health.get('counters') or {},
            'recovered_url': health.get('recovered_url'),
            'amfi_listed_url': health.get('amfi_listed_url'),
        })
    rows.sort(key=lambda row: (order.get(row['state'], 5), str(row['amc'])))
    for row in rows:
        row['display_state'] = display_state(row)
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'by_state': _count(row['state'] for row in rows), 'sources': rows}
