"""Optional bulk retrieval of official portfolios, to fill the JSON cache ahead of time."""
from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any

from django.utils import timezone

from .adapters import (
    DISCLOSURE_NOT_PUBLISHED,
    DOWNLOAD_BLOCKED,
    DOWNLOAD_FAILED,
    HOLDINGS_AVAILABLE,
    SOURCE_NOT_SUPPORTED,
    classify_error,
    source_for_amc,
)
from .holdings import _holdings_from_link, _link_error_status, _monthly_files_for_fund, _overlap_debug_record, _recent_portfolio_months
from .portfolio_store import discovery_due, file_scheme_name, is_eligible_equity, portfolio_key, record_status, stored_links
from .providers import ProviderError, fetch_amfi_latest

logger = logging.getLogger(__name__)

MAX_FILE_ATTEMPTS = 6
MAX_DOWNLOAD_RETRIES = 2


def eligible_portfolios(records: list[dict[str, Any]], *, amc: str | None = None, scheme_codes: list[str] | None = None) -> list[dict[str, Any]]:
    """One record per underlying portfolio, so Direct and Regular are fetched once."""
    wanted_codes = {str(code) for code in scheme_codes or []}
    portfolios: dict[str, dict[str, Any]] = {}
    for record in records:
        if wanted_codes:
            if str(record.get('scheme_code')) not in wanted_codes:
                continue
        elif not is_eligible_equity(record):
            continue
        if amc and record.get('amc') != amc:
            continue
        portfolios.setdefault(portfolio_key(record), record)
    return sorted(portfolios.values(), key=lambda record: (str(record.get('amc')), str(record.get('scheme_name'))))


def _with_retries(load, retries: int, pause: float):
    """Retry a temporary failure a limited number of times. A block is never retried."""
    attempt = 0
    while True:
        try:
            return load()
        except Exception as exc:
            if classify_error(exc) != DOWNLOAD_FAILED or attempt >= retries:
                raise
            attempt += 1
            time.sleep(pause * (2 ** attempt))


def ingest_portfolio(record: dict[str, Any], source: dict[str, Any], *, months_to_keep: int, retries: int, pause: float, downloaded: dict, workbooks: dict) -> tuple[str, str, str | None]:
    """Fetch this portfolio's newest months that are not yet stored. Returns status, detail, month."""
    code = str(record.get('scheme_code'))
    name = file_scheme_name(record)
    recent_months = _recent_portfolio_months()
    stored = {link['month'] for link in stored_links(record)}
    try:
        links = _monthly_files_for_fund(source, name)
    except ProviderError as exc:
        status = classify_error(exc)
        if stored:
            return HOLDINGS_AVAILABLE, f'Source unavailable, using stored data: {exc}', max(stored)
        # A page that lists no usable files is a page this system cannot read yet.
        return (SOURCE_NOT_SUPPORTED if status == DISCLOSURE_NOT_PUBLISHED else status), str(exc), None
    links = sorted((link for link in links if link.get('month') in recent_months), key=lambda link: link['month'], reverse=True)
    if not links:
        if stored:
            return HOLDINGS_AVAILABLE, 'No new files listed; using stored data', max(stored)
        return SOURCE_NOT_SUPPORTED, 'No monthly portfolio file was found for this scheme', None

    debug = _overlap_debug_record(code, record)
    first_error: tuple[str, str] | None = None
    attempts = 0
    collected = stored & recent_months
    for link in links:
        month = link['month']
        if month in collected:
            continue
        if len(collected) >= months_to_keep or attempts >= MAX_FILE_ATTEMPTS:
            break
        attempts += 1
        try:
            _with_retries(
                lambda current=link: _holdings_from_link(code, current['month'], current, source, name, debug, downloaded, workbooks, record),
                retries, pause,
            )
            collected.add(month)
        except Exception as exc:
            first_error = first_error or (_link_error_status(link, exc), f'{month}: {exc}')
            logger.info('Portfolio %s failed for %s: %s', month, record.get('scheme_name'), exc)
    if collected:
        return HOLDINGS_AVAILABLE, '', max(collected)
    if first_error:
        return first_error[0], first_error[1], None
    return DISCLOSURE_NOT_PUBLISHED, 'No file could be tried', None


def run_ingestion(*, amc: str | None = None, scheme_codes: list[str] | None = None, force: bool = False, months_to_keep: int = 2, retries: int = MAX_DOWNLOAD_RETRIES, pause: float = 1.0, max_seconds: float | None = None) -> dict[str, Any]:
    """Check due portfolios, AMC by AMC, until done or out of time."""
    started = time.monotonic()
    records = fetch_amfi_latest()
    portfolios = eligible_portfolios(records, amc=amc, scheme_codes=scheme_codes)
    if not force:
        portfolios = [record for record in portfolios if discovery_due(record)]
    results: Counter = Counter()
    by_amc: dict[str, Counter] = {}
    blocked_pages: dict[str, tuple[str, str]] = {}
    downloaded: dict[str, bytes] = {}
    workbooks: dict[str, Any] = {}
    current_amc = None
    processed = 0
    timed_out = False
    for record in portfolios:
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            timed_out = True
            break
        amc_name = record.get('amc') or ''
        if amc_name != current_amc:
            # A workbook is shared within an AMC. Free it before moving to the next.
            current_amc = amc_name
            downloaded.clear()
            workbooks.clear()
        source = source_for_amc(amc_name)
        files_before = len(downloaded)
        if source is None:
            outcome = (SOURCE_NOT_SUPPORTED, 'AMFI lists no portfolio page for this AMC', None)
        elif amc_name in blocked_pages:
            # The AMC's page already refused this run. Do not ask it again for every scheme.
            outcome = (*blocked_pages[amc_name], None)
        else:
            outcome = ingest_portfolio(record, source, months_to_keep=months_to_keep, retries=retries, pause=pause, downloaded=downloaded, workbooks=workbooks)
            if outcome[0] == DOWNLOAD_BLOCKED and not outcome[1].startswith('20') and source.get('parser') != 'hdfc_xlsx_by_scheme':
                blocked_pages[amc_name] = (outcome[0], outcome[1])
        record_status(record, outcome[0], outcome[1], outcome[2])
        results[outcome[0]] += 1
        by_amc.setdefault(amc_name, Counter())[outcome[0]] += 1
        processed += 1
        if pause and len(downloaded) > files_before:
            time.sleep(pause)
    needs_manual = sorted(
        amc_name for amc_name, counts in by_amc.items()
        if not counts.get(HOLDINGS_AVAILABLE) and (counts.get(DOWNLOAD_BLOCKED) or counts.get(SOURCE_NOT_SUPPORTED))
    )
    return {
        'finished_at': timezone.now().isoformat(),
        'seconds': round(time.monotonic() - started, 1),
        'due': len(portfolios),
        'processed': processed,
        'timed_out': timed_out,
        'results': dict(results),
        'by_amc': {name: dict(counts) for name, counts in sorted(by_amc.items())},
        'amcs_needing_manual_action': needs_manual,
    }
