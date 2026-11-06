from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from io import BytesIO
from threading import Lock
from typing import Any, Callable
import zipfile
from urllib.parse import unquote, urljoin, urlsplit

import pandas as pd
import requests
from django.conf import settings
from django.core.cache import cache

from .adapters import (
    AMC_SOURCES,
    DISCLOSURE_NOT_PUBLISHED,
    DOWNLOAD_BLOCKED,
    DOWNLOAD_FAILED,
    HOLDINGS_AVAILABLE,
    INVALID_WORKBOOK,
    NO_COMMON_MONTH,
    PARSING_FAILED,
    SCHEME_NOT_FOUND,
    SOURCE_NOT_SUPPORTED,
    PortfolioError,
    adapter_for,
    candidate_link as _candidate_link,
    classify_error,
    is_official_url,
    month_in_text as _month_in_text,
    normalise_name as _normalise_name,
    official_domains,
    portfolio_date as _portfolio_date,
    source_for_amc,
)
from . import charlie_alpha
from .portfolio_store import ASSET_DEBT, ASSET_EQUITY, ASSET_FOF, ASSET_FUND_UNIT, ASSET_HYBRID, ISIN_PATTERN, MAX_DEBT_WEIGHT, SOURCE_CHARLIE, SOURCE_IMPORT, discovery_due, file_scheme_name, load_snapshot, record_status, source_status, store_snapshot, stored_links
from .providers import HEADERS, ProviderError, fetch_amfi_latest, fetch_url
from .riskometer import read_scheme_riskometer

logger = logging.getLogger(__name__)

PORTFOLIO_CACHE_SECONDS = 24 * 60 * 60
PAGE_CACHE_SECONDS = 60 * 60
PAGE_ERROR_CACHE_SECONDS = 5 * 60
PORTFOLIO_LOOKBACK_MONTHS = settings.FUND_ANALYZER_PORTFOLIO_LOOKBACK_MONTHS
MAX_PORTFOLIO_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKBOOK_UNPACKED_BYTES = 512 * 1024 * 1024
TOP_SECTORS = 6
REASON_NO_COMMON_SECURITIES = 'NO_COMMON_SECURITIES'
REASON_HOLDINGS_UNAVAILABLE = 'HOLDINGS_UNAVAILABLE'
REASON_PARSER_UNSUPPORTED = 'PARSER_UNSUPPORTED'
REASON_NO_COMMON_MONTH = 'NO_COMMON_REPORTING_MONTH'
REASON_NO_COMMON_FUNDS = 'NO_COMMON_UNDERLYING_FUNDS'
REASON_FOF_LOOKTHROUGH = 'FOF_LOOKTHROUGH_UNAVAILABLE'
REASON_HISTORICAL_UNAVAILABLE = 'HISTORICAL_HOLDINGS_UNAVAILABLE'
REASON_SOURCE_NOT_SUPPORTED = 'SOURCE_NOT_SUPPORTED'
REASON_FILE_UNAVAILABLE = 'OFFICIAL_FILE_UNAVAILABLE'
REASON_DOWNLOAD_BLOCKED = 'DOWNLOAD_BLOCKED'
REASON_SCHEME_NOT_IN_FILES = 'SCHEME_NOT_IN_OFFICIAL_FILES'
REASON_MESSAGES = {
    REASON_SCHEME_NOT_IN_FILES: 'Scheme not found in the AMC published portfolio files',
    REASON_FILE_UNAVAILABLE: 'Official file unavailable',
    REASON_DOWNLOAD_BLOCKED: 'Download blocked by the AMC website',
    REASON_HISTORICAL_UNAVAILABLE: 'Historical portfolio unavailable',
    REASON_SOURCE_NOT_SUPPORTED: 'Portfolio source not supported',
    REASON_NO_COMMON_SECURITIES: 'No common securities',
    REASON_HOLDINGS_UNAVAILABLE: 'Holdings unavailable',
    REASON_PARSER_UNSUPPORTED: 'Parser unsupported',
    REASON_NO_COMMON_MONTH: 'No common reporting month',
    REASON_NO_COMMON_FUNDS: 'No common underlying funds',
    REASON_FOF_LOOKTHROUGH: 'Look-through overlap is unavailable for a Fund of Funds. Only two Funds of Funds can be compared, on the fund units they hold.',
}
# The kinds of security each fund type is compared on. A pair uses what both share.
FUND_SECURITY_CLASSES = {
    ASSET_EQUITY: {ASSET_EQUITY},
    ASSET_DEBT: {ASSET_DEBT},
    ASSET_HYBRID: {ASSET_EQUITY, ASSET_DEBT},
    ASSET_FOF: {ASSET_FUND_UNIT},
}
OVERLAP_DIRECT_FOF = 'DIRECT_FOF'
OVERLAP_DIRECT_FOF_LABEL = 'Direct FoF Overlap'
OVERLAP_SECURITIES = 'SECURITIES'
# Above this share of NAV, unclassified rows are reported instead of being ignored.
MATERIAL_UNCLASSIFIED_WEIGHT = 1
PARSED_HOLDINGS_CACHE_SECONDS = 180 * 24 * 60 * 60
PORTFOLIO_CACHE_MAX_BYTES = 128 * 1024 * 1024
IMPORTED_HOLDINGS_CACHE_SECONDS = 70 * 24 * 60 * 60
_download_lock = Lock()
_file_locks: dict[tuple[str, str, str], Lock] = {}
_portfolio_file_cache: dict[tuple[str, str], tuple[datetime, dict[str, bytes]]] = {}
SBI_PORTFOLIO_LIST_URL = 'https://www.sbimf.com/ajaxcall/CMS/GetSchemePortfolioSheets'


class _NoHoldingsFound(ProviderError):
    """The file was read correctly and holds no rows of this kind."""


class _PortfolioLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            self._current = {'href': dict(attrs).get('href', ''), 'text': ''}

    def handle_data(self, data):
        if self._current is not None:
            self._current['text'] += data

    def handle_endtag(self, tag):
        if tag.lower() == 'a' and self._current is not None:
            self.links.append(self._current)
            self._current = None


def _recent_portfolio_months() -> set[str]:
    cursor = date.today().replace(day=1) - timedelta(days=1)
    months = set()
    for _ in range(PORTFOLIO_LOOKBACK_MONTHS):
        months.add(cursor.strftime('%Y-%m'))
        cursor = cursor.replace(day=1) - timedelta(days=1)
    return months


def _backup_window(allowed: set[str] | None = None) -> set[str]:
    """The completed months the backup source may supply: the newest FUND_ANALYZER_CHARLIE_ALPHA_LOOKBACK_MONTHS.

    Taken from the official window, so on 6 November 2026 with the default of 4 it is October back
    to July. allowed narrows it further, for example to one month in historical mode.
    """
    count = int(getattr(settings, 'FUND_ANALYZER_CHARLIE_ALPHA_LOOKBACK_MONTHS', 4))
    window = set(sorted(_recent_portfolio_months(), reverse=True)[:count])
    return window if allowed is None else window & set(allowed)


def _amc_directory_name(amc_name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', amc_name.lower()).strip('-')


def _import_index_key(amc_name: str, scheme_code: str) -> str:
    return f'fund-analyzer:portfolio-import:index:{_amc_directory_name(amc_name)}:{scheme_code}'


def _imported_links(amc_name: str, scheme_code: str) -> list[dict[str, str]]:
    return cache.get(_import_index_key(amc_name, scheme_code)) or []


def _parsed_index_key(scheme_code: str) -> str:
    return f'fund-analyzer:portfolio-parsed:index:{scheme_code}'


def _parsed_links(scheme_code: str) -> list[dict[str, str]]:
    return cache.get(_parsed_index_key(scheme_code)) or []


def _cache_parsed_holdings(scheme_code: str, reporting_date: str, holdings: dict[str, float], url: str, names: dict[str, str] | None = None, industries: dict[str, str] | None = None, benchmark: str | None = None, risk_level: str | None = None, instrument_types: dict[str, str] | None = None, issuers: dict[str, str] | None = None, other_holdings: list[dict[str, Any]] | None = None, extras: dict[str, Any] | None = None) -> None:
    month = reporting_date[:7]
    cache_key = f'fund-analyzer:portfolio-parsed:{scheme_code}:{month}'
    link = {'url': url, 'cache_key': cache_key, 'month': month, 'portfolio_date': reporting_date, 'cached': True}
    try:
        cache.set(cache_key, {'holdings': holdings, 'names': names or {}, 'industries': industries or {}, 'benchmark': benchmark, 'risk_level': risk_level, 'instrument_types': instrument_types or {}, 'issuers': issuers or {}, 'other_holdings': other_holdings or [], 'portfolio_date': reporting_date, **(extras or {})}, timeout=PARSED_HOLDINGS_CACHE_SECONDS)
        existing = cache.get(_parsed_index_key(scheme_code)) or []
        links = [link, *(item for item in existing if item.get('month') != month)]
        links.sort(key=lambda item: item['month'], reverse=True)
        cache.set(_parsed_index_key(scheme_code), links[:PORTFOLIO_LOOKBACK_MONTHS], timeout=PARSED_HOLDINGS_CACHE_SECONDS)
    except Exception:
        logger.exception('Parsed portfolio cache write failed for scheme %s', scheme_code)


# AMFI's older NAV file files closed and some open schemes under one-word categories.
# 'Income' is its debt category, so a fixed term plan filed there is a debt scheme.
LEGACY_DEBT_CATEGORIES = {'income', 'gilt', 'liquid', 'money market'}
LEGACY_HYBRID_CATEGORIES = {'balanced'}


def _category(record: dict) -> str:
    return re.sub(r'\s+', ' ', str(record.get('category') or '')).strip().lower()


def _is_debt_fund(record: dict) -> bool:
    category = _category(record)
    return category.startswith(('debt scheme', 'debt fund', 'income/debt')) or category in LEGACY_DEBT_CATEGORIES


def _is_hybrid_fund(record: dict) -> bool:
    category = _category(record)
    return category.startswith('hybrid') or category in LEGACY_HYBRID_CATEGORIES


# A scheme whose last NAV is older than this has stopped reporting. Its monthly portfolios
# were published back then, so searching the latest months for it can only fail.
HISTORICAL_NAV_AGE_DAYS = 120
SCHEME_ACTIVE = 'ACTIVE'
SCHEME_MATURED = 'MATURED'
SCHEME_DISCONTINUED = 'DISCONTINUED'
_CLOSED_TYPES = {'close', 'closed', 'close ended', 'closed ended', 'close-ended', 'closed-ended'}


def _nav_date(value: Any) -> date | None:
    text = str(value or '').strip()
    for pattern in ('%d-%b-%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(text[:11], pattern).date()
        except ValueError:
            continue
    return None


def scheme_status(record: dict[str, Any] | None, today: date | None = None) -> dict[str, Any]:
    """Whether a scheme still reports. A closed-ended scheme with an old last NAV has matured.

    This reads AMFI's own scheme type and last NAV date. A record with no NAV date is treated
    as active, because nothing proves otherwise.
    """
    last = _nav_date((record or {}).get('date'))
    if last is None:
        return {'status': SCHEME_ACTIVE, 'last_nav_date': None, 'nav_age_days': None}
    age = ((today or date.today()) - last).days
    if age <= HISTORICAL_NAV_AGE_DAYS:
        status = SCHEME_ACTIVE
    else:
        status = SCHEME_MATURED if str(record.get('scheme_type') or '').strip().lower() in _CLOSED_TYPES else SCHEME_DISCONTINUED
    return {'status': status, 'last_nav_date': last.isoformat(), 'nav_age_days': age}


def historical_message(names: list[str]) -> str:
    return f"Historical portfolio unavailable for {', '.join(name for name in names if name)}."


def _is_fof_fund(record: dict) -> bool:
    """A Fund of Funds by the scheme category AMFI publishes, such as 'Other Scheme - FoF Domestic'.

    The scheme's name is never used: plenty of equity and index funds have 'Fund of Funds'
    in a name without being one, and a name says nothing about what the portfolio holds.
    """
    category = re.sub(r'\s+', ' ', str(record.get('category') or '')).strip().lower()
    return bool(re.search(r'\bfof\b|\bfunds? of funds?\b', category))


def _overlap_debug_record(code: str, record: dict | None = None, *, error: str | None = None) -> dict[str, Any]:
    """Create a stable, JSON-serialisable diagnostic record for the overlap endpoint."""
    record = record or {}
    amc_name = record.get('amc') or None
    return {
        'scheme_code': str(code),
        'scheme_name': record.get('scheme_name'),
        'category': record.get('category'),
        'amc': amc_name,
        'amc_name': amc_name,
        'amc_in_sources': bool(amc_name and amc_name in AMC_SOURCES),
        'monthly_links_found': 0,
        'months_found': [],
        'chosen_common_month': None,
        'matched_sheet_name': None,
        'parse_result': None,
        'attempted_months': [],
        'errors_by_month': {},
        'error': error,
    }


def _sbi_monthly_links() -> list[dict[str, str]]:
    """SBI publishes the workbook links through the public portfolios page's POST feed."""
    months = []
    cursor = date.today().replace(day=1) - timedelta(days=1)
    for _ in range(PORTFOLIO_LOOKBACK_MONTHS):
        months.append(cursor)
        cursor = cursor.replace(day=1) - timedelta(days=1)

    def fetch_month(month_end: date) -> list[dict[str, str]]:
        response = requests.post(
            SBI_PORTFOLIO_LIST_URL,
            json={
                'FundId': 0,
                'PSYear': str(month_end.year),
                'PSMonth': month_end.strftime('%B'),
                'PSFrequency': 'Monthly',
            },
            headers={**HEADERS, 'Content-Type': 'application/json;charset=utf-8'},
            timeout=(3, 10),
        )
        response.raise_for_status()
        parser = _PortfolioLinks()
        parser.feed(response.text)
        found = {}
        for item in parser.links:
            url = urljoin('https://www.sbimf.com/portfolios', item['href'].strip())
            title = re.sub(r'\s+', ' ', item['text']).strip()
            if not _candidate_link('sbi_monthly_excel', title, url):
                continue
            month = _month_in_text(f'{title} {url}')
            if month != month_end.strftime('%Y-%m'):
                continue
            found[url] = {
                'url': url,
                'text': title,
                'month': month,
                'portfolio_date': month_end.isoformat(),
            }
        return list(found.values())

    links = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch_month, month) for month in months]
        for future in futures:
            try:
                links.extend(future.result())
            except (requests.RequestException, ValueError) as exc:
                logger.warning('SBI monthly portfolio listing unavailable: %s', exc)
    if not links:
        raise ProviderError('SBI monthly portfolio listing unavailable')
    return links


_EMBEDDED_EXCEL = re.compile(r'''["'(]((?:https?://|/)[^"'()\s<>]+?\.xlsx?)(?=["')\s?#])''', re.IGNORECASE)


def _embedded_excel_paths(page_text: str) -> list[str]:
    """Excel file paths written inside a page's scripts or data rather than in links."""
    return list(dict.fromkeys(_EMBEDDED_EXCEL.findall(page_text.replace('\\/', '/'))))


