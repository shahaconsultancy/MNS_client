from __future__ import annotations

import contextvars
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from threading import Lock
from time import perf_counter
from typing import Any

from django.core.cache import cache

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


def _is_price(row: dict[str, Any]) -> bool:
    return isinstance(row.get('nav'), (int, float)) and not isinstance(row.get('nav'), bool) and row['nav'] > 0


def _priced_history(history: list[dict[str, Any]], scheme_code: str) -> list[dict[str, Any]]:
    """The NAV history without rows that are not prices.

    A zero or negative NAV is not a price. A scheme whose latest NAV is one (a segregated portfolio
    publishes 0) keeps its history as it is, and gets no metrics. A live scheme with the odd zero row
    (MFAPI has a few, years old) loses just those rows, so its returns use real prices only.
    """
    if not history or not _is_price(history[-1]):
        return history
    priced = [row for row in history if _is_price(row)]
    if len(priced) != len(history):
        logger.info('Scheme %s: ignored %d NAV row(s) that are zero or negative', scheme_code, len(history) - len(priced))
    return priced


def _metrics_or_reason(history: list[dict[str, Any]], risk_free_rate: float, scheme_code: str) -> tuple[dict[str, Any] | None, str | None]:
    """Metrics for a usable NAV history, or None and the reason.

    Pass the history through _priced_history first. If a zero or negative NAV is still in it, the
    scheme's latest NAV is not a price (for example a segregated portfolio), so returns are undefined
    and it gets no metrics instead of failing the whole comparison. Every other history goes to
    metrics() unchanged.
    """
    if not history:
        return None, None
    if any(not _is_price(row) for row in history):
        logger.warning('Scheme %s reports a zero or negative latest NAV; returns and risk are not calculated', scheme_code)
        return None, 'Returns and risk are not available: this scheme reports a zero NAV (for example a segregated portfolio).'
    return metrics(history, risk_free_rate), None


def _today_window() -> tuple[date, date]:
    end = date.today()
    return end - timedelta(days=365 * 5 + 7), end


# --- Timing: where a comparison's time goes (cache, AMFI, MFAPI, validation, calculation) ----------------
_timings: contextvars.ContextVar['Timings | None'] = contextvars.ContextVar('fund_analyzer_timings', default=None)


class Timings:
    """Durations and counts for one request, shared by its worker threads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.seconds: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    def add(self, name: str, seconds: float) -> None:
        with self._lock:
            self.seconds[name] = self.seconds.get(name, 0.0) + seconds

    def count(self, name: str) -> None:
        with self._lock:
            self.counts[name] = self.counts.get(name, 0) + 1

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {'ms': {name: round(value * 1000) for name, value in self.seconds.items()}, 'counts': dict(self.counts)}


@contextmanager
def _timed(name: str):
    started = perf_counter()
    try:
        yield
    finally:
        collector = _timings.get()
        if collector:
            collector.add(name, perf_counter() - started)


def _count(name: str) -> None:
    collector = _timings.get()
    if collector:
        collector.count(name)


# --- NAV history store: one entry per scheme, in the shared Django cache --------------------------------
# Redis when REDIS_URL is set (shared by every process and every Vercel instance); otherwise each process's
# own memory. The entry is checked against AMFI's latest NAV date for the scheme, so a history that already
# reaches it is used without any request, and an older one is extended with just the missing days.
NAV_CACHE_PREFIX = 'fund-analyzer:nav:v1:'
NAV_CACHE_SECONDS = 30 * 86400  # kept a month, so an upstream outage can still be answered from it
NAV_RECHECK_SECONDS = 6 * 3600  # when AMFI's latest date for the scheme is not known
NAV_LAG_RECHECK_SECONDS = 30 * 60  # AMFI listed a newer NAV than its history service has yet
NAV_LOCK_SECONDS = 45
NAV_WAIT_SECONDS = 15  # how long a request waits for another process already fetching the same scheme
STALE_HISTORY_NOTE = 'Returns and risk use NAV history up to {date}; newer NAVs could not be loaded right now. Press Update analysis again later for the newest figures.'
_nav_locks: dict[str, Lock] = {}
_nav_locks_guard = Lock()


def _nav_key(scheme_code: str) -> str:
    return f'{NAV_CACHE_PREFIX}{scheme_code}'


def _cache_get(key: str) -> Any:
    try:
        return cache.get(key)
    except Exception:
        logger.exception('Fund Analyzer cache read failed for %s', key)
        return None


def _nav_fresh(entry: dict[str, Any] | None, newest: str | None) -> bool:
    """True if the stored history needs no request: it reaches AMFI's latest NAV date, or was checked lately."""
    if not entry or not entry.get('rows'):
        return False
    if newest:
        if str(entry.get('last_date') or '') >= newest:
            return True
        # AMFI's history service can trail its latest-NAV list by a few hours. Having just asked, ask again later.
        return entry.get('checked_for') == newest and time.time() - float(entry.get('checked_at') or 0) < NAV_LAG_RECHECK_SECONDS
    return time.time() - float(entry.get('checked_at') or 0) < NAV_RECHECK_SECONDS


