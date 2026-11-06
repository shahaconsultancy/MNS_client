"""Charlie Alpha API: an optional, third-party backup for portfolio holdings.

The official AMC reader stays the primary source. This module is used only for a fund the official
reader could not supply, and only when FUND_ANALYZER_CHARLIE_ALPHA_ENABLED is on. Its data is never
trusted as received: every portfolio is checked against the AMFI record and against itself, then
turned into the same internal shape the official reader produces. A response that fails any check is
rejected with a specific code. It is never repaired, topped up from another source, or counted as 0%.

Verified against the live API (OpenAPI at /v1/openapi.json):
    GET /mf/holdings-periods/{code}   -> amfi_code, isin, scheme_name, latest_as_of, periods_count,
                                         periods[{as_of, holdings_count}]
    GET /mf/holdings/{code}?as_of=    -> amfi_code, isin, scheme_name, category, as_of, total_holdings,
                                         holdings[{isin, name, industry, holding_type, holding_pct, quantity}]
    GET /mf/overlap?schemes=a,b,...   -> the provider's own overlap. Used only to cross-check, never shown.
No key is needed. The limit is 50 requests per 10 seconds. An unknown scheme answers 404.
"""
from __future__ import annotations

import contextvars
import logging
import re
import time
from collections import deque
from datetime import date, datetime
from threading import Lock
from typing import Any

import requests
from django.conf import settings

from .adapters import scheme_link_matches
from .portfolio_store import ASSET_DEBT, ASSET_EQUITY, ASSET_FOF, ASSET_FUND_UNIT, MAX_DEBT_WEIGHT, MAX_EQUITY_WEIGHT
from .providers import ProviderError

logger = logging.getLogger(__name__)

SOURCE_OFFICIAL = 'official_amc'
SOURCE_THIRD_PARTY = 'charlie_alpha'
SOURCE_LABELS = {SOURCE_OFFICIAL: 'Official AMC', SOURCE_THIRD_PARTY: 'Third-party API'}

# Failure codes. None of them is ever shown to a client as raw text.
DISABLED = 'BACKUP_DISABLED'
NOT_FOUND = 'NOT_FOUND'
RATE_LIMITED = 'RATE_LIMITED'
TIMEOUT = 'TIMEOUT'
HTTP_ERROR = 'HTTP_ERROR'
INVALID_JSON = 'INVALID_JSON'
BUDGET_EXHAUSTED = 'REQUEST_BUDGET_EXHAUSTED'
NO_PERIODS = 'NO_REPORTING_PERIODS'
SCHEME_MISMATCH = 'SCHEME_MISMATCH'
DATE_MISMATCH = 'REPORTING_DATE_MISMATCH'
INCOMPLETE = 'INCOMPLETE_PORTFOLIO'
INVALID_ISIN = 'INVALID_SECURITY_ISIN'
BAD_WEIGHT = 'INVALID_WEIGHTS'
WRONG_CLASS = 'CLASSIFICATION_MISMATCH'
NO_HOLDINGS = 'NO_HOLDINGS'
UNAVAILABLE = 'PROVIDER_TEMPORARILY_UNAVAILABLE'

REQUEST_TIMEOUT = (3, 12)
MAX_ATTEMPTS = 3
RATE_LIMIT_REQUESTS = 40          # the provider allows 50 per 10 seconds. Stay below it.
RATE_LIMIT_WINDOW = 10.0
MAX_RATE_WAIT = 5.0               # never queue behind the limiter longer than this
MAX_RETRY_AFTER = 3.0
CACHE_SECONDS = 60 * 60
# After the provider times out or keeps failing, stop asking for a while instead of making every fund wait again.
BREAKER_SECONDS = 60.0
CACHE_ENTRIES = 256

# A complete disclosure adds up to the whole scheme, cash included. A top-10 list cannot.
COMPLETE_MIN_TOTAL = 98.0
COMPLETE_MAX_TOTAL = 105.0
INVALID_ISIN_TOLERANCE = 1.0      # percent of NAV that may sit on unverifiable ISINs before the portfolio is refused
EQUITY_FUND_MIN_EQUITY = 30.0
DEBT_FUND_MAX_EQUITY = 10.0
FOF_MIN_UNITS = 50.0