def _load_page_links(amc_key: str, page_url: str, parser_key: str) -> list[dict[str, str]]:
    cache_key = f'fund-analyzer:portfolio-page:{amc_key}'
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        logger.exception('Portfolio page cache read failed for %s', amc_key)

    try:
        recent_error = cache.get(f'{cache_key}:error')
        if recent_error:
            stale = cache.get(f'{cache_key}:stale')
            if stale:
                return stale
            raise ProviderError(recent_error)
    except ProviderError:
        raise
    except Exception:
        logger.exception('Portfolio page error cache read failed for %s', amc_key)

    try:
        if parser_key == 'sbi_monthly_excel':
            results = _sbi_monthly_links()
        else:
            response = fetch_url(page_url, timeout=(3, 15), attempts=1)
            parser = _PortfolioLinks()
            parser.feed(response.text)
            results = []
            domains = official_domains(amc_key)
            seen_urls = set()
            # Some AMC pages fill their file lists in with JavaScript from data inside the page,
            # so no <a> tag exists. Read the Excel paths in that data too, under the same rules.
            parser.links.extend({'href': path, 'text': unquote(path.rsplit('/', 1)[-1])} for path in _embedded_excel_paths(response.text))
            for link in parser.links:
                href = urljoin(page_url, (link['href'] or '').strip())
                text = re.sub(r'\s+', ' ', link['text']).strip()
                # Pages can repeat one file for its download and share buttons.
                if href in seen_urls or not _candidate_link(parser_key, text, href) or not is_official_url(href, domains):
                    continue
                month = _month_in_text(f'{text} {href}')
                if month:
                    seen_urls.add(href)
                    results.append({'url': href, 'text': text, 'month': month, 'portfolio_date': _portfolio_date(month)})
        if not results:
            raise ProviderError(f'No monthly portfolio files found for {amc_key}')
    except ProviderError as exc:
        try:
            cache.set(f'{cache_key}:error', str(exc), timeout=PAGE_ERROR_CACHE_SECONDS)
            stale = cache.get(f'{cache_key}:stale')
            if stale:
                logger.warning('Using cached monthly portfolio links after %s page failed', amc_key)
                return stale
        except Exception:
            logger.exception('Stale portfolio page cache read failed for %s', amc_key)
        raise
    try:
        cache.set(cache_key, results, timeout=PAGE_CACHE_SECONDS)
        cache.set(f'{cache_key}:stale', results, timeout=PARSED_HOLDINGS_CACHE_SECONDS)
        cache.delete(f'{cache_key}:error')
    except Exception:
        logger.exception('Portfolio page cache write failed for %s', amc_key)
    return results


def _monthly_files_for_fund(source: dict, fund_name: str) -> list[dict[str, str]]:
    adapter = adapter_for(source)
    try:
        links = _load_page_links(source['key'], source['url'], source['parser'])
    except ProviderError as exc:
        links = adapter.fallback_links(fund_name, list(_recent_portfolio_months()), exc)
        logger.warning('Listing failed for %s; trying its published file pattern: %s', source['key'], exc)
        return links
    return adapter.select_links(links, fund_name)


def _check_workbook_payload(payload: bytes) -> None:
    """Accept only real Excel files, and refuse any that would unpack to something huge."""
    if payload[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return
    if payload[:4] != b'PK\x03\x04':
        raise PortfolioError(INVALID_WORKBOOK, 'Monthly portfolio file is not an Excel workbook')
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            unpacked = sum(item.file_size for item in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise PortfolioError(INVALID_WORKBOOK, 'Monthly portfolio file is a damaged Excel workbook') from exc
    if unpacked > MAX_WORKBOOK_UNPACKED_BYTES:
        raise PortfolioError(INVALID_WORKBOOK, 'Monthly portfolio workbook is too large to read')


def _download_portfolio_file(amc_key: str, url: str) -> bytes:
    """Download a file found by an adapter, staying on the AMC's domain and within size limits."""
    domains = official_domains(amc_key)
    if not is_official_url(url, domains):
        raise PortfolioError(SOURCE_NOT_SUPPORTED, f'File is not on an official domain of {amc_key}')
    try:
        with requests.get(url, headers=HEADERS, timeout=(3, 30), stream=True) as response:
            if not is_official_url(response.url, domains):
                raise PortfolioError(DOWNLOAD_BLOCKED, f'File redirected away from an official domain of {amc_key}')
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=256 * 1024):
                size += len(chunk)
                if size > MAX_PORTFOLIO_FILE_BYTES:
                    raise PortfolioError(INVALID_WORKBOOK, 'Monthly portfolio file is larger than the allowed size')
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise ProviderError(str(exc)) from exc
    payload = b''.join(chunks)
    if not payload:
        raise ProviderError(f'Empty monthly portfolio file from {amc_key}')
    _check_workbook_payload(payload)
    return payload


def _cached_file(key: tuple[str, str], url: str) -> bytes | None:
    entry = _portfolio_file_cache.get(key)
    return entry[1][url] if entry and entry[0] > datetime.now() and url in entry[1] else None


def _download_cached(amc_key: str, month: str, url: str) -> bytes:
    """One download per file, however many requests ask at once.

    Each file has its own lock, so a second request for the same file waits for the first and
    reads its result, while downloads of different files (or AMCs) run side by side.
    """
    key = (amc_key, month)
    with _download_lock:
        hit = _cached_file(key, url)
        if hit is not None:
            return hit
        file_lock = _file_locks.setdefault((amc_key, month, url), Lock())
    with file_lock:
        with _download_lock:
            hit = _cached_file(key, url)
            if hit is not None:
                return hit
        payload = _download_portfolio_file(amc_key, url)
        with _download_lock:
            now = datetime.now()
            entry = _portfolio_file_cache.get(key)
            cached_files = entry[1] if entry and entry[0] > now else {}
            cached_files[url] = payload
            _portfolio_file_cache[key] = (now + timedelta(seconds=PORTFOLIO_CACHE_SECONDS), cached_files)
            # Bound memory use if a long-lived worker sees many AMC/month pairs.
            while len(_portfolio_file_cache) > 32:
                oldest = min(_portfolio_file_cache, key=lambda cache_key: _portfolio_file_cache[cache_key][0])
                del _portfolio_file_cache[oldest]
            while sum(len(file_bytes) for _, files in _portfolio_file_cache.values() for file_bytes in files.values()) > PORTFOLIO_CACHE_MAX_BYTES:
                oldest = min(_portfolio_file_cache, key=lambda cache_key: _portfolio_file_cache[cache_key][0])
                del _portfolio_file_cache[oldest]
        return payload


def _sheet_name_score(sheet_name: str, frame: pd.DataFrame, fund_name: str) -> int:
    has_holdings_header = any(
        'isin' in ' '.join(str(value).lower() for value in frame.iloc[row_index].tolist())
        for row_index in range(min(len(frame), 80))
    )
    if not has_holdings_header:
        return 0
    wanted = _normalise_name(fund_name)
    sheet_title = _normalise_name(sheet_name)
    if wanted and wanted in sheet_title:
        return 3
    # A sheet's title beats a mention lower down, such as one fund holding units of another.
    title = _normalise_name(' '.join(frame.iloc[:3, :12].fillna('').astype(str).to_numpy().ravel()))
    if wanted and wanted in title:
        return 2.5
    context = frame.iloc[:25, :12].fillna('').astype(str).to_numpy().ravel()
    candidate = _normalise_name(' '.join([sheet_name, *context]))
    if wanted and wanted in candidate:
        return 2
    if candidate and candidate in wanted and len(candidate) >= max(12, int(len(wanted) * 0.7)):
        return 1
    return 0


def _reporting_date_from_frame(frame: pd.DataFrame) -> str | None:
    patterns = (
        '%d %B %Y', '%d %b %Y', '%B %d %Y', '%b %d %Y',
        '%d %B %y', '%d %b %y', '%B %d %y', '%b %d %y',
    )
    for row in frame.iloc[:40].itertuples(index=False, name=None):
        values = [value for value in row if not pd.isna(value) and str(value).strip()]
        context = ' '.join(str(value) for value in values)
        if not re.search(r'(?i)(portfolio|statement|holdings|as\s+on|as\s+at|month\s+ended)', context):
            continue
        for value in values:
            if isinstance(value, (datetime, date)):
                return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
        for candidate in re.findall(
            r'(?i)\b(?:\d{1,2}[\s/-]+[A-Za-z]{3,9}[\s/-]+\d{2,4}|'
            r'[A-Za-z]{3,9}[\s/-]+\d{1,2},?[\s/-]*\d{2,4}|\d{4}-\d{2}-\d{2})\b',
            context,
        ):
            cleaned = re.sub(r'[,/-]', ' ', candidate).strip()
            if re.fullmatch(r'\d{4}\s+\d{2}\s+\d{2}', cleaned):
                cleaned = candidate
                try:
                    return date.fromisoformat(cleaned).isoformat()
                except ValueError:
                    continue
            for pattern in patterns:
                try:
                    return datetime.strptime(cleaned, pattern).date().isoformat()
                except ValueError:
                    continue
    return None


def _column_index(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for index, header in enumerate(headers):
        compact = re.sub(r'[^a-z0-9%]+', ' ', header.lower()).strip()
        if any(candidate in compact for candidate in candidates):
            return index
    return None


def _is_equity_section(text: str) -> bool | None:
    value = re.sub(r'[^a-z]+', ' ', text.lower())
    if re.search(r'\b(reits?|invits?|derivatives?)\b', value) or any(token in value for token in (
        'real estate investment trust', 'infrastructure investment trust', 'mutual fund units',
    )):
        return False
    if value.strip() in ('equity', 'equities'):
        return True
    if any(token in value for token in ('equity and equity related', 'equity equity related', 'equity related instruments', 'equity shares', 'equity instruments')):
        return True
    if any(token in value for token in ('debt instruments', 'money market instruments', 'government securities', 'cash and cash equivalents', 'other assets')):
        return False
    return None


def _section_for_row(row) -> bool | None:
    values = [str(value) for value in row if not pd.isna(value) and str(value).strip()]
    return _is_equity_section(' '.join(values)) if len(values) <= 3 else None


def _as_weight(value, number_format: str = '', upper: float = 100) -> float | None:
    try:
        if pd.isna(value):
            return None
        raw_value = str(value).strip()
        result = float(raw_value.replace(',', '').replace('%', '').strip())
        if '%' in number_format and '%' not in raw_value:
            result *= 100
        return result if 0 <= result <= upper else None
    except (TypeError, ValueError):
        return None


def _clean_holding_name(value: Any) -> str:
    """AMC files add footnote marks to names, such as 'HDFC Bank Ltd.£'."""
    if pd.isna(value):
        return ''
    return re.sub(r'[^\w.)]+$', '', re.sub(r'\s+', ' ', str(value)).strip())


def _name_and_industry_columns(frame: pd.DataFrame, header_at: int, asset_at: int | None) -> tuple[int | None, int | None]:
    headers = [re.sub(r'[^a-z0-9%]+', ' ', str(value).lower()).strip() if not pd.isna(value) else '' for value in frame.iloc[header_at].tolist()]
    name_at = _column_index(headers, ('name of',))
    if name_at is None:
        name_at = _column_index(headers, ('instrument', 'security', 'company', 'scrip'))
    return (None if name_at == asset_at else name_at), _column_index(headers, ('industry',))


def _benchmark_from_frame(frame: pd.DataFrame) -> str | None:
    """AMC files name the benchmark in a note, as 'Benchmark Name : X' or 'Portfolio Benchmark "X"'."""
    for row in frame.itertuples(index=False, name=None):
        for value in row:
            if pd.isna(value) or 'benchmark' not in str(value).lower():
                continue
            match = re.search(r'(?i)benchmark\s+name\s*[:\-–]\s*(.+)|portfolio\s+benchmark\s+"([^"]+)"', str(value))
            if match:
                return re.sub(r'\s+', ' ', match.group(1) or match.group(2)).strip()[:80]
    return None


def _workbook_sheets(payload: bytes, suffix: str) -> tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]]:
    if suffix.endswith('.csv'):
        for encoding in ('utf-8-sig', 'cp1252'):
            try:
                frame = pd.read_csv(BytesIO(payload), header=None, dtype=object, encoding=encoding)
                return {'Portfolio': frame}, {'Portfolio': []}
            except UnicodeDecodeError:
                continue
        raise ProviderError('Portfolio CSV encoding was not recognised')
    _check_workbook_payload(payload)
    # Some AMCs publish an OOXML workbook with a legacy .xls filename.
    if suffix.endswith('.xls') and not payload.startswith(b'PK\x03\x04'):
        import xlrd

        workbook = xlrd.open_workbook(file_contents=payload, formatting_info=True)
        frames = {}
        formats = {}
        for sheet in workbook.sheets():
            frames[sheet.name] = pd.DataFrame([sheet.row_values(row) for row in range(sheet.nrows)])
            cell_formats = []
            for row in range(sheet.nrows):
                format_row = []
                for column in range(sheet.ncols):
                    xf = workbook.xf_list[sheet.cell_xf_index(row, column)]
                    fmt = workbook.format_map.get(xf.format_key)
                    format_row.append(fmt.format_str if fmt else '')
                cell_formats.append(format_row)
            formats[sheet.name] = cell_formats
        return frames, formats

    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
    frames = {}
    formats = {}
    for sheet in workbook.worksheets:
        frames[sheet.title] = pd.DataFrame(list(sheet.values))
        formats[sheet.title] = [[cell.number_format or '' for cell in row] for row in sheet.iter_rows()]
    workbook.close()
    return frames, formats