def _valid_rows(rows: list[dict[str, Any]], scheme_code: str) -> list[dict[str, Any]]:
    """NAV rows with a real date (not in the future) and a number, one per date, oldest first.

    Prices are not changed; rows that cannot be read as a dated NAV are left out (and logged).
    Zero or negative NAVs stay here and are handled by _priced_history, as before.
    """
    latest_allowed = (date.today() + timedelta(days=1)).isoformat()
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        day, nav = str(row.get('date') or ''), row.get('nav')
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        if day > latest_allowed or not isinstance(nav, (int, float)) or isinstance(nav, bool):
            continue
        by_date[day] = row
    if len(by_date) != len(rows):
        logger.info('Scheme %s: kept %d of %d NAV rows (undated, future-dated or repeated dates left out)', scheme_code, len(by_date), len(rows))
    return [by_date[day] for day in sorted(by_date)]


def _mfapi_identity_problem(scheme_code: str, amfi_record: dict[str, Any] | None, raw_meta: dict[str, Any], meta: dict[str, Any]) -> str | None:
    """Why MFAPI's data is not this exact scheme, or None. Backup data is never accepted on a near match."""
    returned = str(raw_meta.get('scheme_code') or '').strip()
    if returned and returned != str(scheme_code):
        return f'MFAPI returned scheme {returned}, not {scheme_code}'
    if not amfi_record:
        return None
    conflict = plan_option_conflict(amfi_record, meta)
    if conflict:
        return f'MFAPI data for scheme {scheme_code} is a different plan or option: {conflict}'
    empty = {None, '', '-'}
    amfi_isins = {str(value).strip().upper() for value in (amfi_record.get('isin'), amfi_record.get('isin_reinvest')) if value not in empty}
    backup_isins = {str(value).strip().upper() for value in (raw_meta.get('isin_growth'), raw_meta.get('isin_div_reinvestment')) if value not in empty}
    if amfi_isins and backup_isins and not amfi_isins & backup_isins:
        return f'MFAPI data for scheme {scheme_code} has ISIN {", ".join(sorted(backup_isins))}, AMFI has {", ".join(sorted(amfi_isins))}'
    return None