_OTHER_LABELS = {'cash': 'Cash & Margin', 'derivative': 'Derivatives', 'commodity': 'Commodity'}
_CDMDF = re.compile(r'(?i)corporate debt market|cdmdf|alternative investment')


class CharlieError(ProviderError):
    """The backup source could not supply usable data. code says why."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(message or code)
        self.code = code


def enabled() -> bool:
    return bool(getattr(settings, 'FUND_ANALYZER_CHARLIE_ALPHA_ENABLED', False))


def base_url() -> str:
    return str(getattr(settings, 'FUND_ANALYZER_CHARLIE_ALPHA_URL', 'https://api.charliealpha.in/v1')).rstrip('/')


# --- Requests: bounded in time, in rate, and in count -------------------------------------------------

_window: deque[float] = deque()
_window_lock = Lock()
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = Lock()
_inflight: dict[tuple, Lock] = {}
_breaker = {'open_until': 0.0}
_budget: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar('charlie_budget', default=None)


def begin_analysis() -> None:
    """Start counting this analysis's requests. Called once per Update Analysis."""
    _budget.set([int(getattr(settings, 'FUND_ANALYZER_CHARLIE_ALPHA_MAX_REQUESTS', 40))])


def _spend() -> None:
    budget = _budget.get()
    if budget is None:
        return
    if budget[0] <= 0:
        raise CharlieError(BUDGET_EXHAUSTED, 'Too many backup requests for one analysis')
    budget[0] -= 1


def _wait_for_slot() -> None:
    deadline = time.monotonic() + MAX_RATE_WAIT
    while True:
        with _window_lock:
            now = time.monotonic()
            while _window and now - _window[0] >= RATE_LIMIT_WINDOW:
                _window.popleft()
            if len(_window) < RATE_LIMIT_REQUESTS:
                _window.append(now)
                return
            wait = RATE_LIMIT_WINDOW - (now - _window[0])
        if time.monotonic() + wait > deadline:
            raise CharlieError(RATE_LIMITED, 'Local request limit reached')
        time.sleep(max(wait, 0.01))


def _get_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One GET with a timeout, a few retries on throttling or server trouble, and strict JSON checks."""
    if time.monotonic() < _breaker['open_until']:
        raise CharlieError(UNAVAILABLE, 'Backup source recently failed. Not asking again yet.')
    url = f'{base_url()}{path}'
    last: CharlieError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _spend()
        _wait_for_slot()
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, headers={'Accept': 'application/json', 'User-Agent': 'MNS-FundAnalyzer/1.0'})
        except requests.Timeout as exc:
            last = CharlieError(TIMEOUT, str(exc))
        except requests.RequestException as exc:
            last = CharlieError(HTTP_ERROR, str(exc))
        else:
            status = response.status_code
            if status == 404:
                raise CharlieError(NOT_FOUND, 'Scheme not covered by the backup source')
            if status == 429:
                try:
                    delay = min(float(response.headers.get('Retry-After', 1)), MAX_RETRY_AFTER)
                except ValueError:
                    delay = 1.0
                last = CharlieError(RATE_LIMITED, 'Provider rate limit')
                if attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                continue
            if status >= 500:
                last = CharlieError(HTTP_ERROR, f'Provider error {status}')
            elif status >= 400:
                # 403 and other client errors will not change on a retry.
                raise CharlieError(HTTP_ERROR, f'Provider refused the request ({status})')
            else:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise CharlieError(INVALID_JSON, 'Provider returned invalid JSON') from exc
                if not isinstance(payload, dict):
                    raise CharlieError(INVALID_JSON, 'Provider returned an unexpected response')
                return payload
        if attempt < MAX_ATTEMPTS and last and last.code != RATE_LIMITED:
            time.sleep(0.4 * attempt)
    # Every attempt failed on time, throttling or a server error: the provider itself is in trouble.
    _breaker['open_until'] = time.monotonic() + BREAKER_SECONDS
    raise last or CharlieError(HTTP_ERROR)


def _cached(key: tuple, load):
    """One request per key at a time, and a short memory so repeat analyses do not repeat requests."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        lock = _inflight.setdefault(key, Lock())
    with lock:
        with _cache_lock:
            entry = _cache.get(key)
            if entry and entry[0] > time.monotonic():
                return entry[1]
        value = load()
        with _cache_lock:
            _cache[key] = (time.monotonic() + CACHE_SECONDS, value)
            while len(_cache) > CACHE_ENTRIES:
                del _cache[min(_cache, key=lambda item: _cache[item][0])]
        return value