def _locate_holdings_table(
    payload: bytes,
    url: str,
    fund_name: str,
    diagnostics: dict[str, Any] | None,
    workbook: tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]] | None,
) -> dict[str, Any]:
    """Find this scheme's sheet, its holdings table and how weights are given."""
    suffix = urlsplit(url).path.lower()
    try:
        sheets, formats_by_sheet = workbook if workbook is not None else _workbook_sheets(payload, suffix)
    except Exception as exc:
        raise ProviderError('Monthly portfolio workbook could not be parsed') from exc
    if not sheets:
        raise ProviderError('Monthly portfolio workbook contains no sheets')

    ranked = sorted((( _sheet_name_score(name, frame, fund_name), name, frame) for name, frame in sheets.items()), key=lambda row: row[0], reverse=True)
    if not ranked or ranked[0][0] == 0:
        # A file name proves nothing. The workbook itself must name this scheme.
        raise ProviderError(f'No sheet matched fund {fund_name}')
    _, sheet_name, frame = ranked[0]
    if diagnostics is not None:
        diagnostics['matched_sheet_name'] = sheet_name
        diagnostics['reporting_date'] = _reporting_date_from_frame(frame)

    header_candidates = []
    for row_index in range(min(len(frame), 80)):
        row_headers = [str(value).strip() if not pd.isna(value) else '' for value in frame.iloc[row_index].tolist()]
        normalised = [re.sub(r'[^a-z0-9%]+', ' ', value.lower()).strip() for value in row_headers]
        candidate_isin = _column_index(normalised, ('isin',))
        candidate_weight = _column_index(normalised, ('% to nav', 'percent to nav', '% of net assets', 'percentage to nav', 'percentage of net assets', '% to aum', 'percentage to aum', '% of aum'))
        candidate_market_value = _column_index(normalised, ('market value', 'market value rs', 'value rs'))
        if candidate_isin is not None and (candidate_weight is not None or candidate_market_value is not None):
            header_candidates.append((row_index, candidate_isin, candidate_weight, _column_index(normalised, ('asset class', 'asset type', 'nature of instrument', 'instrument type', 'classification')), candidate_market_value))
    if not header_candidates:
        raise ProviderError(f'ISIN and portfolio weight/value columns were not found for {fund_name}')

    wanted_name = _normalise_name(fund_name)
    named_headers = []
    for header in header_candidates:
        header_row = header[0]
        context_rows = frame.iloc[max(0, header_row - 20):header_row, :12].fillna('').astype(str).to_numpy().ravel()
        context = _normalise_name(' '.join(context_rows))
        if wanted_name and wanted_name in context:
            named_headers.append(header)
    if named_headers:
        header_at, isin_at, weight_at, asset_at, market_value_at = named_headers[-1]
    elif len(header_candidates) == 1 and _sheet_name_score(sheet_name, frame, fund_name) > 0:
        header_at, isin_at, weight_at, asset_at, market_value_at = header_candidates[0]
    else:
        raise ProviderError(f'No holdings section matched fund {fund_name}')
    section_end = next((header[0] for header in header_candidates if header[0] > header_at), len(frame))

    total_fund_value = None
    if weight_at is None and market_value_at is not None:
        for _, total_row in frame.iterrows():
            label = ' '.join(str(value) for value in total_row.iloc[:min(len(total_row), 6)].tolist()).lower()
            if not any(marker in label for marker in ('net assets', 'grand total', 'total assets', 'total portfolio value')):
                continue
            if market_value_at >= len(total_row) or pd.isna(total_row.iloc[market_value_at]):
                continue
            try:
                value = float(str(total_row.iloc[market_value_at]).replace(',', '').strip())
                if value > 0:
                    total_fund_value = value
            except (TypeError, ValueError):
                continue
        if total_fund_value is None:
            raise ProviderError(f'Total fund value was not found for {fund_name}')
    return {
        'sheet_name': sheet_name, 'frame': frame, 'cell_formats': formats_by_sheet.get(sheet_name, []),
        'header_at': header_at, 'isin_at': isin_at, 'weight_at': weight_at, 'asset_at': asset_at,
        'market_value_at': market_value_at, 'section_end': section_end, 'total_fund_value': total_fund_value,
    }


def _row_weight(table: dict[str, Any], row: list, row_index: int, upper: float = 100) -> float | None:
    """A holding's weight as a percentage of the whole scheme, not of one asset class.

    upper is the largest weight accepted. A Fund of Funds holding one fund can show a little
    over 100% when its net current assets are negative, so its reader raises the limit.
    """
    weight_at, market_value_at = table['weight_at'], table['market_value_at']
    if weight_at is not None:
        cell_formats = table['cell_formats']
        weight_format = cell_formats[row_index][weight_at] if row_index < len(cell_formats) and weight_at < len(cell_formats[row_index]) else ''
        return _as_weight(row[weight_at], weight_format, upper)
    try:
        market_value = float(str(row[market_value_at]).replace(',', '').strip())
        return market_value / table['total_fund_value'] * 100 if market_value >= 0 else None
    except (TypeError, ValueError):
        return None


def _add_file_details(diagnostics: dict[str, Any] | None, payload: bytes, table: dict[str, Any], fund_name: str, holdings: dict[str, float]) -> None:
    if diagnostics is None or diagnostics.get('_skip_file_details'):
        return
    diagnostics['parse_result'] = {'status': 'ok', 'holding_count': len(holdings)}
    diagnostics['benchmark'] = _benchmark_from_frame(table['frame'])
    try:
        diagnostics['risk_level'] = read_scheme_riskometer(payload, table['sheet_name'], table['frame'])
    except Exception:
        # The riskometer is optional. It must never stop the holdings being used.
        logger.exception('Riskometer read failed for %s', fund_name)
        diagnostics['risk_level'] = None


def _read_equity_holdings(
    payload: bytes,
    url: str,
    fund_name: str,
    parser_key: str,
    diagnostics: dict[str, Any] | None = None,
    workbook: tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]] | None = None,
) -> dict[str, float]:
    table = _locate_holdings_table(payload, url, fund_name, diagnostics, workbook)
    frame, header_at, section_end = table['frame'], table['header_at'], table['section_end']
    isin_at, weight_at, asset_at, market_value_at = table['isin_at'], table['weight_at'], table['asset_at'], table['market_value_at']

    equity_context: bool | None = None
    for row_index in range(header_at):
        section = _section_for_row(frame.iloc[row_index].tolist())
        if section is not None:
            equity_context = section

    holdings: dict[str, float] = {}
    names: dict[str, str] = {}
    industries: dict[str, str] = {}
    name_at, industry_at = _name_and_industry_columns(frame, header_at, asset_at)
    saw_equity_classification = False
    for row_index in range(header_at + 1, section_end):
        row = frame.iloc[row_index].tolist()
        section = _section_for_row(row)
        if section is not None:
            equity_context = section
            continue

        is_equity = equity_context is True
        if asset_at is not None and asset_at < len(row) and not pd.isna(row[asset_at]):
            classification = str(row[asset_at]).strip().lower()
            if classification:
                saw_equity_classification = True
                is_equity = 'equity' in classification and 'non equity' not in classification
        if not is_equity:
            continue
        if isin_at >= len(row) or (weight_at is not None and weight_at >= len(row)) or (weight_at is None and (market_value_at is None or market_value_at >= len(row))):
            continue
        isin_value = '' if pd.isna(row[isin_at]) else str(row[isin_at])
        isin = re.sub(r'\s+', '', isin_value).upper()
        weight = _row_weight(table, row, row_index)
        if not ISIN_PATTERN.match(isin) or weight is None:
            continue
        holdings[isin] = holdings.get(isin, 0.0) + weight
        if name_at is not None and name_at < len(row):
            names.setdefault(isin, _clean_holding_name(row[name_at]))
        if industry_at is not None and industry_at < len(row):
            industries.setdefault(isin, _clean_holding_name(row[industry_at]))

    if not holdings:
        classification_note = ' Equity rows could not be identified.' if not saw_equity_classification and equity_context is None else ''
        raise _NoHoldingsFound(f'No ISIN equity holdings found for {fund_name}.{classification_note}')
    if sum(holdings.values()) > 101:
        raise ProviderError(f'Equity holdings weights exceed 100% for {fund_name}')
    if diagnostics is not None:
        diagnostics['holding_names'] = names
        diagnostics['holding_industries'] = industries
    _add_file_details(diagnostics, payload, table, fund_name, holdings)
    return holdings


# AMC files give debt instrument types as heading rows above each block, in their own words.
# These blocks are not direct debt securities. They stay visible but never count in the overlap.
ALTERNATIVE_FUND_UNITS = 'Alternative Investment Fund Units'
MUTUAL_FUND_UNITS = 'Mutual Fund Units / ETFs'
REPO = 'TREPS / Repo'
NET_CURRENT_ASSETS = 'Net Current Assets'
CASH_AND_MARGIN = 'Cash & Margin'
_OTHER_INSTRUMENT_TYPES = (
    (r'alternative investment|corporate debt market', ALTERNATIVE_FUND_UNITS),
    (r'foreign securities', 'Foreign Securities'),
    (r'mutual funds?\b|exchange traded|\betfs?\b', MUTUAL_FUND_UNITS),
    (r'\breits?\b|\binvits?\b|real estate investment trust|infrastructure investment trust', 'REIT / InvIT Units'),
    (r'term deposit', 'Term Deposits'),
    (r'margin|cash', CASH_AND_MARGIN),
    (r'derivative', 'Derivatives'),
    (r'treps|repo\b', REPO),
    (r'net current|net receivable|current assets', NET_CURRENT_ASSETS),
    (r'equity', 'Equity'),
)
_FUND_UNIT_TYPES = (ALTERNATIVE_FUND_UNITS, MUTUAL_FUND_UNITS)
# Balances, not investments. They appear in the allocation only, never as holdings.
_BALANCE_TYPES = (NET_CURRENT_ASSETS, CASH_AND_MARGIN)
# India's ISIN numbering gives mutual fund units the prefix INF, so no heading can make one a bond.
FUND_UNIT_ISIN_PREFIX = 'INF'
# Stated weights are rounded to 2 places, so a correct one is at most 0.005 from value / total.
MAX_WEIGHT_DIFFERENCE = 0.02
MAX_TOTAL_DIFFERENCE = 1
_DEBT_INSTRUMENT_TYPES = (
    (r'state government|\bsdl\b', 'State Government Securities'),
    (r'government securit|central government|\bg-?sec|\bgilt\b', 'Government Securities'),
    (r'zero coupon|deep discount', 'Zero Coupon Bonds'),
    (r'securiti[sz]ed|pass through|\bptc\b', 'Securitised Debt'),
    (r'certificate of deposit', 'Certificate of Deposit'),
    (r'commercial paper', 'Commercial Paper'),
    (r'treasury bill|t-?bill', 'Treasury Bills'),
    (r'\bstrips\b', 'Government Securities STRIPS'),
    (r're-?discount', 'Bills Rediscounting'),
    (r'preference shares', 'Non-Convertible Preference Shares'),
    (r'non[\s-]*convertible|debenture|\bncds?\b|\bbonds?\b|perpetual', 'Non-Convertible Debentures / Bonds'),
)
_DEBT_BLOCK_HEADINGS = re.compile(r'debt instruments|money market')
_TOTAL_ROW = re.compile(r'^(sub\s*-?\s*total|total|grand total)\b')
_GRAND_TOTAL_ROW = re.compile(r'^grand\s+total\b')


def _other_instrument_type(text: str) -> str | None:
    value = text.lower()
    return next((label for pattern, label in _OTHER_INSTRUMENT_TYPES if re.search(pattern, value)), None)


def _debt_heading(text: str) -> tuple[bool, str | None] | None:
    """Read a heading row as (holds debt securities, instrument type). None if it isn't one."""
    value = re.sub(r'^\(?[a-z0-9]\)\s*', '', re.sub(r'\s+', ' ', text).strip().lower())
    if not value or _TOTAL_ROW.match(value):
        return None
    other_type = _other_instrument_type(value)
    if other_type or value in ('other', 'others'):
        return False, other_type
    for pattern, label in _DEBT_INSTRUMENT_TYPES:
        if re.search(pattern, value):
            return True, label
    return (True, None) if _DEBT_BLOCK_HEADINGS.search(value) else None


def _debt_instrument_type(section_type: str | None, name: str, rating: str) -> str:
    """Use the file's own heading. Some files list several types under one heading."""
    if section_type:
        return section_type
    for pattern, label in _DEBT_INSTRUMENT_TYPES:
        if re.search(pattern, name.lower()):
            return label
    if rating.lower() == 'sovereign' or re.search(r'\b(goi|cgl)\b', name.lower()):
        return 'Government Securities'
    return 'Debt Instrument'


def _debt_issuer(name: str, rating: str, isin: str) -> str | None:
    """The issuer, where the instrument's name shows it. Different issues stay separate by ISIN."""
    lowered = name.lower()
    state = re.search(r'(?i)state government of ([a-z ]+?)(?:\s+\d|$)|([a-z ]+?)\s+sdl\b', re.sub(r'^\s*[\d.]+%\s*', '', name))
    if state:
        return f'State Government of {(state.group(1) or state.group(2)).strip().title()}'
    if isin.startswith('IN00') or re.search(r'\b(goi|cgl|t-?bill|tbill|government of india)\b', lowered):
        return 'Government of India'
    if rating.lower() == 'sovereign':
        return None
    issuer = re.sub(r'^\s*[\d.]+%\s*', '', name)
    issuer = re.split(r'(?i)\s*\(|\s+(?:ncd|ser\b|series\b|tier\b|mat\b|md\b)', issuer)[0].strip(' -')
    return issuer or None


def _signed_row_weight(table: dict[str, Any], row: list, row_index: int, upper: float = 100) -> float | None:
    """Like _row_weight, but keeps a minus sign. Net current assets can be negative."""
    at = table['weight_at'] if table['weight_at'] is not None else table['market_value_at']
    if at >= len(row):
        return None
    raw = '' if pd.isna(row[at]) else str(row[at]).strip()
    if not raw.startswith('-'):
        return _row_weight(table, row, row_index, upper)
    positive = [*row]
    positive[at] = raw[1:]
    weight = _row_weight(table, positive, row_index, upper)
    return -weight if weight is not None else None


def _market_value(table: dict[str, Any], row: list) -> float | None:
    at = table['market_value_at']
    if at is None or at >= len(row) or pd.isna(row[at]):
        return None
    try:
        return float(str(row[at]).replace(',', '').strip())
    except ValueError:
        return None