def _refresh_nav(scheme_code: str, amfi_record: dict[str, Any] | None, entry: dict[str, Any] | None, newest: str | None = None) -> dict[str, Any]:
    """A new store entry: AMFI first (only the missing days when an AMFI history is stored), then MFAPI.

    AMFI's history is used only for a scheme in AMFI's latest NAV list, as before; MFAPI's only when it is
    the exact scheme, plan and option.
    """
    start, end = _today_window()
    if amfi_record is not None:
        try:
            if entry and entry.get('source') == 'AMFI' and entry.get('rows'):
                since = date.fromisoformat(entry['last_date']) + timedelta(days=1)
                try:
                    with _timed('amfi'):
                        fresh = fetch_amfi_history(scheme_code, since, end) if since <= end else []
                except ProviderError as exc:
                    if 'returned no history' not in str(exc):
                        raise
                    fresh = []  # nothing newer published yet: keep the stored history, not a failure
                rows = [*entry['rows'], *fresh]
                _count('amfi_incremental')
            else:
                with _timed('amfi'):
                    rows = fetch_amfi_history(scheme_code, start, end)
                _count('amfi_full')
            with _timed('validate'):
                rows = _valid_rows(rows, scheme_code)
            if not rows:
                raise ProviderError(f'AMFI returned no usable history for scheme {scheme_code}')
            # Kept a month beyond the five-year window, so the stored history does not grow without end.
            keep_from = (start - timedelta(days=31)).isoformat()
            rows = [row for row in rows if row['date'] >= keep_from] or rows
            return {'source': 'AMFI', 'rows': rows, 'meta': dict(amfi_record), 'last_date': rows[-1]['date'], 'checked_at': time.time(), 'checked_for': newest}
        except (ProviderError, ValueError, TypeError, KeyError) as exc:
            logger.warning('AMFI fallback for scheme %s: %s', scheme_code, exc)
    with _timed('mfapi'):
        payload = fetch_mfapi_data(scheme_code)
        meta, rows = parse_mfapi(payload, scheme_code)
    _count('mfapi')
    with _timed('validate'):
        problem = _mfapi_identity_problem(scheme_code, amfi_record, (payload or {}).get('meta') or {}, meta)
        if problem:
            _count('mfapi_rejected')
            raise ProviderError(problem)
        rows = _valid_rows(rows, scheme_code)
    if not rows:
        raise ProviderError(f'MFAPI returned no usable history for scheme {scheme_code}')
    return {'source': 'MFAPI', 'rows': rows, 'meta': meta, 'last_date': rows[-1]['date'], 'checked_at': time.time()}


def _lock_for(scheme_code: str) -> Lock:
    with _nav_locks_guard:
        return _nav_locks.setdefault(str(scheme_code), Lock())


def _served(entry: dict[str, Any], *, stale: bool) -> dict[str, Any]:
    """The rows a caller uses: an AMFI history over the usual five-year window, an MFAPI history as received."""
    rows = entry['rows']
    if entry.get('source') == 'AMFI':
        start, end = _today_window()
        rows = [row for row in rows if start.isoformat() <= row['date'] <= end.isoformat()]
    return {'source': entry.get('source'), 'rows': rows, 'meta': entry.get('meta'), 'stale': stale, 'last_date': entry.get('last_date')}


def _nav_history(scheme_code: str, amfi_record: dict[str, Any] | None) -> dict[str, Any]:
    """The scheme's NAV history: from the store when current, refreshed once when not, stale only if refreshing fails.

    Returns {source: 'AMFI'|'MFAPI', rows, meta, stale, last_date}. Raises ProviderError when no source has it.
    Simultaneous requests for one scheme fetch it once: a lock per scheme inside each process, and a short
    shared lock in the cache so another process waits for the first one's result instead of fetching again.
    A stored history is never replaced by a failed refresh; it is served marked stale instead.
    """
    key = _nav_key(scheme_code)
    newest = _iso_date(amfi_record.get('date')) if amfi_record else None
    with _timed('cache'):
        entry = _cache_get(key)
    if _nav_fresh(entry, newest):
        _count('cache_hit')
        return _served(entry, stale=False)
    with _lock_for(scheme_code):
        with _timed('cache'):
            entry = _cache_get(key) or entry
        if _nav_fresh(entry, newest):
            _count('cache_hit')
            return _served(entry, stale=False)
        owns = True
        try:
            # False means another process holds the lock. None is an unreachable Redis (IGNORE_EXCEPTIONS):
            # nobody can hold it then, so fetch now rather than wait.
            owns = cache.add(f'{key}:lock', True, timeout=NAV_LOCK_SECONDS) is not False
        except Exception:
            logger.exception('Fund Analyzer cache lock failed for %s', key)
        if not owns:
            waited_until = time.monotonic() + NAV_WAIT_SECONDS
            while time.monotonic() < waited_until:
                time.sleep(0.25)
                current = _cache_get(key)
                if _nav_fresh(current, newest):
                    _count('cache_hit_after_wait')
                    return _served(current, stale=False)
        _count('cache_miss')
        try:
            fresh = _refresh_nav(scheme_code, amfi_record, entry, newest)
        except (ProviderError, ValueError, TypeError, KeyError) as exc:
            if entry and entry.get('rows'):
                logger.warning('Scheme %s: serving stored NAV history up to %s; refresh failed: %s', scheme_code, entry.get('last_date'), exc)
                _count('stale_served')
                return _served(entry, stale=True)
            _count('failed')
            raise ProviderError(f'Unable to load scheme {scheme_code} from AMFI or MFAPI') from exc
        finally:
            if owns:
                try:
                    cache.delete(f'{key}:lock')
                except Exception:
                    logger.exception('Fund Analyzer cache unlock failed for %s', key)
        try:
            cache.set(key, fresh, timeout=NAV_CACHE_SECONDS)
        except Exception:
            logger.exception('Fund Analyzer cache write failed for %s', key)
        return _served(fresh, stale=False)