def clear_memory() -> None:
    with _cache_lock:
        _cache.clear()
    with _window_lock:
        _window.clear()
    _breaker['open_until'] = 0.0


def list_periods(record: dict[str, Any]) -> list[str]:
    """Reporting dates (newest first) the provider holds for this exact AMFI scheme."""
    code = str(record.get('scheme_code'))
    payload = _cached(('periods', code), lambda: _get_json(f'/mf/holdings-periods/{code}'))
    if str(payload.get('amfi_code')) != code:
        raise CharlieError(SCHEME_MISMATCH, 'Provider answered for a different scheme')
    periods = payload.get('periods')
    if not isinstance(periods, list):
        raise CharlieError(INVALID_JSON, 'Periods missing')
    dates = []
    for item in periods:
        try:
            as_of = date.fromisoformat(str(item.get('as_of')))
        except (AttributeError, ValueError):
            continue
        if as_of <= date.today():
            dates.append(as_of.isoformat())
    if not dates:
        raise CharlieError(NO_PERIODS, 'No reporting periods')
    return sorted(set(dates), reverse=True)


def fetch_holdings(record: dict[str, Any], as_of: str) -> dict[str, Any]:
    code = str(record.get('scheme_code'))
    return _cached(('holdings', code, as_of), lambda: _get_json(f'/mf/holdings/{code}', {'as_of': as_of}))


def endpoint_reference(code: str, as_of: str) -> str:
    return f'{base_url()}/mf/holdings/{code}?as_of={as_of}'


def cross_check_overlap(scheme_codes: list[str]) -> dict[str, Any]:
    """The provider's own overlap for these schemes. For debugging and comparison only.

    Nothing in the analyzer reads this: every figure shown comes from our own calculation on
    validated holdings. It exists so a person can check that the two agree.
    """
    return _get_json('/mf/overlap', {'schemes': ','.join(str(code) for code in scheme_codes), 'include_pairwise': 'true'})


# --- Validation and conversion ---------------------------------------------------------------------------

def isin_is_valid(isin: str) -> bool:
    """Shape and check digit. A wrong digit means a typo or a placeholder, and nothing can be matched on it."""
    if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', isin):
        return False
    digits = ''.join(str(int(char, 36)) for char in isin[:11])
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 == 0 else 1)
        total += value // 10 + value % 10
    return (10 - total % 10) % 10 == int(isin[11])


def classify(isin: str | None, label: str | None) -> tuple[str, str | None]:
    """(class, other-label). The ISIN's own structure decides, not the provider's label.

    The provider mislabels instruments: bonds, state securities and commercial paper turn up as
    'equity'. An Indian ISIN carries its security type in characters 8-9: 01 is an equity share,
    23 and 25 are InvIT or REIT units, and any other type is a debt instrument.
    """
    label = (label or '').lower()
    if label in _OTHER_LABELS:
        return 'other', _OTHER_LABELS[label]
    if not isin:
        return 'other', 'Other'
    if isin.startswith('INF'):
        return ASSET_FUND_UNIT, None
    if isin.startswith('IN'):
        kind = isin[7:9]
        if kind == '01':
            return ASSET_EQUITY, None
        if kind in ('23', '25'):
            return 'other', 'REIT / InvIT Units'
        return ASSET_DEBT, None
    return (ASSET_EQUITY, None) if label == 'equity' else ('other', 'Other')


def _reject(code: str, message: str):
    raise CharlieError(code, message)


