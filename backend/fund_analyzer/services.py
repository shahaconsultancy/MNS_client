from __future__ import annotations

import logging
import re
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
from .scheme_registry import get_registry, registry_status

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


def _metrics_or_reason(history: list[dict[str, Any]], risk_free_rate: float, scheme_code: str) -> tuple[dict[str, Any] | None, str | None]:
    """Metrics for a usable NAV history, or None and the reason.

    A zero or negative NAV is not a price (segregated portfolios publish 0), and returns over it
    are undefined, so such a history gets no metrics instead of failing the whole comparison.
    Every other history goes to metrics() unchanged.
    """
    if not history:
        return None, None
    if any(not isinstance(row.get('nav'), (int, float)) or row['nav'] <= 0 for row in history):
        logger.warning('Scheme %s has zero or negative NAVs in its history; returns and risk are not calculated', scheme_code)
        return None, 'Returns and risk are not available: this scheme reports a zero NAV (for example a segregated portfolio).'
    return metrics(history, risk_free_rate), None


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
        result['metrics'], result['metrics_unavailable'] = _metrics_or_reason(history, risk_free_rate, scheme_code)
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
            result['metrics'], result['metrics_unavailable'] = _metrics_or_reason(history, risk_free_rate, scheme_code)
            return result
        except (ProviderError, ValueError, TypeError, KeyError) as backup_exc:
            logger.error('AMFI and MFAPI failed for scheme %s: %s', scheme_code, backup_exc)
            raise ProviderError(f'Unable to load scheme {scheme_code} from AMFI or MFAPI') from backup_exc


SEARCH_RESULT_LIMIT = 50
# AMFI spells IDCW out in full on some rows and older rows still say Dividend.
_IDCW_LONG = re.compile(r'income\s+distribution\s+cum\s+capital\s+withdrawal')
_QUERY_ALTERNATIVES = {'idcw': ('idcw', 'dividend', 'payout', 'reinvest'), 'dividend': ('idcw', 'dividend'), 'div': ('idcw', 'div')}
# Words people add that AMFI does not always print ("Direct Plan" vs "- Direct -").
_FILLER_WORDS = {'plan', 'option', 'scheme', 'opt'}


def _search_norm(value: Any) -> str:
    # The words are kept beside 'idcw', so either spelling, in any word order, still matches.
    text = _IDCW_LONG.sub(lambda match: f'{match.group(0)} idcw ', str(value or '').lower().replace('&', ' and '))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


def _plan_rank(record: dict[str, Any]) -> int:
    text = f"{record.get('plan') or ''} {record.get('scheme_name') or ''}".lower()
    return 0 if re.search(r'\bdirect\b', text) else 1 if re.search(r'\bregular\b', text) else 2


def _option_rank(record: dict[str, Any]) -> int:
    text = _search_norm(f"{record.get('option') or ''} {record.get('scheme_name') or ''}")
    return 1 if re.search(r'\b(idcw|dividend|payout|reinvest\w*)\b', text) else 0 if re.search(r'\bgrowth\b', text) else 2


MAX_SEARCH_PAGE = 100


def _search_result(record: dict[str, Any], display: str) -> dict[str, Any]:
    state = scheme_status(record)
    return {**record, 'display_name': display, 'scheme_status': state['status'], 'last_nav_date': state['last_nav_date'], 'nav_date': record.get('date')}


def search_funds(query: str, limit: int = SEARCH_RESULT_LIMIT, offset: int = 0) -> dict[str, Any]:
    """Every AMFI scheme whose name, plan, option, code, ISIN, AMC or category holds every typed word.

    The whole registry is matched and ranked first; offset and limit only page through that full,
    ranked list, and total counts every match. Each plan and option is its own scheme code, so no
    Regular, IDCW, inactive or unpriced scheme is dropped; Direct Growth only sorts first.
    """
    registry = get_registry()
    limit = max(1, min(int(limit), MAX_SEARCH_PAGE))
    offset = max(0, int(offset))
    meta = {'source': 'AMFI', 'registry': registry_status(), 'offset': offset, 'limit': limit}
    # An exact scheme code or ISIN means that scheme.
    key = ''.join(query.split()).upper()
    exact = ([registry.by_code[key]] if key in registry.by_code else []) or registry.by_isin.get(key, [])
    if exact:
        page = [_search_result(record, record['display_name']) for record in exact[offset:offset + limit]]
        return {**meta, 'asOfDate': _iso_date(exact[0].get('date')), 'total': len(exact), 'results': page}

    phrase = _search_norm(query)
    words = phrase.split()
    required = [word for word in words if word not in _FILLER_WORDS] or words
    if not required:
        return {**meta, 'asOfDate': None, 'total': 0, 'results': []}
    alternatives = [_QUERY_ALTERNATIVES.get(word, (word,)) for word in required]
    matched = []
    for record, (display, haystack, compact, display_norm, name_norm) in zip(registry.records, registry.search_rows):
        # Every word must appear, in any order. Spacing is ignored too, so 'mid cap' finds 'Midcap' and back.
        if all(any(alt in haystack or alt in compact for alt in options) for options in alternatives):
            matched.append((record, display, display_norm, name_norm))

    # The typed phrase itself, then names starting with it, then names containing it, then names
    # that only hold every word. Live schemes before wound-up ones; Direct Growth first within a fund.
    def rank(item):
        record, _, display_norm, name_norm = item
        return (
            phrase not in (display_norm, name_norm), not display_norm.startswith(phrase), phrase not in display_norm,
            scheme_status(record)['status'] != 'ACTIVE', record.get('nav') is None, len(name_norm.split()), name_norm,
            _plan_rank(record), _option_rank(record), str(record.get('scheme_code')),
        )
    matched.sort(key=rank)
    page = [_search_result(record, display) for record, display, _, _ in matched[offset:offset + limit]]
    as_of = max((record.get('date') for record in page if record.get('date')), default=None)
    return {**meta, 'asOfDate': _iso_date(as_of), 'total': len(matched), 'results': page}


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
