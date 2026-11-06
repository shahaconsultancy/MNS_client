from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from time import perf_counter
from typing import Any

from .analytics import correlation, metrics
from .holdings import analyze_holdings_overlap, fetch_fund_holdings, scheme_status
from .providers import (
    ProviderError,
    plan_option_conflict,
    fetch_amfi_aum,
    fetch_amfi_history,
    fetch_amfi_latest,
    fetch_amfi_ter,
    fetch_amfi_ter_snapshot,
    fetch_mfapi_data,
    parse_amfi_latest_nav_excel,
    parse_amfi_history_text_data,
    parse_amfi_text_data,
    parse_mfapi,
)

logger = logging.getLogger(__name__)


def _iso_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    for pattern in ('%d-%b-%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(text[:11], pattern).date().isoformat()
        except ValueError:
            continue
    return text


def _is_direct_growth(record: dict[str, Any]) -> bool:
    plan = (record.get('plan') or '').lower()
    option = (record.get('option') or '').lower()
    name = (record.get('scheme_name') or '').lower()
    return ('direct' in plan or 'direct' in name) and ('growth' in option or 'growth' in name)


def _pick_latest(records: list[dict[str, Any]], scheme_code: str) -> dict[str, Any]:
    matches = [record for record in records if str(record.get('scheme_code')) == str(scheme_code)]
    if not matches:
        raise ProviderError(f'Scheme {scheme_code} was not found in AMFI NAV data')
    direct_growth = [record for record in matches if _is_direct_growth(record)]
    return (direct_growth or matches)[0]


def _base_detail(record: dict[str, Any], *, source: str, as_of: str | None) -> dict[str, Any]:
    state = scheme_status(record)
    return {
        'scheme_status': state['status'], 'last_nav_date': state['last_nav_date'],
        'source': source, 'asOfDate': _iso_date(as_of), 'scheme_code': str(record.get('scheme_code')),
        'scheme_name': record.get('scheme_name'), 'amc': record.get('amc'), 'category': record.get('category'),
        'scheme_type': record.get('scheme_type'), 'plan': record.get('plan'), 'option': record.get('option'),
        'isin': record.get('isin'), 'nav': record.get('nav'), 'nav_date': record.get('date'),
    }


def _today_window() -> tuple[date, date]:
    end = date.today()
    return end - timedelta(days=365 * 5 + 7), end


def fetch_fund_data(scheme_code: str, *, risk_free_rate: float = 0.065, include_history: bool = True, include_metadata: bool = True, latest_records: list[dict[str, Any]] | None = None, ter_snapshot: dict[str, tuple[float | None, str | None]] | None = None, aum_data: tuple[float | None, str | None] | None = None) -> dict[str, Any]:
    """Fetch a fund live, preferring AMFI and using MFAPI only on AMFI failure."""
    try:
        latest_records = latest_records if latest_records is not None else fetch_amfi_latest()
        latest = _pick_latest(latest_records, scheme_code)
        history = []
        if include_history:
            start, end = _today_window()
            history = fetch_amfi_history(scheme_code, start, end)
        ter, ter_as_of = (None, None)
        aum, aum_as_of = (None, None)
        if include_metadata:
            ter, ter_as_of = fetch_amfi_ter(scheme_code, latest_records, ter_snapshot)
            aum, aum_as_of = aum_data if aum_data is not None else fetch_amfi_aum(latest.get('scheme_name') or '', latest.get('amc') or '')
        result = _base_detail(latest, source='AMFI', as_of=latest.get('date'))
        result.update({'ter': ter, 'ter_as_of': ter_as_of, 'aum': aum, 'aum_unit': 'lakhs' if aum is not None else None, 'aum_as_of': aum_as_of, 'history': history})
        result['metrics'] = metrics(history, risk_free_rate) if history else None
        return result
    except (ProviderError, ValueError, TypeError, KeyError) as exc:
        logger.warning('AMFI fallback for scheme %s: %s', scheme_code, exc)
        try:
            payload = fetch_mfapi_data(scheme_code)
            meta, history = parse_mfapi(payload, scheme_code)
            # One scheme code is one plan and one option. If AMFI and MFAPI disagree about which,
            # MFAPI's NAV history is another fund's, and showing it would mix the two.
            amfi_record = next((row for row in (latest_records or []) if str(row.get('scheme_code')) == str(scheme_code)), None)
            conflict = plan_option_conflict(amfi_record, meta) if amfi_record else None
            if conflict:
                raise ProviderError(f'MFAPI data for scheme {scheme_code} is a different plan or option: {conflict}')
            latest = history[-1]
            result = _base_detail({**meta, **latest}, source='MFAPI (backup)', as_of=latest.get('date'))
            result.update({'ter': None, 'ter_as_of': None, 'aum': None, 'aum_unit': None, 'aum_as_of': None, 'history': history})
            result['metrics'] = metrics(history, risk_free_rate) if history else None
            return result
        except (ProviderError, ValueError, TypeError, KeyError) as backup_exc:
            logger.error('AMFI and MFAPI failed for scheme %s: %s', scheme_code, backup_exc)
            raise ProviderError(f'Unable to load scheme {scheme_code} from AMFI or MFAPI') from backup_exc


def search_funds(query: str, limit: int = 25) -> dict[str, Any]:
    records = fetch_amfi_latest()
    # Every word must appear, in any order. AMFI names sometimes carry repeated spaces, so spacing is ignored.
    words = query.lower().split()
    filtered = [
        record for record in records
        if all(word in ' '.join(' '.join(str(record.get(key) or '') for key in ('scheme_name', 'scheme_code', 'isin', 'amc', 'category')).lower().split()) for word in words)
    ]
    filtered = [record for record in filtered if _is_direct_growth(record)] or filtered
    # The typed phrase itself, then names starting with it, ahead of names that only contain every word.
    phrase = ' '.join(words)
    def rank(record):
        name = ' '.join(str(record.get('scheme_name') or '').lower().split())
        return (not name.startswith(phrase), phrase not in name)
    filtered.sort(key=rank)
    as_of = max((record.get('date') for record in filtered if record.get('date')), default=None)
    return {'source': 'AMFI', 'asOfDate': _iso_date(as_of), 'results': filtered[:limit]}


def compare_funds(scheme_codes: list[str], risk_free_rate: float = 0.065) -> dict[str, Any]:
    started = perf_counter()
    try:
        latest_records = fetch_amfi_latest()
    except ProviderError as exc:
        logger.warning('AMFI latest snapshot unavailable for comparison: %s', exc)
        # An empty snapshot skips another latest-NAV retry per selected fund and
        # lets each fund go directly to the MFAPI fallback.
        latest_records = []

    # History and analytics are the critical path for Compare. AUM still loads only
    # when the client opens Fundamentals.
    worker_count = min(4, len(scheme_codes))
    with ThreadPoolExecutor(max_workers=max(1, worker_count)) as pool:
        funds = list(pool.map(
            lambda code: fetch_fund_data(code, risk_free_rate=risk_free_rate, include_metadata=False, latest_records=latest_records),
            scheme_codes,
        ))
    # TER shares a host with NAV history, and that host throttles bursts, so load it
    # afterwards. A TER failure must not fail the comparison.
    try:
        ter_snapshot = fetch_amfi_ter_snapshot(scheme_codes, latest_records)
    except Exception:
        logger.exception('Fund Analyzer TER lookup failed')
        ter_snapshot = {}
    for fund in funds:
        fund['ter'], fund['ter_as_of'] = ter_snapshot.get(fund['scheme_code'], (None, None))
    logger.info('Fund Analyzer comparison loaded %d funds in %.2fs', len(funds), perf_counter() - started)
    matrix = {fund['scheme_code']: {} for fund in funds}
    for left in funds:
        for right in funds:
            if left['scheme_code'] == right['scheme_code']:
                matrix[left['scheme_code']][right['scheme_code']] = 1.0
            else:
                matrix[left['scheme_code']][right['scheme_code']] = correlation(left['history'], right['history'])
    source = 'MFAPI (backup)' if any(fund['source'] == 'MFAPI (backup)' for fund in funds) else 'AMFI'
    as_of = max((fund.get('asOfDate') for fund in funds if fund.get('asOfDate')), default=None)
    valid = [fund for fund in funds if fund.get('metrics')]
    top_cagr = max(valid, key=lambda fund: fund['metrics'].get('r3y') if fund['metrics'].get('r3y') is not None else float('-inf'), default=None)
    best_sharpe = max(valid, key=lambda fund: fund['metrics'].get('sharpe') if fund['metrics'].get('sharpe') is not None else float('-inf'), default=None)
    lowest_vol = min(valid, key=lambda fund: fund['metrics'].get('volatility') if fund['metrics'].get('volatility') is not None else float('inf'), default=None)
    return {'source': source, 'asOfDate': as_of, 'funds': funds, 'correlation': matrix, 'summary': {
        'riskFreeRate': risk_free_rate,
        'top3yCagr': top_cagr['scheme_code'] if top_cagr else None,
        'bestSharpe': best_sharpe['scheme_code'] if best_sharpe else None,
        'lowestVolatility': lowest_vol['scheme_code'] if lowest_vol else None,
    }}


def fetch_fundamentals(scheme_codes: list[str]) -> dict[str, Any]:
    """Load slow, optional TER and AUM data for the Fundamentals tab."""
    latest_records = fetch_amfi_latest()

    def load_aum_values() -> dict[str, tuple[float | None, str | None]]:
        result = {}
        for code in scheme_codes:
            try:
                record = _pick_latest(latest_records, code)
                result[code] = fetch_amfi_aum(record.get('scheme_name') or '', record.get('amc') or '')
            except ProviderError:
                result[code] = (None, None)
        return result

    record_by_code = {str(record.get('scheme_code')): record for record in latest_records}

    with ThreadPoolExecutor(max_workers=2 + min(4, len(scheme_codes))) as pool:
        ter_future = pool.submit(fetch_amfi_ter_snapshot, scheme_codes, latest_records)
        aum_future = pool.submit(load_aum_values)
        holdings_futures = {code: pool.submit(fetch_fund_holdings, str(code), record_by_code.get(str(code))) for code in scheme_codes}
        try:
            ter_snapshot = ter_future.result()
        except Exception:
            logger.exception('Fund Analyzer TER lookup failed')
            ter_snapshot = {}
        try:
            aum_by_code = aum_future.result()
        except Exception:
            logger.exception('Fund Analyzer AUM lookup failed')
            aum_by_code = {}
        holdings_by_code = {}
        for code, future in holdings_futures.items():
            try:
                holdings_by_code[code] = future.result()
            except Exception:
                logger.exception('Fund Analyzer holdings lookup failed for %s', code)
                holdings_by_code[code] = {'holdings': [], 'holdings_as_of': None, 'benchmark': None, 'allocation': None, 'sectors': [], 'riskometer': None, 'asset_class': None}

    funds = []
    for code in scheme_codes:
        record = _pick_latest(latest_records, code)
        aum, aum_as_of = aum_by_code.get(code, (None, None))
        funds.append({
            **_base_detail(record, source='AMFI', as_of=record.get('date')),
            'ter': ter_snapshot.get(str(code), (None, None))[0],
            'ter_as_of': ter_snapshot.get(str(code), (None, None))[1],
            'aum': aum,
            'aum_unit': 'lakhs' if aum is not None else None,
            'aum_as_of': aum_as_of,
            **holdings_by_code[code],
        })
    return {'source': 'AMFI', 'funds': funds}


def compare_holdings_overlap(scheme_codes: list[str], portfolio_month: str | None = None, progress=None) -> dict[str, Any]:
    return analyze_holdings_overlap(scheme_codes, portfolio_month, progress)