def _read_debt_holdings(
    payload: bytes,
    url: str,
    fund_name: str,
    parser_key: str,
    diagnostics: dict[str, Any] | None = None,
    workbook: tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]] | None = None,
) -> dict[str, float]:
    """Read direct debt securities, each as a percentage of net assets, matched only by ISIN.

    Fund units, repo, cash and net current assets go in other_holdings: they stay
    visible, but overlap never uses them.
    """
    table = _locate_holdings_table(payload, url, fund_name, diagnostics, workbook)
    frame, isin_at, asset_at = table['frame'], table['isin_at'], table['asset_at']
    name_at, _ = _name_and_industry_columns(frame, table['header_at'], asset_at)
    headers = [re.sub(r'[^a-z0-9%]+', ' ', str(value).lower()).strip() if not pd.isna(value) else '' for value in frame.iloc[table['header_at']].tolist()]
    rating_at = _column_index(headers, ('rating', 'industry'))

    holdings: dict[str, float] = {}
    names: dict[str, str] = {}
    ratings: dict[str, str] = {}
    instrument_types: dict[str, str] = {}
    issuers: dict[str, str] = {}
    others: dict[str, dict[str, Any]] = {}
    stated: list[tuple[float, float]] = []
    grand_total: float | None = None
    in_debt = False
    section_type: str | None = None

    def add_other(isin: str | None, name: str, weight: float, instrument_type: str) -> None:
        entry = others.setdefault(isin or f'{instrument_type}|{name}', {
            **({'isin': isin} if isin else {}), 'name': name, 'weight': 0.0, 'instrument_type': instrument_type,
        })
        entry['weight'] += weight

    for row_index in range(table['header_at'] + 1, table['section_end']):
        row = frame.iloc[row_index].tolist()
        values = [str(value).strip() for value in row if not pd.isna(value) and str(value).strip()]
        if values and _GRAND_TOTAL_ROW.match(values[0].lower()):
            # What follows is notes and summaries, which repeat the labels above.
            grand_total = _market_value(table, row)
            break
        isin = re.sub(r'\s+', '', '' if isin_at >= len(row) or pd.isna(row[isin_at]) else str(row[isin_at])).upper()
        name = _clean_holding_name(row[name_at]) if name_at is not None and name_at < len(row) else ''
        if not ISIN_PATTERN.match(isin):
            heading = _debt_heading(values[0]) if values and len(values) <= 4 else None
            if heading is not None:
                in_debt, new_type = heading
                # "Debt Instruments" opens a block. The types inside it come next.
                section_type = new_type
            if not in_debt and values and not _TOTAL_ROW.match(values[0].lower()):
                weight = _signed_row_weight(table, row, row_index)
                if weight is not None:
                    add_other(None, name or values[0], weight, _other_instrument_type(' '.join(values)) or section_type or 'Other')
            continue
        is_debt, row_type = in_debt, section_type
        if asset_at is not None and asset_at < len(row) and not pd.isna(row[asset_at]) and str(row[asset_at]).strip():
            classified = _debt_heading(str(row[asset_at]))
            is_debt, row_type = bool(classified and classified[0]), classified[1] if classified else None
        if table['weight_at'] is not None and table['weight_at'] >= len(row):
            continue
        if table['weight_at'] is None and table['market_value_at'] >= len(row):
            continue
        weight = _row_weight(table, row, row_index)
        if weight is None:
            continue
        market_value = _market_value(table, row)
        if table['weight_at'] is not None and market_value is not None:
            stated.append((weight, market_value))
        is_fund_unit = isin.startswith(FUND_UNIT_ISIN_PREFIX)
        if is_fund_unit or not is_debt:
            other_type = _other_instrument_type(name) or row_type or 'Other'
            if is_fund_unit and other_type not in _FUND_UNIT_TYPES:
                other_type = MUTUAL_FUND_UNITS
            # A repo row's "ISIN" is the AMC's own deal code, such as TREP01102026.
            add_other(None if other_type == REPO else isin, name or isin, weight, other_type)
            continue
        holdings[isin] = holdings.get(isin, 0.0) + weight
        # Not the name cleaner: its footnote removal would turn AA+ into AA.
        rating = re.sub(r'\s+', ' ', str(row[rating_at])).strip() if rating_at is not None and rating_at < len(row) and not pd.isna(row[rating_at]) else ''
        names.setdefault(isin, name)
        ratings.setdefault(isin, rating)
        instrument_types.setdefault(isin, _debt_instrument_type(section_type, name, rating))
        issuer = _debt_issuer(name, rating, isin)
        if issuer:
            issuers.setdefault(isin, issuer)

    if not holdings:
        raise _NoHoldingsFound(f'No ISIN debt holdings found for {fund_name}')
    # Debt schemes can hold slightly over 100% when net current assets are negative.
    if sum(holdings.values()) > MAX_DEBT_WEIGHT:
        raise ProviderError(f'Debt holdings weights are too high for {fund_name}')
    # % to NAV and % to AUM are only comparable if both are a share of the same net assets.
    if grand_total and grand_total > 0 and stated:
        worst = max(abs(weight - value / grand_total * 100) for weight, value in stated)
        if worst > MAX_WEIGHT_DIFFERENCE:
            raise ProviderError(f'Debt weights for {fund_name} do not match market value over grand total')
    other_rows = list(others.values())
    total = sum(holdings.values()) + sum(entry['weight'] for entry in other_rows)
    if abs(total - 100) > MAX_TOTAL_DIFFERENCE:
        logger.warning('Debt portfolio rows for %s add up to %.2f%%, not 100%%', fund_name, total)
    if diagnostics is not None:
        diagnostics['holding_names'] = names
        diagnostics['holding_industries'] = ratings
        diagnostics['instrument_types'] = instrument_types
        diagnostics['issuers'] = issuers
        diagnostics['other_holdings'] = other_rows
    _add_file_details(diagnostics, payload, table, fund_name, holdings)
    return holdings


def _read_fund_unit_holdings(
    payload: bytes,
    url: str,
    fund_name: str,
    parser_key: str,
    diagnostics: dict[str, Any] | None = None,
    workbook: tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]] | None = None,
) -> dict[str, float]:
    """Read the mutual fund units a Fund of Funds holds, each as a percentage of its NAV.

    Only rows that are verifiably mutual fund / ETF units count: an INF ISIN (India gives
    every fund unit that prefix) or a row the file itself places under a mutual fund
    heading or asset class. Alternative investment funds such as CDMDF are not scheme
    choices, so they stay visible but are never matched. Repo, cash and net current
    assets are kept as other holdings, so allocation can add up.
    """
    table = _locate_holdings_table(payload, url, fund_name, diagnostics, workbook)
    frame, isin_at, asset_at = table['frame'], table['isin_at'], table['asset_at']
    name_at, _ = _name_and_industry_columns(frame, table['header_at'], asset_at)

    holdings: dict[str, float] = {}
    names: dict[str, str] = {}
    instrument_types: dict[str, str] = {}
    others: dict[str, dict[str, Any]] = {}
    stated: list[tuple[float, float]] = []
    grand_total: float | None = None
    section_type: str | None = None

    def add_other(isin: str | None, name: str, weight: float, instrument_type: str) -> None:
        entry = others.setdefault(isin or f'{instrument_type}|{name}', {
            **({'isin': isin} if isin else {}), 'name': name, 'weight': 0.0, 'instrument_type': instrument_type,
        })
        entry['weight'] += weight

    for row_index in range(table['header_at'] + 1, table['section_end']):
        row = frame.iloc[row_index].tolist()
        values = [str(value).strip() for value in row if not pd.isna(value) and str(value).strip()]
        if values and _GRAND_TOTAL_ROW.match(values[0].lower()):
            # What follows is notes and summaries, which repeat the labels above.
            grand_total = _market_value(table, row)
            break
        isin = re.sub(r'\s+', '', '' if isin_at >= len(row) or pd.isna(row[isin_at]) else str(row[isin_at])).upper()
        name = _clean_holding_name(row[name_at]) if name_at is not None and name_at < len(row) else ''
        if not ISIN_PATTERN.match(isin):
            heading = _debt_heading(values[0]) if values and len(values) <= 4 else None
            if heading is not None:
                section_type = heading[1]
            if values and not _TOTAL_ROW.match(values[0].lower()):
                weight = _signed_row_weight(table, row, row_index, MAX_DEBT_WEIGHT)
                if weight is not None:
                    add_other(None, name or values[0], weight, _other_instrument_type(' '.join(values)) or section_type or 'Other')
            continue
        if table['weight_at'] is not None and table['weight_at'] >= len(row):
            continue
        if table['weight_at'] is None and table['market_value_at'] >= len(row):
            continue
        weight = _row_weight(table, row, row_index, MAX_DEBT_WEIGHT)
        if weight is None:
            continue
        row_type = section_type
        if asset_at is not None and asset_at < len(row) and not pd.isna(row[asset_at]) and str(row[asset_at]).strip():
            row_type = _other_instrument_type(str(row[asset_at])) or row_type
        row_type = _other_instrument_type(name) if row_type is None else row_type
        if row_type == ALTERNATIVE_FUND_UNITS:
            add_other(isin, name or isin, weight, ALTERNATIVE_FUND_UNITS)
            continue
        if isin.startswith(FUND_UNIT_ISIN_PREFIX) or row_type == MUTUAL_FUND_UNITS:
            market_value = _market_value(table, row)
            if table['weight_at'] is not None and market_value is not None:
                stated.append((weight, market_value))
            holdings[isin] = holdings.get(isin, 0.0) + weight
            names.setdefault(isin, name or isin)
            instrument_types[isin] = MUTUAL_FUND_UNITS
            continue
        add_other(isin, name or isin, weight, row_type or 'Other')

    if not holdings:
        raise _NoHoldingsFound(f'No mutual fund unit holdings found for {fund_name}')
    if sum(holdings.values()) > MAX_DEBT_WEIGHT:
        raise ProviderError(f'Fund unit weights are too high for {fund_name}')
    # % to NAV must be a share of the same net assets the market values add up to.
    if grand_total and grand_total > 0 and stated:
        worst = max(abs(weight - value / grand_total * 100) for weight, value in stated)
        if worst > MAX_WEIGHT_DIFFERENCE:
            raise ProviderError(f'Fund unit weights for {fund_name} do not match market value over grand total')
    other_rows = list(others.values())
    total = sum(holdings.values()) + sum(entry['weight'] for entry in other_rows)
    if abs(total - 100) > MAX_TOTAL_DIFFERENCE:
        logger.warning('Fund of Funds portfolio rows for %s add up to %.2f%%, not 100%%', fund_name, total)
    if diagnostics is not None:
        diagnostics['holding_names'] = names
        diagnostics['holding_industries'] = {}
        diagnostics['instrument_types'] = instrument_types
        diagnostics['issuers'] = {}
        diagnostics['other_holdings'] = other_rows
        diagnostics['security_classes'] = {isin: ASSET_FUND_UNIT for isin in holdings}
        diagnostics['classes_read'] = [ASSET_FUND_UNIT]
        diagnostics['unclassified_weight'] = sum(row['weight'] for row in other_rows if row['instrument_type'] == 'Other')
    _add_file_details(diagnostics, payload, table, fund_name, holdings)
    return holdings


def _asset_class(record: dict[str, Any] | None) -> str:
    if record and _is_fof_fund(record):
        return ASSET_FOF
    if record and _is_debt_fund(record):
        return ASSET_DEBT
    return ASSET_HYBRID if record and _is_hybrid_fund(record) else ASSET_EQUITY


def _read_portfolio(asset_class: str, payload: bytes, url: str, fund_name: str, parser_key: str, diagnostics: dict[str, Any] | None = None, workbook: Any = None) -> dict[str, float]:
    """Read every eligible security and label each one equity or debt, whatever the fund type.

    The fund's main kind must be readable. For an equity or debt fund the other kind is
    a bonus: if it can't be read, classes_read says so and the file is still used.
    """
    details = diagnostics if diagnostics is not None else {}
    order = (ASSET_DEBT, ASSET_EQUITY) if asset_class == ASSET_DEBT else (ASSET_EQUITY, ASSET_DEBT)
    readers = {ASSET_EQUITY: _read_equity_holdings, ASSET_DEBT: _read_debt_holdings}
    merged_keys = ('holding_names', 'holding_industries', 'instrument_types', 'issuers')
    holdings: dict[str, float] = {}
    security_classes: dict[str, str] = {}
    classes_read: list[str] = []
    other_rows: list[dict[str, Any]] = []
    merged: dict[str, dict[str, str]] = {key: {} for key in merged_keys}
    for index, security_class in enumerate(order):
        part: dict[str, Any] = {'_skip_file_details': True} if holdings else {}
        try:
            rows = readers[security_class](payload, url, fund_name, parser_key, part, workbook)
        except _NoHoldingsFound:
            if index == 0 and asset_class != ASSET_HYBRID:
                raise
            classes_read.append(security_class)
            rows = {}
        except Exception as exc:
            if index == 0 or asset_class == ASSET_HYBRID:
                raise
            logger.info('%s rows not read for %s: %s', security_class, fund_name, exc)
            continue
        else:
            # The main kind decides any row both readers claim, so its results never change.
            rows = {isin: weight for isin, weight in rows.items() if isin not in holdings}
            if sum(holdings.values()) + sum(rows.values()) > MAX_DEBT_WEIGHT:
                if asset_class == ASSET_HYBRID:
                    raise ProviderError(f'Holdings weights are too high for {fund_name}')
                continue
            classes_read.append(security_class)
        holdings.update(rows)
        security_classes.update({isin: security_class for isin in rows})
        for key in merged_keys:
            merged[key].update({isin: value for isin, value in (part.get(key) or {}).items() if isin in rows})
        other_rows.extend(part.get('other_holdings') or [])
        for key, value in part.items():
            if key not in (*merged_keys, 'other_holdings', '_skip_file_details') and (index == 0 or details.get(key) is None):
                details[key] = value
    if not holdings:
        raise _NoHoldingsFound(f'No ISIN holdings found for {fund_name}')
    # A row in an equity block that the equity reader didn't take is reported, never dropped.
    other_rows = [{**row, 'instrument_type': 'Other' if row['instrument_type'] == 'Equity' else row['instrument_type']} for row in other_rows if row.get('isin') not in holdings]
    unclassified = None
    if len(classes_read) == 2:
        # Rows nobody could classify, or a file whose rows don't reach its own total.
        known_total = sum(holdings.values()) + sum(row['weight'] for row in other_rows)
        has_total = any(row['instrument_type'] == NET_CURRENT_ASSETS for row in other_rows)
        unclassified = max(sum(row['weight'] for row in other_rows if row['instrument_type'] == 'Other'), abs(100 - known_total) if has_total else 0.0)
    details.update(merged)
    details.update({'security_classes': security_classes, 'classes_read': classes_read, 'other_holdings': other_rows, 'unclassified_weight': unclassified})
    return holdings


def _read_holdings(record: dict[str, Any] | None):
    asset_class = _asset_class(record)
    if asset_class == ASSET_FOF:
        return _read_fund_unit_holdings

    def read(payload, url, fund_name, parser_key, diagnostics=None, workbook=None):
        return _read_portfolio(asset_class, payload, url, fund_name, parser_key, diagnostics, workbook)

    return read


def _security_class(data: dict[str, Any], isin: str) -> str:
    """Files cached before securities were labelled hold one kind only."""
    return (data.get('security_classes') or {}).get(isin) or (data.get('classes_read') or [ASSET_EQUITY])[0]


def _weights_of(data: dict[str, Any], classes: set[str]) -> dict[str, float]:
    return {isin: weight for isin, weight in data['holdings'].items() if _security_class(data, isin) in classes}


def _eligible_classes(left_type: str, right_type: str) -> set[str]:
    """What both fund types are compared on, or every eligible security if they share nothing."""
    return (FUND_SECURITY_CLASSES[left_type] & FUND_SECURITY_CLASSES[right_type]) or {ASSET_EQUITY, ASSET_DEBT}


def _pair_overlap(left: dict[str, Any], right: dict[str, Any], classes: set[str]) -> dict[str, Any]:
    """The same formula for every pair. A kind that wasn't compared is None, not zero."""
    left_weights, right_weights = _weights_of(left, classes), _weights_of(right, classes)
    common = _common_holdings({**left, 'holdings': left_weights}, {**right, 'holdings': right_weights})
    overlap = calculate_overlap(left_weights, right_weights)

    def part(security_class: str) -> tuple[float | None, int | None]:
        if security_class not in classes:
            return None, None
        rows = {row['isin'] for row in common if row['security_class'] == security_class}
        return round(sum(min(left_weights[isin], right_weights[isin]) for isin in rows), 2), len(rows)

    equity_overlap, equity_count = part(ASSET_EQUITY)
    debt_overlap, debt_count = part(ASSET_DEBT)
    return {
        'overlap_type': OVERLAP_SECURITIES,
        'overlap_pct': overlap,
        'common_holdings': common,
        'equity_overlap_pct': equity_overlap,
        'debt_overlap_pct': debt_overlap,
        'common_equity_count': equity_count,
        'common_debt_count': debt_count,
        'common_count': len(common),
        'message': None if common else REASON_MESSAGES[REASON_NO_COMMON_SECURITIES],
    }


def calculate_overlap(holdings_a: dict[str, float], holdings_b: dict[str, float]) -> float:
    return sum(min(holdings_a[isin], holdings_b[isin]) for isin in holdings_a.keys() & holdings_b.keys())