def fetch_fund_data(scheme_code: str, *, risk_free_rate: float = 0.065, include_history: bool = True, include_metadata: bool = True, latest_records: list[dict[str, Any]] | None = None, ter_snapshot: dict[str, tuple[float | None, str | None]] | None = None, aum_data: tuple[float | None, str | None] | None = None) -> dict[str, Any]:
    """Fetch a fund: NAV history from the store first, then AMFI, then a validated MFAPI copy."""
    try:
        latest_records = latest_records if latest_records is not None else fetch_amfi_latest()
    except ProviderError as exc:
        logger.warning('AMFI latest snapshot unavailable for scheme %s: %s', scheme_code, exc)
        latest_records = []
    try:
        amfi_record = _pick_latest(latest_records, scheme_code)
    except ProviderError:
        amfi_record = None

    nav = None
    if include_history or amfi_record is None:
        nav = _nav_history(scheme_code, amfi_record)
    history = []
    if nav is not None and include_history:
        with _timed('validate'):
            history = _priced_history(nav['rows'], scheme_code)

    if nav is not None and (nav['source'] == 'MFAPI' or amfi_record is None):
        rows = history or nav['rows']
        last = rows[-1] if rows else {}
        source = 'MFAPI (backup)' if nav['source'] == 'MFAPI' else 'AMFI'
        result = _base_detail({**(nav['meta'] or {}), 'scheme_code': scheme_code, **last}, source=source, as_of=last.get('date'))
        result.update({'ter': None, 'ter_as_of': None, 'aum': None, 'aum_unit': None, 'aum_as_of': None, 'history': history})
    else:
        ter, ter_as_of = (None, None)
        aum, aum_as_of = (None, None)
        if include_metadata:
            ter, ter_as_of = fetch_amfi_ter(scheme_code, latest_records, ter_snapshot)
            aum, aum_as_of = aum_data if aum_data is not None else fetch_amfi_aum(amfi_record.get('scheme_name') or '', amfi_record.get('amc') or '')
        result = _base_detail(amfi_record, source='AMFI', as_of=amfi_record.get('date'))
        result.update({'ter': ter, 'ter_as_of': ter_as_of, 'aum': aum, 'aum_unit': 'lakhs' if aum is not None else None, 'aum_as_of': aum_as_of, 'history': history})
    with _timed('calc'):
        result['metrics'], result['metrics_unavailable'] = _metrics_or_reason(history, risk_free_rate, scheme_code)
    if nav is not None and include_history:
        result['history_as_of'] = nav['last_date']
        result['history_stale'] = nav['stale']
        if nav['stale'] and not result['metrics_unavailable']:
            result['history_note'] = STALE_HISTORY_NOTE.format(date=nav['last_date'])
    return result


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


COMPARE_OMITTED_SERIES = ('growthOf10000', 'rollingReturns')
NAV_HISTORY_UNAVAILABLE ='NAV history could not be loaded right now, so returns and risk are not shown. Press Update analysis again in a moment.'


def _unavailable_fund(scheme_code: str, latest_records: list[dict[str, Any]]) -> dict[str, Any]:
    """A fund row with no figures, named from the AMFI scheme list, for a fund whose NAV history failed."""
    record = next((row for row in latest_records if str(row.get('scheme_code')) == str(scheme_code)), None) or {'scheme_code': scheme_code}
    result = _base_detail(record, source='AMFI', as_of=record.get('date'))
    result.update({
        'ter': None, 'ter_as_of': None, 'aum': None, 'aum_unit': None, 'aum_as_of': None,
        'history': [], 'metrics': None, 'metrics_unavailable': NAV_HISTORY_UNAVAILABLE, 'history_unavailable': True,
    })
    return result


