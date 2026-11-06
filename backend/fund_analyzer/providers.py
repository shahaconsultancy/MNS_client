from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html import unescape
from io import BytesIO
from threading import BoundedSemaphore
from time import perf_counter, sleep
from typing import Any
from urllib.parse import urlsplit

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

AMFI_NAV_URL = 'https://portal.amfiindia.com/spages/NAVAll.txt'
AMFI_HISTORY_DOWNLOAD_URL = 'https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx'
AMFI_HISTORY_URL = 'https://www.amfiindia.com/api/nav-history'
# After AMFI history fails for a scheme, go straight to the backup for this long instead of waiting again.
AMFI_HISTORY_RETRY_SECONDS = 15 * 60
AMFI_TER_URL = 'https://www.amfiindia.com/api/populate-te-rdata-revised'
AMFI_TER_PAGE_URL = 'https://www.amfiindia.com/ter-of-mf-schemes'
AMFI_DISCLOSURE_DIRECTORY_URL = 'https://www.amfiindia.com/online-center/portfolio-disclosure'
AMFI_AUM_URL = 'https://portal.amfiindia.com/AUMReport_Rpt_Po.aspx'
MFAPI_URL = 'https://api.mfapi.in/mf'
HEADERS = {'User-Agent': 'MNS-Fund-Analyzer/1.0', 'Accept': '*/*'}
CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 8
MAX_PROVIDER_ATTEMPTS = 2
AUM_LOOKBACK_QUARTERS = 2
TER_LOOKBACK_MONTHS = 3
TER_THROTTLE_RETRY_SECONDS = 3
TER_COLUMNS = ('nsdl scheme code', 'scheme name', 'ter date', 'regular plan - total ter (%)', 'direct plan - total ter (%)')
MAX_CONCURRENT_PROVIDER_REQUESTS = 4
_provider_request_slots = BoundedSemaphore(MAX_CONCURRENT_PROVIDER_REQUESTS)


class ProviderError(RuntimeError):
    pass


def fetch_url(url: str, *, params: dict[str, Any] | None = None, timeout: tuple[int, int] = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS), attempts: int = MAX_PROVIDER_ATTEMPTS) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        started = perf_counter()
        try:
            with _provider_request_slots:
                response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            logger.info('Fund Analyzer provider %s succeeded in %.2fs (attempt %d)', urlsplit(url).netloc, perf_counter() - started, attempt + 1)
            return response
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning('Fund Analyzer provider %s failed in %.2fs (attempt %d/%d): %s', urlsplit(url).netloc, perf_counter() - started, attempt + 1, attempts, exc)
            if attempt + 1 < attempts:
                continue
    raise ProviderError(str(last_error) if last_error else 'Provider request failed')


def _cached_provider_value(key: str, loader, *, ttl: int, stale_ttl: int) -> Any:
    """Use shared cached provider data and fall back to stale data on outages."""
    stale_key = f'{key}:stale'
    lock_key = f'{key}:refresh-lock'
    try:
        value = cache.get(key)
        if value is not None:
            return value
    except Exception:
        logger.exception('Fund Analyzer cache read failed for %s', key)

    owns_refresh = False
    try:
        owns_refresh = cache.add(lock_key, True, timeout=30)
        if not owns_refresh:
            stale_value = cache.get(stale_key)
            if stale_value is not None:
                logger.info('Serving cached Fund Analyzer data during refresh for %s', key)
                return stale_value
    except Exception:
        logger.exception('Fund Analyzer cache refresh lock failed for %s', key)

    try:
        value = loader()
    except ProviderError:
        try:
            stale_value = cache.get(stale_key)
            if stale_value is not None:
                logger.warning('Serving stale Fund Analyzer provider data for %s', key)
                return stale_value
        except Exception:
            logger.exception('Fund Analyzer stale cache read failed for %s', key)
        raise
    finally:
        if owns_refresh:
            try:
                cache.delete(lock_key)
            except Exception:
                logger.exception('Fund Analyzer cache refresh lock cleanup failed for %s', key)

    try:
        cache.set(key, value, timeout=ttl)
        cache.set(stale_key, value, timeout=stale_ttl)
    except Exception:
        logger.exception('Fund Analyzer cache write failed for %s', key)
    return value