def _common_holdings(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    """The stocks behind calculate_overlap, largest contribution first."""
    left_weights, right_weights = left['holdings'], right['holdings']
    left_names, right_names = left.get('names') or {}, right.get('names') or {}
    common = sorted(left_weights.keys() & right_weights.keys(), key=lambda isin: (-min(left_weights[isin], right_weights[isin]), isin))
    return [{
        'isin': isin,
        'name': left_names.get(isin) or right_names.get(isin) or isin,
        'left_weight': round(left_weights[isin], 2),
        'right_weight': round(right_weights[isin], 2),
        'overlap_weight': round(min(left_weights[isin], right_weights[isin]), 2),
        'security_class': _security_class(left, isin),
    } for isin in common]


def _share_class_identities(records: list[dict[str, Any]]) -> dict[str, str]:
    """ISIN -> the AMFI scheme code it belongs to, only where AMFI itself ties two ISINs together.

    AMFI lists the growth/payout ISIN and the reinvestment ISIN of one option against one
    scheme code, which is official proof they are the same share class. Nothing else is
    merged: Direct and Regular plans have their own scheme codes, and a similar name proves
    nothing. An ISIN not in this map is matched only to an identical ISIN.
    """
    identities: dict[str, str] = {}
    for record in records:
        isins = {str(isin).strip().upper() for isin in (record.get('isin'), record.get('isin_reinvest')) if isin}
        if len(isins) == 2 and record.get('scheme_code'):
            identities.update({isin: f"AMFI:{record['scheme_code']}" for isin in isins})
    return identities


def _fund_unit_weights(data: dict[str, Any], identities: dict[str, str]) -> dict[str, dict[str, Any]]:
    """One entry per underlying fund. Rows repeating a unit, or holding both ISINs of one share class, add up."""
    weights, names = _weights_of(data, {ASSET_FUND_UNIT}), data.get('names') or {}
    units: dict[str, dict[str, Any]] = {}
    for isin in sorted(weights):
        entry = units.setdefault(identities.get(isin, isin), {'isins': [], 'name': names.get(isin) or isin, 'weight': 0.0})
        entry['isins'].append(isin)
        entry['weight'] += weights[isin]
    return units


def _fof_pair_overlap(left: dict[str, Any], right: dict[str, Any], identities: dict[str, str]) -> dict[str, Any]:
    """Direct FoF overlap: sum(min(weight_a, weight_b)) over underlying funds both hold.

    This is the overlap of the fund units themselves. It says nothing about the stocks or
    bonds inside those funds, and it is never mixed into an equity or debt figure.
    """
    left_units, right_units = _fund_unit_weights(left, identities), _fund_unit_weights(right, identities)
    common = sorted(left_units.keys() & right_units.keys(), key=lambda key: (-min(left_units[key]['weight'], right_units[key]['weight']), key))
    rows = [{
        'isin': left_units[key]['isins'][0],
        **({'right_isin': right_units[key]['isins'][0]} if right_units[key]['isins'][0] != left_units[key]['isins'][0] else {}),
        'name': left_units[key]['name'],
        'left_weight': round(left_units[key]['weight'], 2),
        'right_weight': round(right_units[key]['weight'], 2),
        'overlap_weight': round(min(left_units[key]['weight'], right_units[key]['weight']), 2),
        'security_class': ASSET_FUND_UNIT,
        'matched_by': 'AMFI share class' if key.startswith('AMFI:') else 'ISIN',
    } for key in common]
    overlap = sum(min(left_units[key]['weight'], right_units[key]['weight']) for key in common)
    return {
        'overlap_type': OVERLAP_DIRECT_FOF,
        'overlap_label': OVERLAP_DIRECT_FOF_LABEL,
        'overlap_pct': overlap,
        'common_holdings': rows,
        'equity_overlap_pct': None,
        'debt_overlap_pct': None,
        'common_equity_count': None,
        'common_debt_count': None,
        'common_count': len(rows),
        'common_fund_unit_count': len(rows),
        'message': None if rows else REASON_MESSAGES[REASON_NO_COMMON_FUNDS],
    }


def _fund_units(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row['isin']: row for row in data.get('other_holdings') or [] if row.get('isin') and row['instrument_type'] in _FUND_UNIT_TYPES}


def _common_fund_units(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    """Fund units both schemes hold. These are never part of calculate_overlap."""
    left_units, right_units = _fund_units(left), _fund_units(right)
    return [{
        'isin': isin,
        'name': left_units[isin]['name'],
        'left_weight': round(left_units[isin]['weight'], 2),
        'right_weight': round(right_units[isin]['weight'], 2),
    } for isin in sorted(left_units.keys() & right_units.keys())]


def import_portfolio_holdings(scheme_code: str, portfolio_date: str, filename: str, payload: bytes, source_url: str = '') -> dict[str, Any]:
    """Validate a real disclosure file and cache its parsed holdings for monthly use."""
    code = str(scheme_code)
    if not filename.lower().endswith(('.xlsx', '.xls', '.csv')):
        raise ProviderError('Upload an Excel or CSV portfolio disclosure file')
    if not payload or len(payload) > 15 * 1024 * 1024:
        raise ProviderError('Portfolio disclosure file must be between 1 byte and 15 MB')
    try:
        reporting_date = date.fromisoformat(portfolio_date)
    except ValueError as exc:
        raise ProviderError('portfolio_date must be YYYY-MM-DD') from exc
    if reporting_date > date.today():
        raise ProviderError('Portfolio reporting date cannot be in the future')
    record = next((item for item in fetch_amfi_latest() if str(item.get('scheme_code')) == code), None)
    if record is None:
        raise ProviderError('Scheme code was not found in AMFI NAV data')
    amc_name = record.get('amc') or ''
    if not amc_name:
        raise ProviderError('AMC name was not found in AMFI NAV data')
    diagnostics: dict[str, Any] = {}
    holdings = _read_holdings(record)(payload, filename, record.get('scheme_name') or '', 'import_by_code', diagnostics)
    actual_date = diagnostics.get('reporting_date')
    if not actual_date:
        raise ProviderError('Portfolio reporting date was not found inside the disclosure file')
    if actual_date != reporting_date.isoformat():
        raise ProviderError(f'Portfolio file reports {actual_date}, not {reporting_date.isoformat()}')
    cache_key = f'fund-analyzer:portfolio-import:{_amc_directory_name(amc_name)}:{code}:{reporting_date.isoformat()}'
    link = {
        'url': f'cache://{_amc_directory_name(amc_name)}/{code}/{reporting_date.isoformat()}',
        'cache_key': cache_key,
        'text': filename,
        'month': reporting_date.strftime('%Y-%m'),
        'portfolio_date': reporting_date.isoformat(),
        'imported': True,
    }
    try:
        cache.set(cache_key, {
            'holdings': holdings, 'names': diagnostics.get('holding_names') or {},
            'industries': diagnostics.get('holding_industries') or {}, 'benchmark': diagnostics.get('benchmark'),
            'risk_level': diagnostics.get('risk_level'), 'source_url': source_url,
            'instrument_types': diagnostics.get('instrument_types') or {}, 'issuers': diagnostics.get('issuers') or {},
            'other_holdings': diagnostics.get('other_holdings') or [],
            'security_classes': diagnostics.get('security_classes'), 'classes_read': diagnostics.get('classes_read'),
            'unclassified_weight': diagnostics.get('unclassified_weight'),
        }, timeout=IMPORTED_HOLDINGS_CACHE_SECONDS)
        index_key = _import_index_key(amc_name, code)
        existing = cache.get(index_key) or []
        links = [link, *(item for item in existing if item.get('portfolio_date') != link['portfolio_date'])]
        links.sort(key=lambda item: item['portfolio_date'], reverse=True)
        cache.set(index_key, links[:6], timeout=IMPORTED_HOLDINGS_CACHE_SECONDS)
        if cache.get(cache_key) is None:
            raise ProviderError('Portfolio holdings cache did not retain the import')
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError('Portfolio holdings cache is unavailable') from exc
    store_snapshot(record, code, reporting_date.strftime('%Y-%m'), {
        'holdings': holdings, 'names': diagnostics.get('holding_names'), 'industries': diagnostics.get('holding_industries'),
        'benchmark': diagnostics.get('benchmark'), 'risk_level': diagnostics.get('risk_level'), 'portfolio_date': reporting_date.isoformat(),
        'asset_class': _asset_class(record), 'instrument_types': diagnostics.get('instrument_types'), 'issuers': diagnostics.get('issuers'),
        'other_holdings': diagnostics.get('other_holdings'),
        'security_classes': diagnostics.get('security_classes'), 'classes_read': diagnostics.get('classes_read'),
        'unclassified_weight': diagnostics.get('unclassified_weight'),
    }, source_url=source_url, payload=payload, source_type=SOURCE_IMPORT)
    record_status(record, HOLDINGS_AVAILABLE, 'Staff upload', reporting_date.strftime('%Y-%m'))
    return {
        'scheme_code': code,
        'scheme_name': record.get('scheme_name'),
        'amc': amc_name,
        'portfolio_date': reporting_date.isoformat(),
        'holding_count': len(holdings),
    }


ALL_SOURCES_UNAVAILABLE = 'Portfolio holdings unavailable for a matching reporting month.'
# A month-end disclosure can be dated the last business day, so allow a few days before the calendar end.
MONTH_END_FROM_DAY = 25


def _charlie_links(record: dict[str, Any], allowed_months: set[str]) -> list[dict[str, Any]]:
    """The backup source's reporting months for this scheme, one per month, newest first.

    Only the list of dates is requested here. Holdings are fetched later, for just the months
    the comparison actually needs.
    """
    code = str(record.get('scheme_code'))
    links, seen = [], set()
    for as_of in charlie_alpha.list_periods(record):
        month = as_of[:7]
        # Month-end portfolios only. The provider also keeps mid-month (fortnightly) snapshots, but the
        # official reader and every comparison use the month-end disclosure, so dates stay like for like.
        if int(as_of[8:10]) < MONTH_END_FROM_DAY:
            continue
        if month in allowed_months and month not in seen:
            seen.add(month)
            links.append({'url': f'charlie://{code}/{as_of}', 'month': month, 'portfolio_date': as_of, 'charlie': True, 'as_of': as_of})
    return links


def _holdings_from_charlie(code: str, month: str, link: dict[str, Any], debug: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any]:
    """One month of holdings from the backup source, checked, converted and saved as third-party JSON.

    Raises if the response fails any check. Nothing is ever repaired or topped up from another source.
    """
    if not charlie_alpha.enabled():
        raise charlie_alpha.CharlieError(charlie_alpha.DISABLED, 'Backup source is switched off')
    if record is None:
        raise ProviderError('Scheme is missing from the AMFI list')
    if month not in _backup_window():
        raise charlie_alpha.CharlieError(charlie_alpha.DATE_MISMATCH, f'{month} is outside the backup lookback')
    as_of = link['as_of']
    payload = charlie_alpha.fetch_holdings(record, as_of)
    asset_class = _asset_class(record)
    category = payload.get('category')
    result = charlie_alpha.normalise_portfolio(
        record, payload, requested_as_of=as_of, asset_class=asset_class,
        payload_asset_class=_asset_class({'category': category}) if category else None,
    )
    if result['portfolio_date'][:7] != month:
        raise ProviderError(f"Portfolio reports {result['portfolio_date']}, not {month}")
    validation = result.pop('validation')
    reference = charlie_alpha.endpoint_reference(code, as_of)
    result.update({
        'matched_sheet_name': 'Third-party API', 'source': 'Charlie Alpha API', 'asset_class': asset_class,
        'source_url': reference, 'source_type': charlie_alpha.SOURCE_THIRD_PARTY,
    })
    debug['parse_result'] = {'status': 'ok', 'holding_count': len(result['holdings']), 'source': 'charlie_alpha'}
    debug['matched_sheet_name'] = 'Third-party API'
    # store_snapshot never lets this replace an official month, and never fails the analysis if it cannot write.
    store_snapshot(record, code, month, result, source_url=reference, source_type=SOURCE_CHARLIE, validation=validation)
    result['validation'] = validation
    return result


def _ensure_classes(needed: set[str], code: str, month: str, data: dict[str, Any], source: dict[str, Any], scheme_name: str, debug: dict[str, Any], downloaded: dict[str, bytes], workbooks: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any]:
    """A file cached before securities were labelled lacks a kind. Re-read the official file once."""
    if needed <= set(data.get('classes_read') or []) or not data.get('source_url'):
        return data
    try:
        return _holdings_from_link(code, month, {'url': data['source_url'], 'month': month}, source, scheme_name, debug, downloaded, workbooks, record)
    except Exception as exc:
        logger.warning('Portfolio %s could not be re-read for scheme %s: %s', month, code, str(exc) or exc.__class__.__name__)
        return data


def _empty_highest_overlap(total_pairs: int) -> dict[str, Any]:
    return {
        'available': False,
        'percentage': None,
        'fund_a_code': None,
        'fund_a_name': None,
        'fund_b_code': None,
        'fund_b_name': None,
        'portfolio_month': None,
        'portfolio_date': None,
        'pairs_different_months': False,
        'partial_coverage': False,
        'compared_pairs': 0,
        'total_pairs': total_pairs,
        'message': 'Portfolio holdings unavailable',
        'reason_code': REASON_HOLDINGS_UNAVAILABLE,
    }


def _holdings_from_link(
    code: str,
    month: str,
    link: dict[str, Any],
    source: dict[str, Any],
    scheme_name: str,
    debug: dict[str, Any],
    downloaded: dict[str, bytes],
    workbooks: dict[str, tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]]],
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load one month's verified holdings from storage, a cached entry or a disclosure file."""
    if link.get('charlie'):
        return _holdings_from_charlie(code, month, link, debug, record)
    instrument_types: dict[str, str] = {}
    issuers: dict[str, str] = {}
    other_holdings: list[dict[str, Any]] = []
    security_classes: dict[str, str] = {}
    default_classes = {ASSET_DEBT: [ASSET_DEBT], ASSET_FOF: [ASSET_FUND_UNIT]}.get(_asset_class(record), [ASSET_EQUITY])
    classes_read: list[str] = default_classes
    unclassified_weight: float | None = None
    source_url = link['url'] if str(link.get('url', '')).startswith('http') else ''
    stored_source = charlie_alpha.SOURCE_OFFICIAL
    if link.get('stored'):
        stored = load_snapshot(link['snapshot_id'])
        holdings, names, industries = stored['holdings'], stored['names'], stored['industries']
        benchmark, risk_level, reporting_date = stored['benchmark'], stored['risk_level'], stored['portfolio_date']
        instrument_types, issuers = stored['instrument_types'], stored['issuers']
        other_holdings = stored['other_holdings']
        security_classes, classes_read = stored['security_classes'], stored['classes_read']
        unclassified_weight, source_url = stored['unclassified_weight'], stored['source_url']
        stored_source = charlie_alpha.SOURCE_THIRD_PARTY if stored.get('source_type') == SOURCE_CHARLIE else charlie_alpha.SOURCE_OFFICIAL
        debug['parse_result'] = {'status': 'ok', 'holding_count': len(holdings), 'source': 'stored'}
    elif link.get('imported') or link.get('cached'):
        cached = cache.get(link['cache_key'])
        if not cached or not cached.get('holdings'):
            raise ProviderError('Cached monthly holdings expired')
        holdings = cached['holdings']
        names = cached.get('names') or {}
        industries = cached.get('industries') or {}
        benchmark = cached.get('benchmark')
        risk_level = cached.get('risk_level')
        instrument_types, issuers = cached.get('instrument_types') or {}, cached.get('issuers') or {}
        other_holdings = cached.get('other_holdings') or []
        security_classes, classes_read = cached.get('security_classes') or {}, cached.get('classes_read') or default_classes
        unclassified_weight = cached.get('unclassified_weight')
        reporting_date = cached.get('portfolio_date') or link.get('portfolio_date')
        debug['parse_result'] = {
            'status': 'ok', 'holding_count': len(holdings),
            'source': 'monthly_import' if link.get('imported') else 'parsed_cache',
        }
    else:
        if link['url'] not in downloaded:
            downloaded[link['url']] = _download_cached(source['key'], month, link['url'])
        if link['url'] not in workbooks:
            workbooks[link['url']] = _workbook_sheets(downloaded[link['url']], urlsplit(link['url']).path.lower())
        holdings = _read_holdings(record)(
            downloaded[link['url']], link['url'], scheme_name, source['parser'],
            debug, workbooks[link['url']],
        )
        # These belong with the holdings, not in the debug response.
        names = debug.pop('holding_names', {})
        industries = debug.pop('holding_industries', {})
        benchmark = debug.pop('benchmark', None)
        risk_level = debug.pop('risk_level', None)
        instrument_types = debug.pop('instrument_types', {})
        issuers = debug.pop('issuers', {})
        other_holdings = debug.pop('other_holdings', [])
        security_classes = debug.pop('security_classes', {})
        classes_read = debug.pop('classes_read', default_classes)
        unclassified_weight = debug.pop('unclassified_weight', None)
        reporting_date = debug.get('reporting_date')
    if not reporting_date:
        raise ProviderError('Portfolio reporting date was not found inside the disclosure file')
    if reporting_date[:7] != month:
        raise ProviderError(f'Portfolio file reports {reporting_date}, not {month}')
    source_label = (debug.get('parse_result') or {}).get('source')
    debug['parse_result'] = {
        'status': 'ok', 'holding_count': len(holdings),
        **({'source': source_label} if source_label else {}),
    }
    if link.get('cached') or link.get('imported') or link.get('stored'):
        debug['matched_sheet_name'] = 'Stored holdings' if link.get('stored') else 'Cached holdings'
    result = {
        'holdings': holdings, 'names': names, 'industries': industries, 'benchmark': benchmark, 'risk_level': risk_level, 'portfolio_date': reporting_date,
        'matched_sheet_name': debug['matched_sheet_name'],
        'source': source_label,
        'asset_class': _asset_class(record),
        'instrument_types': instrument_types,
        'issuers': issuers,
        'other_holdings': other_holdings,
        'security_classes': security_classes,
        'classes_read': classes_read,
        'unclassified_weight': unclassified_weight,
        'source_url': source_url,
        'source_type': stored_source,
    }
    if not link.get('imported') and not link.get('cached') and not link.get('stored'):
        _cache_parsed_holdings(code, reporting_date, holdings, link['url'], names, industries, benchmark, risk_level, instrument_types, issuers, other_holdings, {'security_classes': security_classes, 'classes_read': classes_read, 'unclassified_weight': unclassified_weight})
        if record:
            store_snapshot(record, code, month, result, source_url=link['url'], payload=downloaded.get(link['url']))
    return result


def _link_error_status(link: dict[str, Any], error: Exception) -> str:
    """HDFC's file host answers 403 for a monthly file that does not exist yet."""
    status = classify_error(error)
    return DISCLOSURE_NOT_PUBLISHED if link.get('guessed') and status == DOWNLOAD_BLOCKED else status


def _asset_allocation(weights: dict[str, float]) -> dict[str, float]:
    """Split by ISIN country. Other is what the equity rows leave: debt, cash and REITs."""
    indian = sum(weight for isin, weight in weights.items() if isin.startswith('IN'))
    international = sum(weights.values()) - indian
    return {
        'indian_equity': round(indian, 2),
        'international_equity': round(international, 2),
        'other': round(max(0.0, 100 - indian - international), 2),
    }


def _debt_allocation(weights: dict[str, float]) -> dict[str, float]:
    """Other is what the debt securities leave: fund units, repo, cash and net current assets.

    It goes below zero when net current assets are negative, which is what puts the
    securities above 100%. Liquid funds often show this at month end.
    """
    total = sum(weights.values())
    return {'debt_securities': round(total, 2), 'other': round(100 - total, 2)}


def _fof_allocation(weights: dict[str, float]) -> dict[str, float]:
    """Other is what the fund units leave: repo, cash, net current assets and any AIF units."""
    total = sum(weights.values())
    return {'fund_units': round(total, 2), 'other': round(100 - total, 2)}


def _hybrid_allocation(equity: dict[str, float], debt: dict[str, float]) -> dict[str, float]:
    """Both parts as a share of the whole scheme. Neither is scaled to 100%."""
    equity_total, debt_total = sum(equity.values()), sum(debt.values())
    return {'equity': round(equity_total, 2), 'debt_securities': round(debt_total, 2), 'other': round(100 - equity_total - debt_total, 2)}


def _debt_instrument_allocation(weights: dict[str, float], instrument_types: dict[str, str], others: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every instrument type in the file, so the rows add up to the whole portfolio."""
    totals: dict[str, float] = {}
    for isin, weight in weights.items():
        kind = instrument_types.get(isin) or 'Debt Instrument'
        totals[kind] = totals.get(kind, 0.0) + weight
    for row in others:
        totals[row['instrument_type']] = totals.get(row['instrument_type'], 0.0) + row['weight']
    return [{'name': name, 'weight': round(weight, 2)} for name, weight in sorted(totals.items(), key=lambda item: (-item[1], item[0]))]


def _debt_other_rows(others: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fund units and repo as the file lists them. Overlap does not use these."""
    return [{
        **({'isin': row['isin']} if row.get('isin') else {}),
        'name': row['name'], 'weight': round(row['weight'], 2), 'instrument_type': row['instrument_type'], 'excluded_from_overlap': True,
    } for row in others if row['instrument_type'] not in _BALANCE_TYPES and row['weight'] > 0]


def _sector_allocation(weights: dict[str, float], industries: dict[str, str]) -> list[dict[str, Any]]:
    """Largest industries as the AMC labels them, with the remainder grouped as Others."""
    if not any(industries.values()):
        return []
    totals: dict[str, float] = {}
    for isin, weight in weights.items():
        sector = industries.get(isin) or 'Others'
        totals[sector] = totals.get(sector, 0.0) + weight
    others = totals.pop('Others', 0.0)
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    others += sum(weight for _, weight in ranked[TOP_SECTORS:])
    rows = [{'name': name, 'weight': round(weight, 2)} for name, weight in ranked[:TOP_SECTORS]]
    return [*rows, {'name': 'Others', 'weight': round(others, 2)}] if others > 0 else rows


def fetch_fund_holdings(code: str, record: dict[str, Any] | None) -> dict[str, Any]:
    """Latest verified holdings for one fund, largest first, with their breakdowns."""
    empty = {'holdings': [], 'holdings_as_of': None, 'benchmark': None, 'allocation': None, 'sectors': [], 'riskometer': None, 'asset_class': _asset_class(record)}
    if not record:
        return empty
    amc_name = record.get('amc') or ''
    backup_window = _backup_window()
    stored = [link for link in stored_links(record) if not link.get('third_party') or (charlie_alpha.enabled() and link['month'] in backup_window)]
    try:
        links = [*stored, *_imported_links(amc_name, code), *_parsed_links(code)]
    except Exception as exc:
        logger.warning('Portfolio holdings cache unavailable for %s: %s', code, exc)
        links = list(stored)
    source = source_for_amc(amc_name)
    name = file_scheme_name(record)
    if source and discovery_due(record):
        try:
            links += _monthly_files_for_fund(source, name)
        except ProviderError as exc:
            logger.warning('Portfolio link lookup failed for AMC %s: %s', amc_name, exc)
    recent_months = _recent_portfolio_months()
    # A stable sort keeps stored and uploaded entries ahead of downloads within a month.
    # Newest month first. Within a month, official files come before a saved third-party copy.
    links = sorted((link for link in links if link.get('month') in recent_months), key=lambda link: (link['month'], not link.get('third_party')), reverse=True)
    source = source or {'key': _amc_directory_name(amc_name), 'parser': 'import_by_code'}
    debug = _overlap_debug_record(code, record)
    downloaded: dict[str, bytes] = {}
    workbooks: dict[str, tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]]] = {}

    def with_backup():
        # Reached only after every official month failed, so the backup source is never asked while official data works.
        yield from links
        if charlie_alpha.enabled():
            try:
                yield from _charlie_links(record, backup_window)
            except charlie_alpha.CharlieError as exc:
                logger.warning('Backup periods unavailable for scheme %s: %s', code, exc.code)

    if charlie_alpha.enabled():
        charlie_alpha.begin_analysis()
    for link in with_backup():
        try:
            result = _holdings_from_link(code, link['month'], link, source, name, debug, downloaded, workbooks, record)
        except Exception as exc:
            logger.warning('Portfolio %s failed for scheme %s: %s', link['month'], code, str(exc) or exc.__class__.__name__)
            continue
        asset_class = _asset_class(record)
        needed = FUND_SECURITY_CLASSES[asset_class]
        result = _ensure_classes(needed, code, link['month'], result, source, name, debug, downloaded, workbooks, record)
        # An equity fund shows its equity rows only, as it always has.
        weights, names = _weights_of(result, needed), result['names']
        instrument_types, issuers = result.get('instrument_types') or {}, result.get('issuers') or {}
        industries = result.get('industries') or {}
        others = (result.get('other_holdings') or []) if asset_class != ASSET_EQUITY else []
        is_hybrid = asset_class == ASSET_HYBRID
        equity_rows = _weights_of(result, {ASSET_EQUITY}) if is_hybrid else {}
        rows = [
            {
                'isin': isin, 'name': names.get(isin) or isin, 'weight': round(weights[isin], 2),
                **({'instrument_type': 'Equity Shares'} if isin in equity_rows else {}),
                **({'sector': industries[isin]} if isin in equity_rows and industries.get(isin) else {}),
                **({'instrument_type': instrument_types[isin]} if instrument_types.get(isin) else {}),
                **({'issuer': issuers[isin]} if issuers.get(isin) else {}),
                **({'rating': industries[isin]} if asset_class != ASSET_EQUITY and isin not in equity_rows and industries.get(isin) else {}),
            }
            for isin in sorted(weights, key=lambda isin: (-weights[isin], isin))
        ]
        instrument_labels = {**instrument_types, **{isin: 'Equity Shares' for isin in equity_rows}}
        if asset_class == ASSET_DEBT:
            allocation = _debt_allocation(weights)
        elif asset_class == ASSET_FOF:
            allocation = _fof_allocation(weights)
        elif is_hybrid:
            allocation = _hybrid_allocation(equity_rows, _weights_of(result, {ASSET_DEBT}))
        else:
            allocation = _asset_allocation(weights)
        return {
            'holdings': sorted([*rows, *_debt_other_rows(others)], key=lambda row: -row['weight']),
            'holdings_as_of': result['portfolio_date'],
            'holdings_source': result.get('source_type') or charlie_alpha.SOURCE_OFFICIAL,
            'benchmark': result.get('benchmark'),
            'asset_class': asset_class,
            'allocation': allocation,
            'sectors': _debt_instrument_allocation(weights, instrument_labels, others) if asset_class != ASSET_EQUITY else _sector_allocation(weights, industries),
            'riskometer': {
                'level': result['risk_level'],
                'as_of': result['portfolio_date'],
                'source': 'Official AMC Disclosure',
            } if result.get('risk_level') else None,
        }
    return empty