def compare_funds(scheme_codes: list[str], risk_free_rate: float = 0.065) -> dict[str, Any]:
    started = perf_counter()
    collector = Timings()
    token = _timings.set(collector)
    try:
        return _compare_funds(scheme_codes, risk_free_rate, collector, started)
    finally:
        _timings.reset(token)


def _compare_funds(scheme_codes: list[str], risk_free_rate: float, collector: Timings, started: float) -> dict[str, Any]:
    try:
        with _timed('amfi_latest'):
            latest_records = fetch_amfi_latest()
    except ProviderError as exc:
        logger.warning('AMFI latest snapshot unavailable for comparison: %s', exc)
        # An empty snapshot skips another latest-NAV retry per selected fund and
        # lets each fund go directly to the MFAPI fallback.
        latest_records = []

    # History and analytics are the critical path for Compare. AUM still loads only
    # when the client opens Fundamentals.
    # One fund that cannot be loaded (both NAV sources down, or the backup holding another plan's data) is
    # reported on its own row. It never fails the comparison of every other fund the client selected.
    def load(code: str) -> dict[str, Any]:
        _timings.set(collector)  # each worker thread reports into this request's timings
        try:
            return fetch_fund_data(code, risk_free_rate=risk_free_rate, include_metadata=False, latest_records=latest_records)
        except ProviderError as exc:
            logger.warning('Fund Analyzer comparison: scheme %s unavailable: %s', code, exc)
            return _unavailable_fund(code, latest_records)

    worker_count = min(4, len(scheme_codes))
    with ThreadPoolExecutor(max_workers=max(1, worker_count)) as pool:
        funds = list(pool.map(load, scheme_codes))
    if all(fund.get('history_unavailable') for fund in funds):
        raise ProviderError('No selected fund could be loaded from AMFI or MFAPI')
    # TER shares a host with NAV history, and that host throttles bursts, so load it
    # afterwards. A TER failure must not fail the comparison.
    try:
        with _timed('ter'):
            ter_snapshot = fetch_amfi_ter_snapshot(scheme_codes, latest_records)
    except Exception:
        logger.exception('Fund Analyzer TER lookup failed')
        ter_snapshot = {}
    for fund in funds:
        fund['ter'], fund['ter_as_of'] = ter_snapshot.get(fund['scheme_code'], (None, None))
    matrix = {fund['scheme_code']: {} for fund in funds}
    with _timed('correlation'):
        for left in funds:
            for right in funds:
                if left['scheme_code'] == right['scheme_code']:
                    matrix[left['scheme_code']][right['scheme_code']] = 1.0
                else:
                    matrix[left['scheme_code']][right['scheme_code']] = correlation(left['history'], right['history'])
    collector.add('total', perf_counter() - started)
    timings = collector.summary()
    logger.info('Fund Analyzer comparison loaded %d funds in %.2fs; timings %s', len(funds), perf_counter() - started, timings)
    # The page shows each fund's figures, never these daily chart series (about 530 KB a fund). Leaving them out
    # keeps a comparison far below the 4.5 MB response limit of serverless hosting such as Vercel: 36 funds were
    # 10.9 MB with them. The single-fund endpoint still returns them.
    for fund in funds:
        if isinstance(fund.get('metrics'), dict):
            fund['metrics'] = {key: value for key, value in fund['metrics'].items() if key not in COMPARE_OMITTED_SERIES}
    source = 'MFAPI (backup)' if any(fund['source'] == 'MFAPI (backup)' for fund in funds) else 'AMFI'
    as_of = max((fund.get('asOfDate') for fund in funds if fund.get('asOfDate')), default=None)
    valid = [fund for fund in funds if fund.get('metrics')]
    top_cagr = max(valid, key=lambda fund: fund['metrics'].get('r3y') if fund['metrics'].get('r3y') is not None else float('-inf'), default=None)
    best_sharpe = max(valid, key=lambda fund: fund['metrics'].get('sharpe') if fund['metrics'].get('sharpe') is not None else float('-inf'), default=None)
    lowest_vol = min(valid, key=lambda fund: fund['metrics'].get('volatility') if fund['metrics'].get('volatility') is not None else float('inf'), default=None)
    return {'source': source, 'asOfDate': as_of, 'funds': funds, 'correlation': matrix, 'timings': timings, 'summary': {
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