def _clean(value: Any) -> str | None:
    value = str(value or '').strip()
    return value if value and value != '-' else None


def _category_from_header(value: str) -> tuple[str | None, str | None]:
    match = re.match(r'(?P<type>Open|Close|Interval)\s+Ended\s+Schemes?\s*\(\s*(?P<category>.*?)\s*\)', value, re.I)
    if not match:
        return None, None
    return match.group('type').title(), match.group('category') or None


def parse_amfi_text_data(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    amc: str | None = None
    scheme_type: str | None = None
    category: str | None = None
    for raw in text.splitlines():
        line = raw.strip().lstrip('\ufeff')
        if not line:
            continue
        fields = [field.strip() for field in line.split(';')]
        if len(fields) == 1:
            next_type, next_category = _category_from_header(line)
            if next_type:
                scheme_type, category = next_type, next_category
            elif not line.lower().startswith(('scheme code', 'net asset value')):
                amc = line
            continue
        if not fields[0].isdigit():
            continue
        try:
            if len(fields) >= 8:
                code, isin_growth, isin_reinvest, name, plan, option, nav, nav_date = fields[:8]
            elif len(fields) == 6:
                code, isin_growth, isin_reinvest, name, nav, nav_date = fields
                plan = option = ''
            else:
                continue
            value = float(nav.replace(',', ''))
            if value <= 0:
                continue
            records.append({
                'scheme_code': code, 'isin': _clean(isin_growth) or _clean(isin_reinvest), 'isin_reinvest': _clean(isin_reinvest),
                'scheme_name': name, 'plan': _clean(plan), 'option': _clean(option), 'nav': value,
                'date': nav_date, 'amc': amc, 'scheme_type': scheme_type, 'category': category,
            })
        except (TypeError, ValueError):
            continue
    if not records:
        raise ProviderError('AMFI NAV data was empty or invalid')
    return records


def fetch_amfi_latest() -> list[dict[str, Any]]:
    def load() -> list[dict[str, Any]]:
        response = fetch_url(AMFI_NAV_URL)
        return parse_amfi_text_data(response.text)

    return _cached_provider_value('fund-analyzer:amfi:latest', load, ttl=1800, stale_ttl=86400)


def parse_amfi_latest_nav_excel(payload: bytes) -> list[dict[str, Any]]:
    """Compatibility parser for an AMFI latest-NAV spreadsheet payload."""
    import pandas as pd
    from io import BytesIO

    frame = pd.read_excel(BytesIO(payload))
    columns = {str(column).strip().lower(): column for column in frame.columns}
    code_column = next((columns[key] for key in columns if 'scheme code' in key or key == 'code'), None)
    name_column = next((columns[key] for key in columns if 'scheme name' in key or key == 'name'), None)
    nav_column = next((columns[key] for key in columns if key == 'net asset value' or key == 'nav'), None)
    date_column = next((columns[key] for key in columns if key == 'date' or 'nav date' in key), None)
    if not all((code_column, name_column, nav_column, date_column)):
        raise ProviderError('AMFI latest NAV workbook columns were not recognised')
    result = []
    for row in frame.to_dict('records'):
        try:
            result.append({'scheme_code': str(row[code_column]), 'scheme_name': str(row[name_column]), 'nav': float(row[nav_column]), 'date': str(row[date_column])})
        except (TypeError, ValueError):
            continue
    if not result:
        raise ProviderError('AMFI latest NAV workbook was empty')
    return result


def _history_rows(payload: dict[str, Any], scheme_code: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    data = payload.get('data') or {}
    for group in data.get('nav_groups') or []:
        for row in group.get('historical_records') or []:
            try:
                value = float(row.get('nav'))
                if value > 0:
                    rows.append({'scheme_code': scheme_code, 'date': row.get('date'), 'nav': value})
            except (TypeError, ValueError):
                continue
    return rows


def fetch_amfi_history(scheme_code: str, start: date, end: date) -> list[dict[str, Any]]:
    def load() -> list[dict[str, Any]]:
        chunks: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=1790))
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)

        def fetch_chunk(bounds: tuple[date, date]) -> list[dict[str, Any]]:
            chunk_start, chunk_end = bounds
            response = fetch_url(AMFI_HISTORY_URL, params={'query_type': 'historical_period', 'from_date': chunk_start.isoformat(), 'to_date': chunk_end.isoformat(), 'sd_id': scheme_code}, timeout=(2, 8), attempts=1)
            chunk = _history_rows(response.json(), scheme_code)
            if not chunk:
                raise ProviderError(f'AMFI returned no history for scheme {scheme_code}')
            return chunk

        with ThreadPoolExecutor(max_workers=min(2, len(chunks))) as history_pool:
            rows = [row for chunk in history_pool.map(fetch_chunk, chunks) for row in chunk]
        unique = {(row['date'], row['nav']): row for row in rows}
        return sorted(unique.values(), key=lambda row: row['date'])

    key = f'fund-analyzer:amfi:history:{scheme_code}:{start.isoformat()}:{end.isoformat()}'
    failed_key = f'fund-analyzer:amfi:history-failed:{scheme_code}'
    try:
        if cache.get(failed_key):
            raise ProviderError(f'AMFI history for scheme {scheme_code} failed recently; using the backup source')
    except ProviderError:
        raise
    except Exception:
        logger.exception('Fund Analyzer cache read failed for %s', failed_key)
    try:
        return _cached_provider_value(key, load, ttl=86400, stale_ttl=604800)
    except ProviderError:
        try:
            cache.set(failed_key, True, timeout=AMFI_HISTORY_RETRY_SECONDS)
        except Exception:
            logger.exception('Fund Analyzer cache write failed for %s', failed_key)
        raise