def _overlap_reason(highest_pair: dict[str, Any] | None, funds: list[dict[str, Any]], unavailable_pairs: list[dict[str, Any]]) -> str | None:
    """Say why there is no overlap figure, or why the figure is zero."""
    if highest_pair:
        if highest_pair['common_holdings']:
            return None
        return REASON_NO_COMMON_FUNDS if highest_pair.get('overlap_type') == OVERLAP_DIRECT_FOF else REASON_NO_COMMON_SECURITIES
    available = [fund for fund in funds if fund.get('available')]
    if any(fund.get('reason_code') == REASON_HISTORICAL_UNAVAILABLE for fund in funds):
        return REASON_HISTORICAL_UNAVAILABLE
    if unavailable_pairs:
        reasons = {pair['reason'] for pair in unavailable_pairs}
        return reasons.pop() if len(reasons) == 1 else REASON_HOLDINGS_UNAVAILABLE
    statuses = {fund.get('status') for fund in funds if not fund.get('available')}
    if statuses & {PARSING_FAILED, INVALID_WORKBOOK}:
        return REASON_PARSER_UNSUPPORTED
    if len(available) >= 2 or statuses == {NO_COMMON_MONTH}:
        return REASON_NO_COMMON_MONTH
    return REASON_HOLDINGS_UNAVAILABLE


def _pair_source(left: dict[str, Any], right: dict[str, Any]) -> str:
    """official_amc only if both sides are official. charlie_alpha if both are third-party. Otherwise mixed."""
    sources = {left.get('source_type') or charlie_alpha.SOURCE_OFFICIAL, right.get('source_type') or charlie_alpha.SOURCE_OFFICIAL}
    return sources.pop() if len(sources) == 1 else 'mixed'


def _fund_reason_code(fund: dict[str, Any]) -> str | None:
    """The machine-readable reason a fund has no usable holdings, or None when it has some."""
    if fund.get('available'):
        return None
    status = fund.get('status')
    if status == REASON_HISTORICAL_UNAVAILABLE:
        return REASON_HISTORICAL_UNAVAILABLE
    if status == SOURCE_NOT_SUPPORTED:
        return REASON_SOURCE_NOT_SUPPORTED
    if status == NO_COMMON_MONTH:
        return REASON_NO_COMMON_MONTH
    if status in (PARSING_FAILED, INVALID_WORKBOOK):
        return REASON_PARSER_UNSUPPORTED
    if status == DOWNLOAD_BLOCKED:
        return REASON_DOWNLOAD_BLOCKED
    if status == SCHEME_NOT_FOUND:
        return REASON_SCHEME_NOT_IN_FILES
    if status in (DISCLOSURE_NOT_PUBLISHED, DOWNLOAD_FAILED):
        return REASON_FILE_UNAVAILABLE
    return REASON_HOLDINGS_UNAVAILABLE