def _parse_weight(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        _reject(BAD_WEIGHT, 'A holding has no valid weight')
    return float(value)


def normalise_portfolio(record: dict[str, Any], payload: dict[str, Any], *, requested_as_of: str, asset_class: str, payload_asset_class: str | None) -> dict[str, Any]:
    """Check a provider portfolio and convert it to the internal format. Raises CharlieError if it fails any check.

    asset_class is what our own AMFI record says the scheme is. payload_asset_class is what the
    provider's category says. They must agree: a portfolio of another kind of fund is the wrong one.
    """
    from . import holdings as H  # constants and classes live there. Imported late: holdings imports this module.

    code = str(record.get('scheme_code'))
    # 1. The right scheme. The AMFI code must be the one asked for, and the scheme's own ISIN and name must agree.
    if str(payload.get('amfi_code')) != code:
        _reject(SCHEME_MISMATCH, 'AMFI code differs')
    known_isins = {str(value).strip().upper() for value in (record.get('isin'), record.get('isin_reinvest')) if value}
    scheme_isin = str(payload.get('isin') or '').strip().upper()
    if known_isins and scheme_isin and scheme_isin not in known_isins:
        _reject(SCHEME_MISMATCH, 'Scheme ISIN differs')
    if not scheme_link_matches(str(payload.get('scheme_name') or ''), str(record.get('scheme_name') or '')):
        _reject(SCHEME_MISMATCH, 'Scheme name differs')
    if payload_asset_class is not None and payload_asset_class != asset_class:
        _reject(SCHEME_MISMATCH, 'Scheme category differs')

    # 2. The right date.
    try:
        as_of = date.fromisoformat(str(payload.get('as_of')))
    except ValueError:
        _reject(DATE_MISMATCH, 'No reporting date')
    if as_of.isoformat() != requested_as_of or as_of > date.today():
        _reject(DATE_MISMATCH, 'Reporting date differs from the one requested')

    # 3. A whole portfolio, not a top-N list.
    rows = payload.get('holdings')
    if not isinstance(rows, list) or not rows:
        _reject(NO_HOLDINGS, 'No holdings')
    reported = payload.get('total_holdings')
    if reported is not None and reported != len(rows):
        _reject(INCOMPLETE, 'Row count differs from the stated total')
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            _reject(INVALID_JSON, 'Malformed holding')
        total += _parse_weight(row.get('holding_pct'))
    if not COMPLETE_MIN_TOTAL <= total <= COMPLETE_MAX_TOTAL:
        _reject(INCOMPLETE, f'Weights add up to {total:.2f}%, not a whole portfolio')

    # 4. Row by row: valid ISINs, valid weights, and a class taken from the ISIN.
    is_fof = asset_class == ASSET_FOF
    holdings: dict[str, float] = {}
    names: dict[str, str] = {}
    industries: dict[str, str] = {}
    instrument_types: dict[str, str] = {}
    classes: dict[str, str] = {}
    others: dict[str, dict[str, Any]] = {}
    duplicates = invalid_weight = label_mismatch_rows = 0
    label_mismatch_weight = 0.0

    def add_other(key: str, isin: str | None, name: str, weight: float, instrument_type: str) -> None:
        entry = others.setdefault(key, {**({'isin': isin} if isin else {}), 'name': name, 'weight': 0.0, 'instrument_type': instrument_type})
        entry['weight'] += weight

    for row in rows:
        weight = float(row['holding_pct'])
        name = re.sub(r'\s+', ' ', str(row.get('name') or '')).strip()
        label = str(row.get('holding_type') or '').lower()
        isin = str(row.get('isin') or '').strip().upper() or None
        if isin and not isin_is_valid(isin):
            # A placeholder or typo cannot be matched against another fund. Keep it visible, never match on it.
            invalid_weight += abs(weight)
            add_other(f'invalid|{isin}', None, name or isin, weight, 'Unverified ISIN')
            continue
        security_class, other_label = classify(isin, label)
        if security_class in (ASSET_EQUITY, ASSET_DEBT) and label == 'equity' and security_class != ASSET_EQUITY:
            label_mismatch_rows += 1
            label_mismatch_weight += weight
        if security_class == 'other':
            add_other(isin or f'{other_label}|{name}', isin, name or other_label or 'Other', weight, other_label or 'Other')
            continue
        if security_class == ASSET_FUND_UNIT:
            if _CDMDF.search(name):
                add_other(isin, isin, name, weight, H.ALTERNATIVE_FUND_UNITS)
                continue
            if not is_fof:
                # A scheme's holding of another fund's units is not a direct security. The official reader keeps it aside too.
                add_other(isin, isin, name, weight, H.MUTUAL_FUND_UNITS)
                continue
        elif is_fof:
            # A fund of funds is compared on its fund units only. Anything else it holds stays visible but aside.
            add_other(isin, isin, name, weight, 'Other')
            continue
        if weight < 0 or weight > 100:
            _reject(BAD_WEIGHT, 'A security weight is outside 0-100%')
        if weight == 0:
            # Below 0.005% of NAV. Official files print these as a symbol, not a number, so they are not
            # holdings there either, and a 0% row would only inflate a pair's count of common securities.
            continue
        if isin in holdings:
            duplicates += 1
        holdings[isin] = holdings.get(isin, 0.0) + weight
        names.setdefault(isin, name or isin)
        if row.get('industry'):
            industries.setdefault(isin, str(row['industry']).strip())
        classes[isin] = security_class
        if security_class == ASSET_FUND_UNIT:
            instrument_types[isin] = H.MUTUAL_FUND_UNITS
        elif security_class == ASSET_DEBT:
            instrument_types[isin] = 'Money Market Instrument' if label == 'money_market' else 'Debt Instrument'

    if invalid_weight > INVALID_ISIN_TOLERANCE:
        _reject(INVALID_ISIN, f'{invalid_weight:.2f}% of NAV sits on unverifiable ISINs')
    if not holdings:
        # A fund of funds whose rows hold no fund units is the wrong kind of portfolio, not an empty one.
        if is_fof and len(others) > 1:
            _reject(WRONG_CLASS, 'A fund of funds with no fund units')
        _reject(NO_HOLDINGS, 'No security rows with a valid ISIN')

    # 5. The weights must fit the same limits the official reader and the JSON cache enforce.
    equity_total = sum(weight for isin, weight in holdings.items() if classes[isin] == ASSET_EQUITY)
    unit_total = sum(weight for isin, weight in holdings.items() if classes[isin] == ASSET_FUND_UNIT)
    if equity_total > MAX_EQUITY_WEIGHT or sum(holdings.values()) > MAX_DEBT_WEIGHT:
        _reject(BAD_WEIGHT, 'Security weights exceed the scheme')

    # 6. The portfolio must look like this kind of fund.
    if asset_class == ASSET_EQUITY and equity_total < EQUITY_FUND_MIN_EQUITY:
        _reject(WRONG_CLASS, 'An equity scheme with almost no equity')
    if asset_class == ASSET_DEBT and equity_total > DEBT_FUND_MAX_EQUITY:
        _reject(WRONG_CLASS, 'A debt scheme with a large equity holding')
    if is_fof and unit_total < FOF_MIN_UNITS:
        _reject(WRONG_CLASS, 'A fund of funds with few fund units')

    other_rows = [{**entry, 'weight': round(entry['weight'], 6)} for entry in others.values()]
    unclassified = sum(entry['weight'] for entry in other_rows if entry['instrument_type'] in ('Other', 'Unverified ISIN'))
    return {
        'holdings': {isin: round(weight, 6) for isin, weight in holdings.items()},
        'names': names, 'industries': industries, 'instrument_types': instrument_types, 'issuers': {},
        'other_holdings': other_rows,
        'security_classes': classes,
        'classes_read': [ASSET_FUND_UNIT] if is_fof else [ASSET_EQUITY, ASSET_DEBT],
        'unclassified_weight': unclassified,
        'portfolio_date': as_of.isoformat(),
        'benchmark': None, 'risk_level': None,
        'validation': {
            'status': 'VERIFIED', 'provider': SOURCE_THIRD_PARTY, 'rows': len(rows), 'total_pct': round(total, 4),
            'security_rows': len(holdings), 'duplicate_isin_rows_merged': duplicates,
            'unverifiable_isin_weight': round(invalid_weight, 4),
            # The provider's labels were wrong for these rows. Classes came from the ISINs, so results are not affected.
            'label_mismatch_rows': label_mismatch_rows, 'label_mismatch_weight': round(label_mismatch_weight, 4),
            'checks': ['scheme', 'reporting_date', 'complete_portfolio', 'isin_check_digits', 'weights', 'classification'],
            'checked_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        },
    }