def parse_amfi_history_text_data(text: str, scheme_code: str) -> list[dict[str, Any]]:
    """Parse AMFI's downloadable semicolon history format for one scheme."""
    rows = []
    for raw in text.splitlines():
        fields = [field.strip() for field in raw.strip().split(';')]
        if len(fields) < 8 or fields[0] != str(scheme_code):
            continue
        try:
            nav = float(next(value for value in (fields[4], fields[6]) if value not in ('', '-')).replace(',', ''))
            if nav > 0:
                rows.append({'scheme_code': str(scheme_code), 'date': fields[7], 'nav': nav})
        except (TypeError, ValueError):
            continue
    return sorted(rows, key=lambda row: row['date'])


def fetch_mfapi_data(scheme_code: str, latest: bool = False) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        suffix = '/latest' if latest else ''
        response = fetch_url(f'{MFAPI_URL}/{scheme_code}{suffix}')
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderError('MFAPI returned invalid JSON')
        return payload

    return _cached_provider_value(f'fund-analyzer:mfapi:{scheme_code}:{latest}', load, ttl=86400, stale_ttl=604800)


def parse_mfapi(payload: dict[str, Any], scheme_code: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta = payload.get('meta') or {}
    rows = []
    for item in payload.get('data') or []:
        try:
            raw_date = str(item.get('date') or '')
            parsed_date = None
            for pattern in ('%d-%m-%Y', '%d-%b-%Y', '%Y-%m-%d'):
                try:
                    parsed_date = date.fromisoformat(raw_date) if pattern == '%Y-%m-%d' else datetime.strptime(raw_date, pattern).date()
                    break
                except ValueError:
                    continue
            rows.append({'scheme_code': scheme_code, 'date': parsed_date.isoformat() if parsed_date else raw_date, 'nav': float(item.get('nav'))})
        except (TypeError, ValueError):
            continue
    if not rows:
        raise ProviderError(f'MFAPI returned no NAV history for scheme {scheme_code}')
    plan, option = infer_plan_option(meta.get('scheme_name'))
    info = {'scheme_code': scheme_code, 'scheme_name': meta.get('scheme_name'), 'amc': meta.get('fund_house'), 'category': meta.get('scheme_category'), 'isin': meta.get('isin_div_payout'), 'plan': plan, 'option': option}
    return info, sorted(rows, key=lambda row: row['date'])


def infer_plan_option(name: Any) -> tuple[str | None, str | None]:
    """Plan (Direct or Regular) and option (Growth or IDCW) as a scheme's own name states them.

    Each plan and option has its own scheme code and its own NAV history, so a name that
    says Regular IDCW is not the Direct Growth fund. None means the name does not say.
    """
    text = str(name or '').lower()
    plan = 'Direct Plan' if re.search(r'\bdirect\b', text) else 'Regular Plan' if re.search(r'\bregular\b', text) else None
    option = 'IDCW' if re.search(r'idcw|dividend|payout|reinvest', text) else 'Growth' if re.search(r'\bgrowth\b', text) else None
    return plan, option


def plan_option_conflict(first: dict[str, Any], second: dict[str, Any]) -> str | None:
    """Describe a disagreement between two records of one scheme code, or None if they agree or one is silent."""
    def read(record: dict[str, Any]) -> tuple[str | None, str | None]:
        # The structured field when the source has one, else what the name says.
        from_fields = infer_plan_option(f"{record.get('plan') or ''} {record.get('option') or ''}")
        from_name = infer_plan_option(record.get('scheme_name'))
        return from_fields[0] or from_name[0], from_fields[1] or from_name[1]

    one, two = read(first), read(second)
    for index, label in enumerate(('plan', 'option')):
        if one[index] and two[index] and one[index] != two[index]:
            return f'{label} differs between sources ({one[index]} and {two[index]})'
    return None


def _ter_amc_ids() -> dict[str, str]:
    """AMFI publishes its MF_ID list only inside the official TER page's data. The AUM report uses the same ids."""
    def load() -> dict[str, str]:
        page = fetch_url(AMFI_TER_PAGE_URL, timeout=(2, 15), attempts=1).text
        ids = {name: mf_id for mf_id, name in re.findall(r'\\"mfId\\":\\"(\d+)\\",\\"mfName\\":\\"([^"\\]+)\\"', page)}
        if not ids:
            raise ProviderError('AMFI TER page did not list any mutual funds')
        return ids

    return _cached_provider_value('fund-analyzer:amfi:ter-amc-ids', load, ttl=604800, stale_ttl=2592000)


def fetch_amfi_disclosure_directory() -> dict[str, str]:
    """AMFI's official list of each AMC's monthly portfolio page, keyed by AMFI AMC name.

    AMFI links out to the AMCs here. It does not host the holdings files itself.
    """
    def load() -> dict[str, str]:
        page = fetch_url(AMFI_DISCLOSURE_DIRECTORY_URL, timeout=(3, 20), attempts=1).text.replace('\\"', '"')
        directory = {}
        for match in re.finditer(r'"amc_monthly_portfolio_disclosure":"([^"]*)"', page):
            names = re.findall(r'"mf_name":"([^"]+)"', page[page.rfind('{', 0, match.start()):match.start()])
            if names and match.group(1).startswith('http'):
                directory[_decode_json_text(names[0])] = _decode_json_text(match.group(1))
        if not directory:
            raise ProviderError('AMFI portfolio disclosure directory could not be read')
        return directory

    return _cached_provider_value('fund-analyzer:amfi:disclosure-directory', load, ttl=604800, stale_ttl=2592000)


def _decode_json_text(value: str) -> str:
    return re.sub(r'\\+u([0-9a-fA-F]{4})', lambda match: chr(int(match.group(1), 16)), value).replace('\\/', '/')


def _ter_months() -> list[str]:
    """Newest first, in AMFI's MM-YYYY format; AMCs publish a new month at different times."""
    cursor = date.today().replace(day=1)
    months = []
    for _ in range(TER_LOOKBACK_MONTHS):
        months.append(cursor.strftime('%m-%Y'))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return months


def _ter_name_key(name: Any) -> str:
    return re.sub(r'\s+', ' ', str(name or '')).strip().casefold()


def _ter_percent(value: Any) -> float | None:
    """Return the disclosed total. A blank or zero total means no disclosure, not a free fund."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _ter_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    try:
        return date.fromisoformat(str(value or '')[:10]).isoformat()
    except ValueError:
        return None


def _parse_ter_workbook(payload: bytes) -> dict[str, dict[str, tuple[str, float | None, float | None]]]:
    """Read AMFI's TER download into {scheme name: {NSDL code: (date, regular, direct)}}.

    The workbook has one row per scheme per day, and its Total TER columns already
    include BER, brokerage, transaction cost and statutory levies.
    """
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
        rows = list(workbook.worksheets[0].values)
        workbook.close()
    except Exception as exc:
        raise ProviderError('AMFI TER workbook could not be read') from exc
    if len(rows) < 2:
        return {}
    headers = {str(value or '').strip().lower(): index for index, value in enumerate(rows[0])}
    try:
        code_at, name_at, date_at, regular_at, direct_at = (headers[column] for column in TER_COLUMNS)
    except KeyError as exc:
        raise ProviderError('AMFI TER workbook columns were not recognised') from exc

    table: dict[str, dict[str, tuple[str, float | None, float | None]]] = {}
    for raw_row in rows[1:]:
        # Read-only workbooks drop trailing blank cells, so a short row can still be a real row.
        row = [*raw_row, *([None] * (len(rows[0]) - len(raw_row)))]
        reporting_date = _ter_date(row[date_at])
        # AMFI's footnote rows have no scheme code or date.
        if not reporting_date or not row[code_at]:
            continue
        schemes = table.setdefault(_ter_name_key(row[name_at]), {})
        code = str(row[code_at])
        if code not in schemes or reporting_date >= schemes[code][0]:
            schemes[code] = (reporting_date, _ter_percent(row[regular_at]), _ter_percent(row[direct_at]))
    return table


def _fetch_ter_table(mf_id: str, month: str) -> dict[str, dict[str, tuple[str, float | None, float | None]]]:
    def load() -> dict[str, dict[str, tuple[str, float | None, float | None]]]:
        # The JSON form caps pageSize at 100 and paging it gets a 429; the official
        # Excel download returns the whole AMC month in one request.
        params = {'MF_ID': mf_id, 'Month': month, 'strCat': '-1', 'strType': '-1', 'excel': 'true'}
        try:
            response = fetch_url(AMFI_TER_URL, params=params, timeout=(2, 20), attempts=1)
        except ProviderError as exc:
            if '429' not in str(exc):
                raise
            sleep(TER_THROTTLE_RETRY_SECONDS)
            response = fetch_url(AMFI_TER_URL, params=params, timeout=(2, 20), attempts=1)
        return _parse_ter_workbook(response.content)

    return _cached_provider_value(f'fund-analyzer:amfi:ter:{mf_id}:{month}', load, ttl=21600, stale_ttl=604800)


def _ter_plan(record: dict[str, Any]) -> str | None:
    text = str(record.get('plan') or record.get('scheme_name') or '').lower()
    if 'direct' in text:
        return 'direct'
    return 'regular' if 'regular' in text else None


def _ter_for_record(record: dict[str, Any] | None) -> tuple[float | None, str | None]:
    """Latest official Total TER for this scheme and plan, or (None, None) if unsure."""
    plan = _ter_plan(record) if record else None
    if not plan:
        return None, None
    try:
        mf_id = _ter_amc_ids().get(record.get('amc') or '')
        if not mf_id:
            return None, None
        for month in _ter_months():
            schemes = _fetch_ter_table(mf_id, month).get(_ter_name_key(record.get('scheme_name')))
            if not schemes:
                continue
            latest = max(row[0] for row in schemes.values())
            current = [row for row in schemes.values() if row[0] == latest]
            if len(current) > 1:
                logger.warning('AMFI TER has %d schemes named %s; not guessing', len(current), record.get('scheme_name'))
                return None, None
            reporting_date, regular, direct = current[0]
            value = direct if plan == 'direct' else regular
            return (value, reporting_date) if value is not None else (None, None)
    except ProviderError as exc:
        logger.warning('AMFI TER unavailable for scheme %s: %s', record.get('scheme_code'), exc)
    return None, None


def fetch_amfi_ter_snapshot(scheme_codes: list[str], latest_records: list[dict[str, Any]] | None = None) -> dict[str, tuple[float | None, str | None]]:
    """Return {scheme code: (TER %, reporting date)} for the requested schemes."""
    records = latest_records if latest_records is not None else fetch_amfi_latest()
    record_by_code = {str(record.get('scheme_code')): record for record in records}
    codes = [str(code) for code in scheme_codes]
    if not codes:
        return {}
    try:
        _ter_amc_ids()
    except ProviderError as exc:
        logger.warning('AMFI TER mutual fund list unavailable: %s', exc)
        return {code: (None, None) for code in codes}
    # Funds of one AMC share a workbook, so load them in turn: the first fills the
    # cache for the rest. AMFI answers parallel requests for the same file with a 429.
    codes_by_amc: dict[Any, list[str]] = {}
    for code in codes:
        codes_by_amc.setdefault((record_by_code.get(code) or {}).get('amc'), []).append(code)

    def load_amc(amc_codes: list[str]) -> dict[str, tuple[float | None, str | None]]:
        return {code: _ter_for_record(record_by_code.get(code)) for code in amc_codes}

    result: dict[str, tuple[float | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=min(2, len(codes_by_amc))) as pool:
        for values in pool.map(load_amc, codes_by_amc.values()):
            result.update(values)
    return result


def fetch_amfi_ter(scheme_code: str, latest_records: list[dict[str, Any]], ter_snapshot: dict[str, tuple[float | None, str | None]] | None = None) -> tuple[float | None, str | None]:
    """Fetch current TER for one scheme, returning null when AMFI has no match."""
    snapshot = ter_snapshot if ter_snapshot is not None else fetch_amfi_ter_snapshot([scheme_code], latest_records)
    return snapshot.get(str(scheme_code), (None, None))


def _aum_quarters() -> list[tuple[date, date]]:
    """Completed quarters as (first day, last day), newest first."""
    today = date.today()
    cursor = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    quarters = []
    for _ in range(AUM_LOOKBACK_QUARTERS):
        last_day = cursor - timedelta(days=1)
        first_day = date(last_day.year, last_day.month - 2, 1)
        quarters.append((first_day, last_day))
        cursor = first_day
    return quarters


def _parse_aum_report(text: str, amc_name: str) -> list[tuple[str, float]]:
    """Read (plan name, AAUM in lakhs) rows, only if the report is for this AMC."""
    rows = [
        [re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', '', cell))).strip() for cell in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S | re.I)]
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.S | re.I)
    ]
    if not any(len(row) == 1 and row[0].casefold() == amc_name.casefold() for row in rows):
        return []
    plans = []
    for row in rows:
        if len(row) != 3:
            continue
        try:
            # The two columns are "excluding FoF - Domestic" and "FoF - Domestic".
            plans.append((row[0], float(row[1].replace(',', '')) + float(row[2].replace(',', ''))))
        except ValueError:
            continue
    return plans


def _scheme_aum(plans: list[tuple[str, float]], scheme_name: str) -> float | None:
    """Add up one scheme's plans. The report has a row per plan and option."""
    wanted = _ter_name_key(scheme_name)
    if not wanted:
        return None
    # After the scheme name comes the plan, written as "- Direct", "(Direct)" or "Direct".
    pattern = re.compile(re.escape(wanted) + r'\s*(?:-|\(|(?:direct|regular|growth|idcw|dividend)\b)')
    total = sum(value for name, value in plans if _ter_name_key(name) == wanted or pattern.match(_ter_name_key(name)))
    return total if total > 0 else None


def fetch_amfi_aum(scheme_name: str, amc_name: str) -> tuple[float | None, str | None]:
    """Latest quarterly AAUM for the whole scheme, in lakhs, with its quarter end date."""
    try:
        mf_id = _ter_amc_ids().get(amc_name)
        if not mf_id:
            return None, None
        for first_day, last_day in _aum_quarters():
            def load_report() -> list[tuple[str, float]]:
                # mf=0 is a 6 MB report that takes 20s or more; one AMC takes about 2s.
                params = {'dtAUM': first_day.strftime('%d-%b-%Y'), 'mf': mf_id, 'qt': f'{first_day.strftime("%B")} - {last_day.strftime("%B")} {first_day.year}', 'rpt': 'scwise'}
                return _parse_aum_report(fetch_url(AMFI_AUM_URL, params=params, timeout=(3, 15), attempts=1).text, amc_name)

            plans = _cached_provider_value(f'fund-analyzer:amfi:aum:{mf_id}:{last_day.isoformat()}', load_report, ttl=86400, stale_ttl=2592000)
            if plans:
                total = _scheme_aum(plans, scheme_name)
                return (total, last_day.isoformat()) if total is not None else (None, None)
    except ProviderError as exc:
        logger.warning('AMFI AUM unavailable for %s: %s', scheme_name, exc)
    return None, None