def _pair_gap(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Why two selected funds have no overlap figure. A fund's own problem comes before the pair's."""
    problems = [fund for fund in (left, right) if not fund.get('available')]
    ranking = (REASON_HISTORICAL_UNAVAILABLE, REASON_SOURCE_NOT_SUPPORTED, REASON_DOWNLOAD_BLOCKED, REASON_FILE_UNAVAILABLE, REASON_SCHEME_NOT_IN_FILES, REASON_PARSER_UNSUPPORTED, REASON_HOLDINGS_UNAVAILABLE, REASON_NO_COMMON_MONTH)
    code = min((fund.get('reason_code') or REASON_HOLDINGS_UNAVAILABLE for fund in problems), key=lambda reason: ranking.index(reason) if reason in ranking else len(ranking), default=REASON_NO_COMMON_MONTH)
    message = historical_message([fund.get('scheme_name') for fund in problems if fund.get('reason_code') == REASON_HISTORICAL_UNAVAILABLE]) if code == REASON_HISTORICAL_UNAVAILABLE else REASON_MESSAGES[code]
    return {
        'left': left['scheme_code'], 'right': right['scheme_code'], 'reason': code, 'reason_code': code, 'message': message,
        # Each fund's own latest month, so a pair with no common month shows what each one has.
        'left_portfolio_month': left.get('portfolio_month'), 'right_portfolio_month': right.get('portfolio_month'),
    }


def analyze_holdings_overlap(scheme_codes: list[str], portfolio_month: str | None = None, progress: Callable[[str], None] | None = None) -> dict:
    """Compare funds' disclosed holdings.

    portfolio_month (YYYY-MM) selects historical mode: only that month's verified holdings
    are used, for every fund, and a fund without them is reported unavailable. Without it,
    the latest common month in the lookback is used, as always.

    progress, if given, is called with 'fetching', 'validating' and 'calculating' as the work
    reaches each stage. Downloads happen as each month's file is first needed, so 'validating'
    covers reading and checking that file as well as the checks themselves.
    """
    historical_mode = portfolio_month is not None
    report = progress or (lambda _stage: None)
    report('fetching')
    if len(scheme_codes) < 2:
        return {
            'portfolio_month': None,
            'funds': [],
            'pairs': [],
            'pairs_different_months': False,
            'highest_pair': None,
            'highest_overlap': _empty_highest_overlap(0),
            'available': False,
            'status': NO_COMMON_MONTH,
            'debug': [_overlap_debug_record(str(code), error='At least two funds are required for overlap comparison') for code in scheme_codes],
        }
    try:
        latest_records = fetch_amfi_latest()
    except ProviderError:
        debug = [_overlap_debug_record(str(code), error='AMC lookup unavailable') for code in scheme_codes]
        logger.debug('Holdings overlap diagnostics: %s', debug)
        return {
            'portfolio_month': None,
            'funds': [{'scheme_code': str(code), 'available': False, 'reason': 'AMC lookup unavailable', 'status': DOWNLOAD_FAILED} for code in scheme_codes],
            'pairs': [],
            'pairs_different_months': False,
            'highest_pair': None,
            'highest_overlap': _empty_highest_overlap(len(scheme_codes) * (len(scheme_codes) - 1) // 2),
            'available': False,
            'status': DOWNLOAD_FAILED,
            'debug': debug,
        }
    record_by_code = {str(row.get('scheme_code')): row for row in latest_records}
    recent_months = {portfolio_month} if historical_mode else _recent_portfolio_months()
    backup_window = _backup_window(recent_months)
    fund_results = []
    debug_by_code: dict[str, dict[str, Any]] = {}
    links_by_code: dict[str, list[dict[str, str]]] = {}
    source_by_code: dict[str, dict] = {}
    name_by_code: dict[str, str] = {}
    checked_sources: set[str] = set()
    status_by_code: dict[str, str] = {}
    # The backup source is tried per fund, and only once the official path has failed for it.
    backup_tried: set[str] = set()
    backup_failed: dict[str, str] = {}
    backup_cache: dict[str, list[dict[str, Any]]] = {}
    if charlie_alpha.enabled():
        charlie_alpha.begin_analysis()

    def backup_links(code: str) -> list[dict[str, Any]]:
        """The backup source's months for a fund the official reader could not supply. [] if switched off or unavailable."""
        if not charlie_alpha.enabled():
            return []
        if code not in backup_cache:
            report('backup')
            backup_tried.add(code)
            try:
                backup_cache[code] = _charlie_links(record_by_code[code], backup_window)
            except charlie_alpha.CharlieError as exc:
                logger.warning('Backup periods unavailable for scheme %s: %s', code, exc.code)
                backup_failed[code] = exc.code
                debug_by_code[code]['backup_error'] = exc.code
                backup_cache[code] = []
        return backup_cache[code]

    for code in scheme_codes:
        code = str(code)
        record = record_by_code.get(code)
        debug = _overlap_debug_record(code, record)
        debug_by_code[code] = debug
        if not record:
            debug['error'] = 'Fund is missing from the latest AMFI scheme list'
            status_by_code[code] = SCHEME_NOT_FOUND
            fund_results.append({'scheme_code': code, 'available': False, 'reason': debug['error']})
            continue
        amc_name = record.get('amc') or ''
        try:
            imported_links = _imported_links(amc_name, code)
            parsed_links = _parsed_links(code)
        except Exception as exc:
            logger.warning('Portfolio holdings cache unavailable for %s: %s', code, exc)
            imported_links = []
            parsed_links = []
        # Saved third-party copies are served only while the backup source is on, and only inside its window.
        stored = [link for link in stored_links(record) if not link.get('third_party') or (charlie_alpha.enabled() and link['month'] in backup_window)]
        name_by_code[code] = file_scheme_name(record)
        state = scheme_status(record)
        # A scheme that stopped reporting years ago has no recent portfolio to find. Its own
        # last months are history, and in historical mode a month after its last NAV is impossible.
        stopped = state['status'] != SCHEME_ACTIVE and (not historical_mode or portfolio_month > (state['last_nav_date'] or '')[:7])
        if stopped:
            debug['error'] = f"Scheme stopped reporting NAV on {state['last_nav_date']} ({state['status'].lower()})"
            status_by_code[code] = REASON_HISTORICAL_UNAVAILABLE
            fund_results.append({
                'scheme_code': code, 'scheme_name': record.get('scheme_name'), 'amc': amc_name, 'available': False,
                'reason': historical_message([record.get('scheme_name')]),
                'historical_months_available': sorted({link['month'] for link in stored}, reverse=True),
            })
            continue
        source = source_for_amc(amc_name)
        # Past the AMC site's lookback there is nothing to list, so only saved files can answer.
        can_discover = bool(source) and (not historical_mode or portfolio_month in _recent_portfolio_months())
        if not source and not imported_links and not parsed_links and not stored:
            debug['error'] = 'Overlap not available for this AMC'
            backup = backup_links(code)
            if backup:
                links_by_code[code] = backup
                source_by_code[code] = {'key': _amc_directory_name(amc_name), 'parser': 'import_by_code'}
                debug.update({'monthly_links_found': len(backup), 'months_found': sorted({link['month'] for link in backup})})
                fund_results.append({'scheme_code': code, 'scheme_name': record.get('scheme_name'), 'amc': amc_name, 'available': False, 'reason': 'Waiting for a common portfolio month'})
                continue
            # The background job may know why, such as a blocked AMC website.
            status_by_code[code] = source_status(record) or SOURCE_NOT_SUPPORTED
            fund_results.append({'scheme_code': code, 'scheme_name': record.get('scheme_name'), 'amc': amc_name, 'available': False, 'reason': debug['error']})
            continue
        remote_links = []
        try:
            # Ask the AMC's site only when no recent check has already answered this.
            if can_discover and discovery_due(record):
                checked_sources.add(code)
                remote_links = _monthly_files_for_fund(source, name_by_code[code])
                listing_errors = [link['listing_error'] for link in remote_links if link.get('listing_error')]
                if listing_errors:
                    debug['error'] = listing_errors[0]
            elif can_discover and not stored:
                status_by_code[code] = source_status(record) or DOWNLOAD_FAILED
                debug['error'] = 'Waiting before asking this source again'
        except ProviderError as exc:
            logger.warning('Portfolio link lookup failed for AMC %s: %s', amc_name, exc)
            debug['error'] = str(exc)
        try:
            links = [link for link in [*stored, *imported_links, *parsed_links, *remote_links] if link.get('month') in recent_months]
            # Within a month, official files come before a saved third-party copy of that month.
            links.sort(key=lambda link: bool(link.get('third_party')))
            if not links:
                links = backup_links(code)
            if not links:
                raise ProviderError('No monthly portfolio links or cached holdings in the lookback period')
            months = sorted({str(link.get('month')) for link in links if link.get('month')})
            debug.update({'monthly_links_found': len(links), 'months_found': months})
            links_by_code[str(code)] = links
            source_by_code[str(code)] = source or {'key': _amc_directory_name(amc_name), 'parser': 'import_by_code'}
            fund_results.append({'scheme_code': code, 'scheme_name': record.get('scheme_name'), 'amc': amc_name, 'available': False, 'reason': 'Waiting for a common portfolio month'})
        except ProviderError as exc:
            debug['error'] = debug['error'] or str(exc)
            # A directory page that lists nothing usable is a source we can't read yet.
            if source and source.get('parser') == 'directory_monthly_excel' and classify_error(debug['error']) == DISCLOSURE_NOT_PUBLISHED:
                status_by_code.setdefault(code, SOURCE_NOT_SUPPORTED)
            fund_results.append({'scheme_code': code, 'scheme_name': record.get('scheme_name'), 'amc': amc_name, 'available': False, 'reason': 'Overlap unavailable right now'})

    report('validating')
    valid_by_month: dict[str, dict[str, dict[str, Any]]] = {}
    downloaded: dict[str, bytes] = {}
    workbooks: dict[str, tuple[dict[str, pd.DataFrame], dict[str, list[list[str]]]]] = {}
    loaded: dict[tuple[str, str], dict[str, Any] | None] = {}
    failed_links: set[tuple[str, str]] = set()

    def is_backup(link: dict[str, Any]) -> bool:
        return bool(link.get('charlie') or link.get('third_party'))

    def load(code: str, month: str, comparing: bool = True, backup: bool = False) -> dict[str, Any] | None:
        """This fund's first valid holdings for the month from one kind of source. Each file is tried once.

        backup=False reads official files only. backup=True reads a saved third-party copy first, then
        the backup API, and only inside the backup window. The two are never mixed for one fund-month.
        comparing=False reads a fund's own month only to report it. As before, that is not counted as
        an attempted comparison month, so a failure there never puts the fund's source into back-off.
        """
        key = (code, month, backup)
        if loaded.get(key) is not None:
            return loaded[key]
        if backup and month not in backup_window:
            return None
        candidates = [link for link in links_by_code[code] if link['month'] == month and is_backup(link) == backup and (code, link['url']) not in failed_links]
        # A saved copy answers before a new request.
        candidates.sort(key=lambda link: bool(link.get('charlie')))
        debug = debug_by_code[code]
        if comparing and candidates and month not in debug['attempted_months']:
            debug['attempted_months'].append(month)
        for link in candidates:
            try:
                loaded[key] = _holdings_from_link(code, month, link, source_by_code[code], name_by_code[code], debug, downloaded, workbooks, record_by_code[code])
                return loaded[key]
            except Exception as exc:
                failed_links.add((code, link['url']))
                if link.get('charlie'):
                    # A backup failure never overwrites the official reader's own reason.
                    backup_failed[code] = getattr(exc, 'code', 'ERROR')
                    debug['backup_error'] = backup_failed[code]
                    logger.warning('Backup portfolio %s failed for scheme %s: %s', month, code, backup_failed[code])
                    continue
                error = str(exc) or exc.__class__.__name__
                status_by_code.setdefault(code, _link_error_status(link, exc))
                logger.warning('Portfolio %s failed for scheme %s: %s', month, code, error)
                debug['errors_by_month'].setdefault(month, []).append(error)
                debug['error'] = error
                debug['parse_result'] = {'status': 'error'}
        loaded[key] = None
        return None

    def backup_has(code: str, month: str) -> bool:
        """Can the backup supply this fund for this month? Asks the provider for its dates at most once."""
        if not charlie_alpha.enabled() or month not in backup_window:
            return False
        if not any(is_backup(link) and link['month'] == month for link in links_by_code[code]) and code not in backup_cache:
            links_by_code[code] = [*links_by_code[code], *backup_links(code)]
        return any(is_backup(link) and link['month'] == month and (code, link['url']) not in failed_links for link in links_by_code[code])

    def official_months_of(code: str) -> list[str]:
        return sorted({link['month'] for link in links_by_code[code] if not is_backup(link)}, reverse=True)

    def has_official_data(code: str) -> bool:
        """Does any month within the window give this fund valid official holdings?"""
        if any(code in valid and (valid[code].get('source_type') or charlie_alpha.SOURCE_OFFICIAL) == charlie_alpha.SOURCE_OFFICIAL for valid in valid_by_month.values()):
            return True
        return any(load(code, month, comparing=False) for month in official_months_of(code))

    def choose_months() -> tuple[list[str], str | None]:
        """The one month every comparable fund can be compared in, chosen in this order:

        1. Official files for every fund, newest month first, within the official lookback.
        2. Official files for every fund that has any, and the backup only for funds with none, newest
           month first, within the backup lookback.
        3. The backup for whichever fund lacks that specific month, even one with official data in another month.
        Each fund-month is official or backup, never both, and a valid official portfolio is never replaced.
        If no month suits every fund, the month common to every fund that can be compared at all.
        """
        supported = list(links_by_code)
        official_months = [
            month for month in sorted({link['month'] for code in supported for link in links_by_code[code] if not is_backup(link)}, reverse=True)
            if sum(month in official_months_of(code) for code in supported) >= 2
        ]
        for month in official_months:
            valid = {code: result for code in supported if month in official_months_of(code) and (result := load(code, month))}
            valid_by_month[month] = valid
            if len(supported) >= 2 and len(valid) == len(supported):
                return supported, month
        if len(supported) >= 2 and charlie_alpha.enabled():
            backup_only = {code for code in supported if not has_official_data(code)}
            for allowed in (backup_only, set(supported)):
                for month in sorted(backup_window, reverse=True):
                    valid = dict(valid_by_month.get(month, {}))
                    valid.update({code: result for code in supported if code not in valid and (result := load(code, month))})
                    missing = [code for code in supported if code not in valid]
                    # Check everything that costs nothing before any portfolio is requested.
                    if any(code not in allowed for code in missing) or not all(backup_has(code, month) for code in missing):
                        valid_by_month[month] = valid
                        continue
                    for code in missing:
                        result = load(code, month, backup=True)
                        if not result:
                            break
                        valid[code] = result
                    valid_by_month[month] = valid
                    if len(valid) == len(supported):
                        return supported, month
        # No month suits every fund that has files, but a fund whose files all failed, or that shares
        # no month with the rest, cannot be compared at all. Skip such funds and look again for a month
        # common to every fund that can be. Every month is already loaded here, so this costs nothing.
        evaluable = {code for valid in valid_by_month.values() for code in valid}
        if len(evaluable) >= 2:
            return supported, next((month for month in sorted(valid_by_month, reverse=True) if evaluable <= set(valid_by_month[month])), None)
        return supported, None

    def own_months(supported: list[str]) -> dict[str, tuple[str, dict[str, Any]]]:
        # A fund with no month in common is still a fund with valid holdings. Read its own latest
        # month so it is reported available, and leave "no common month" to the pair it affects.
        own = {}
        for code in supported:
            if any(code in valid for valid in valid_by_month.values()):
                continue
            months = sorted({link['month'] for link in links_by_code[code]}, reverse=True)
            result = next(((month, data) for month in months if (data := load(code, month, comparing=False) or load(code, month, comparing=False, backup=True))), None)
            if result:
                own[code] = result
        return own

    def fund_month_data(code: str, month: str) -> dict[str, Any] | None:
        """This fund's holdings for one month: what is already loaded, an official file, then the backup."""
        valid = valid_by_month.setdefault(month, {})
        if code not in valid:
            result = load(code, month) or (load(code, month, backup=True) if backup_has(code, month) else None)
            if result:
                valid[code] = result
        return valid.get(code)

    def can_supply(code: str, month: str) -> bool:
        if code in valid_by_month.get(month, {}):
            return True
        return any(not is_backup(link) and link['month'] == month and (code, link['url']) not in failed_links for link in links_by_code[code]) or backup_has(code, month)

    def pair_month(left: str, right: str) -> str | None:
        """The month this pair is compared in. Both funds always use the same month.

        The month common to every fund comes first. Otherwise the pair's newest month with official
        holdings on both sides, and only then its newest month where the backup fills one side.
        """
        if left not in links_by_code or right not in links_by_code:
            return None
        if global_month and left in valid_by_month.get(global_month, {}) and right in valid_by_month[global_month]:
            return global_month
        months = sorted(set(valid_by_month) | {link['month'] for code in (left, right) for link in links_by_code[code]}, reverse=True)
        official = lambda month, code: ((valid_by_month.get(month, {}).get(code) or {}).get('source_type') or charlie_alpha.SOURCE_OFFICIAL) == charlie_alpha.SOURCE_OFFICIAL
        for month in months:
            valid = valid_by_month.get(month, {})
            if left in valid and right in valid and official(month, left) and official(month, right):
                return month
        for month in months:
            if can_supply(left, month) and can_supply(right, month) and fund_month_data(left, month) and fund_month_data(right, month):
                return month
        return None

    supported_codes, global_month = choose_months()
    own_holdings = own_months(supported_codes)
    planned_months = {
        (str(left), str(right)): pair_month(str(left), str(right))
        for index, left in enumerate(scheme_codes) for right in scheme_codes[index + 1:]
    }
    candidate_months = sorted(valid_by_month, reverse=True)

    for fund in fund_results:
        code = fund['scheme_code']
        if code not in links_by_code:
            continue
        best_month = global_month if global_month and code in valid_by_month[global_month] else next(
            (month for month in candidate_months if code in valid_by_month.get(month, {})), None,
        )
        if best_month is None and code in own_holdings:
            best_month = own_holdings[code][0]
        if best_month:
            selected = valid_by_month[best_month][code] if code in valid_by_month.get(best_month, {}) else own_holdings[code][1]
            fund.update({
                'available': True, 'reason': None, 'portfolio_month': best_month,
                'portfolio_date': selected['portfolio_date'],
                'holdings_source': selected.get('source_type') or charlie_alpha.SOURCE_OFFICIAL,
                'holding_count': len(_weights_of(selected, FUND_SECURITY_CLASSES[_asset_class(record_by_code[code])])),
                'asset_class': _asset_class(record_by_code[code]),
                **({'unclassified_pct': round(selected['unclassified_weight'], 2)} if (selected.get('unclassified_weight') or 0) > MATERIAL_UNCLASSIFIED_WEIGHT else {}),
            })
            debug_by_code[code]['chosen_common_month'] = best_month
            debug_by_code[code]['reporting_date'] = selected['portfolio_date']
            debug_by_code[code]['matched_sheet_name'] = selected['matched_sheet_name']
            debug_by_code[code]['parse_result'] = {
                'status': 'ok', 'holding_count': len(selected['holdings']),
                **({'source': selected['source']} if selected['source'] else {}),
            }
            status_by_code[code] = HOLDINGS_AVAILABLE
        else:
            fund['reason'] = 'Overlap unavailable right now'
            debug_by_code[code]['error'] = debug_by_code[code]['error'] or (
                'No valid common-month holdings in the lookback period'
                if candidate_months else 'No portfolio month shared with another selected fund'
            )
            if not debug_by_code[code]['attempted_months']:
                status_by_code[code] = NO_COMMON_MONTH

    for fund in fund_results:
        code = fund['scheme_code']
        status = status_by_code.get(code) or classify_error(debug_by_code[code]['error'] or 'No monthly portfolio files')
        fund['status'] = status
        debug_by_code[code]['status'] = status
        # No common month says nothing about the source: its file wasn't even opened,
        # so it must not make the next request wait a day before trying it.
        if code in checked_sources and status != NO_COMMON_MONTH:
            record_status(record_by_code[code], status, debug_by_code[code]['error'] or '', fund.get('portfolio_month'))
        if code in record_by_code:
            state = scheme_status(record_by_code[code])
            fund.update({'scheme_status': state['status'], 'last_nav_date': state['last_nav_date']})
        fund['reason_code'] = _fund_reason_code(fund)
        fund['status_message'] = REASON_MESSAGES.get(fund['reason_code']) if fund['reason_code'] else None
        if historical_mode and not fund.get('available') and fund['reason_code'] != REASON_HISTORICAL_UNAVAILABLE:
            # The month asked for has no verified portfolio. The source status says why, if it knows.
            fund.update({'source_status': status, 'reason_code': REASON_HISTORICAL_UNAVAILABLE, 'reason': historical_message([fund.get('scheme_name') or code]), 'status_message': REASON_MESSAGES[REASON_HISTORICAL_UNAVAILABLE]})
            fund['status'] = REASON_HISTORICAL_UNAVAILABLE
        if fund['reason_code'] == REASON_HISTORICAL_UNAVAILABLE:
            fund['message'] = historical_message([fund.get('scheme_name') or code])
        elif code in backup_tried and not fund.get('available'):
            # The official reader could not supply it and neither could the backup. Say so plainly.
            fund['backup_attempted'] = True
            fund['message'] = ALL_SOURCES_UNAVAILABLE

    report('calculating')
    pairs = []
    unavailable_pairs = []
    # Built from every AMFI record, so only when a Fund of Funds pair actually needs it.
    share_class_identities: dict[str, str] | None = None
    for left_index, left_code in enumerate(scheme_codes):
        for right_code in scheme_codes[left_index + 1:]:
            left_code, right_code = str(left_code), str(right_code)
            pair_month = planned_months.get((left_code, right_code))
            # A pair is compared only in one month that both funds reported.
            if not pair_month or left_code not in valid_by_month[pair_month] or right_code not in valid_by_month[pair_month]:
                continue
            left_data = valid_by_month[pair_month][left_code]
            right_data = valid_by_month[pair_month][right_code]
            left_record = record_by_code[left_code]
            right_record = record_by_code[right_code]
            left_is_fof, right_is_fof = _asset_class(left_record) == ASSET_FOF, _asset_class(right_record) == ASSET_FOF
            if left_is_fof != right_is_fof:
                # A look-through needs the underlying funds' own holdings, retrieved and validated.
                # We hold only the fund units, so a figure here would be invented.
                unavailable_pairs.append({'left': left_code, 'right': right_code, 'portfolio_month': pair_month, 'reason': REASON_FOF_LOOKTHROUGH})
                continue
            classes = {ASSET_FUND_UNIT} if left_is_fof else _eligible_classes(_asset_class(left_record), _asset_class(right_record))
            for code, data, record in ((left_code, left_data, left_record), (right_code, right_data, right_record)):
                fresh = _ensure_classes(classes, code, pair_month, data, source_by_code[code], name_by_code[code], debug_by_code[code], downloaded, workbooks, record)
                valid_by_month[pair_month][code] = fresh
            left_data, right_data = valid_by_month[pair_month][left_code], valid_by_month[pair_month][right_code]
            # Without the rows of a kind we can't say the funds share none of it, so 0% would mislead.
            if not (classes <= set(left_data.get('classes_read') or []) and classes <= set(right_data.get('classes_read') or [])):
                unavailable_pairs.append({'left': left_code, 'right': right_code, 'portfolio_month': pair_month, 'reason': REASON_HOLDINGS_UNAVAILABLE})
                continue
            if left_is_fof and share_class_identities is None:
                share_class_identities = _share_class_identities(latest_records)
            comparison = _fof_pair_overlap(left_data, right_data, share_class_identities) if left_is_fof else _pair_overlap(left_data, right_data, classes)
            overlap_pct = comparison['overlap_pct']
            # Fund units are never part of a stock comparison, or of overlap_pct itself.
            fund_units = _common_fund_units(left_data, right_data) if ASSET_DEBT in classes else []
            pairs.append({
                'left': {'scheme_code': left_code, 'scheme_name': left_record.get('scheme_name')},
                'right': {'scheme_code': right_code, 'scheme_name': right_record.get('scheme_name')},
                **comparison,
                'incomplete_coverage': any((data.get('unclassified_weight') or 0) > MATERIAL_UNCLASSIFIED_WEIGHT for data in (left_data, right_data)),
                'portfolio_month': pair_month,
                'portfolio_date': left_data['portfolio_date'] if left_data['portfolio_date'] == right_data['portfolio_date'] else None,
                'left_portfolio_date': left_data['portfolio_date'],
                'right_portfolio_date': right_data['portfolio_date'],
                'left_source': left_data.get('source_type') or charlie_alpha.SOURCE_OFFICIAL,
                'right_source': right_data.get('source_type') or charlie_alpha.SOURCE_OFFICIAL,
                'holdings_source': _pair_source(left_data, right_data),
                # COMMON_TO_ALL: the one month every comparable fund reported. PAIRWISE: no such month
                # exists, so this pair uses the latest month its own two funds share.
                'month_basis': 'COMMON_TO_ALL' if global_month and pair_month == global_month else 'PAIRWISE',
                # A second figure for reference. overlap_pct above never includes fund units.
                **({'including_fund_units': {
                    'overlap_pct': round(overlap_pct + sum(min(unit['left_weight'], unit['right_weight']) for unit in fund_units), 2),
                    'fund_units': fund_units,
                }} if fund_units else {}),
            })

    # Direct FoF overlap is a different measure from security overlap, so it never outranks one.
    # It heads the summary only when it is the sole kind of pair.
    # Every pair without a figure says why, so one fund's problem never hides as another's.
    fund_by_code = {fund['scheme_code']: fund for fund in fund_results}
    compared = {(pair['left']['scheme_code'], pair['right']['scheme_code']) for pair in pairs}
    explicit = {(item['left'], item['right']): item for item in unavailable_pairs}
    pair_reasons = []
    for left_index, left_code in enumerate(scheme_codes):
        for right_code in scheme_codes[left_index + 1:]:
            key = (str(left_code), str(right_code))
            if key in compared:
                continue
            if key in explicit:
                pair_reasons.append({'left': key[0], 'right': key[1], 'reason_code': explicit[key]['reason'], 'message': REASON_MESSAGES[explicit[key]['reason']]})
            else:
                gap = _pair_gap(fund_by_code[key[0]], fund_by_code[key[1]])
                gap = {name: value for name, value in gap.items() if name != 'reason'}
                if any(fund_by_code[code].get('backup_attempted') for code in key) and gap['reason_code'] not in (REASON_NO_COMMON_MONTH, REASON_HISTORICAL_UNAVAILABLE):
                    gap['message'] = ALL_SOURCES_UNAVAILABLE
                pair_reasons.append(gap)
    ranked_pairs = [pair for pair in pairs if pair.get('overlap_type') != OVERLAP_DIRECT_FOF] or pairs
    highest_pair = max(ranked_pairs, key=lambda pair: pair['overlap_pct'], default=None)
    pair_months = {pair['portfolio_month'] for pair in pairs}
    pairs_different_months = len(pair_months) > 1
    selected_month = global_month or (next(iter(pair_months)) if len(pair_months) == 1 else None)
    rounded_pairs = [{**pair, 'overlap_pct': round(pair['overlap_pct'], 2)} for pair in pairs]
    if highest_pair is not None:
        highest_pair = {**highest_pair, 'overlap_pct': round(highest_pair['overlap_pct'], 2)}
    total_pairs = len(scheme_codes) * (len(scheme_codes) - 1) // 2
    selected_records = [record_by_code[str(code)] for code in scheme_codes if str(code) in record_by_code]
    has_debt = any(_asset_class(record) != ASSET_EQUITY for record in selected_records)
    reason_code = _overlap_reason(highest_pair, fund_results, unavailable_pairs)
    if highest_pair:
        # Equity selections keep showing a plain 0.00%.
        highest_message = REASON_MESSAGES[reason_code] if has_debt and reason_code else None
    elif reason_code == REASON_HISTORICAL_UNAVAILABLE:
        highest_message = historical_message([fund.get('scheme_name') or fund['scheme_code'] for fund in fund_results if fund.get('reason_code') == REASON_HISTORICAL_UNAVAILABLE])
    else:
        highest_message = REASON_MESSAGES[reason_code] if has_debt else 'Portfolio holdings unavailable'
    if not highest_pair and backup_tried and reason_code in (REASON_HOLDINGS_UNAVAILABLE, REASON_PARSER_UNSUPPORTED, REASON_NO_COMMON_MONTH):
        highest_message = ALL_SOURCES_UNAVAILABLE
    highest_overlap = {
        'available': highest_pair is not None,
        'percentage': round(highest_pair['overlap_pct'], 2) if highest_pair else None,
        'fund_a_code': highest_pair['left']['scheme_code'] if highest_pair else None,
        'fund_a_name': highest_pair['left']['scheme_name'] if highest_pair else None,
        'fund_b_code': highest_pair['right']['scheme_code'] if highest_pair else None,
        'fund_b_name': highest_pair['right']['scheme_name'] if highest_pair else None,
        'portfolio_month': highest_pair['portfolio_month'] if highest_pair else None,
        'portfolio_date': highest_pair['portfolio_date'] if highest_pair else None,
        'pairs_different_months': pairs_different_months,
        'partial_coverage': bool(highest_pair and len(pairs) < total_pairs),
        'compared_pairs': len(pairs),
        'total_pairs': total_pairs,
        'month_basis': highest_pair.get('month_basis') if highest_pair else None,
        'holdings_source': highest_pair.get('holdings_source') if highest_pair else None,
        'message': highest_message,
        'reason_code': reason_code,
        'overlap_type': highest_pair.get('overlap_type', OVERLAP_SECURITIES) if highest_pair else None,
        'overlap_label': highest_pair.get('overlap_label') if highest_pair else None,
    }
    debug = [debug_by_code[str(code)] for code in scheme_codes]
    logger.debug('Holdings overlap diagnostics: %s', debug)
    if highest_pair is not None:
        overall_status = HOLDINGS_AVAILABLE
    elif sum(1 for fund in fund_results if fund.get('available')) >= 2:
        overall_status = NO_COMMON_MONTH
    else:
        overall_status = next((fund['status'] for fund in fund_results if fund['status'] != HOLDINGS_AVAILABLE), NO_COMMON_MONTH)
    return {
        'portfolio_month': selected_month,
        # The one month every comparable fund reported, if there is one. None means pairs use their own months.
        'common_month': global_month,
        'pair_months': sorted(pair_months, reverse=True),
        'compared_pairs': len(pairs),
        'total_pairs': total_pairs,
        'partial_coverage': 0 < len(pairs) < total_pairs,
        # Which kinds of source the figures came from. A third-party source is always named.
        'holdings_sources': sorted({fund['holdings_source'] for fund in fund_results if fund.get('holdings_source')}),
        'backup_enabled': charlie_alpha.enabled(),
        # The months each source was allowed to supply, newest first.
        'lookback': {
            'official_months': sorted(recent_months, reverse=True),
            'backup_months': sorted(backup_window, reverse=True) if charlie_alpha.enabled() else [],
        },
        'funds': fund_results,
        'pairs': rounded_pairs,
        'unavailable_pairs': unavailable_pairs,
        'pair_reasons': pair_reasons,
        'mode': 'historical' if historical_mode else 'current',
        'requested_month': portfolio_month,
        'pairs_different_months': pairs_different_months,
        'highest_pair': highest_pair,
        'highest_overlap': highest_overlap,
        'available': highest_pair is not None,
        'status': overall_status,
        'debug': debug,
    }
