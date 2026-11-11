"""Every scheme in AMFI's NAVAll.txt, kept as a validated registry for Fund Analyzer search.

Search never downloads AMFI. It reads this registry, which is refreshed on a schedule:

* A request finds the registry older than FUND_ANALYZER_AMFI_REGISTRY_REFRESH_HOURS and one
  request (holding a shared lock) downloads AMFI again. Everyone else keeps searching the old one.
* `manage.py refresh_amfi_registry` and the admin endpoint refresh on demand.

A downloaded feed replaces the registry only after it validates: enough schemes, enough AMCs,
and no sudden fall in the scheme count. Otherwise the last good registry stays in use and the
rejection is recorded. Storage is a JSON file (written atomically) plus the shared Django cache,
so a restart, a second worker or an AMFI outage still has the last good copy. No database.

Nothing is filtered out: every plan, option, AMC, category, inactive or unpriced scheme code
that AMFI lists is a separate searchable record.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from django.conf import settings
from django.core.cache import cache

from .providers import AMFI_NAV_URL, ProviderError, fetch_url, parse_amfi_text_data, scheme_display_name

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PAYLOAD_KEY = f'fund-analyzer:amfi-registry:v{SCHEMA_VERSION}'
META_KEY = f'{PAYLOAD_KEY}:meta'
LOCK_KEY = f'{PAYLOAD_KEY}:refresh-lock'
BACKOFF_KEY = f'{PAYLOAD_KEY}:backoff'
REFRESH_LOCK_SECONDS = 120
# After a failed or rejected download, wait this long before asking AMFI again.
FAILURE_BACKOFF_SECONDS = 15 * 60
MIN_AMCS = 10


class RegistryRejected(ProviderError):
    """A downloaded feed failed validation and was not used."""


def _setting(name: str, default: float) -> float:
    return float(getattr(settings, name, default))


def refresh_hours() -> float:
    return _setting('FUND_ANALYZER_AMFI_REGISTRY_REFRESH_HOURS', 12)


def min_schemes() -> int:
    return int(_setting('FUND_ANALYZER_AMFI_REGISTRY_MIN_SCHEMES', 5000))


def max_drop_pct() -> float:
    return _setting('FUND_ANALYZER_AMFI_REGISTRY_MAX_DROP_PCT', 10)


def registry_path() -> Path:
    return Path(settings.FUND_ANALYZER_HOLDINGS_CACHE_DIR) / 'amfi' / 'scheme_registry.json'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _nav_day(value: Any) -> str | None:
    try:
        return datetime.strptime(str(value or '')[:11], '%d-%b-%Y').date().isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------- build and validate

def build_registry(text: str, *, previous_count: int | None = None, force: bool = False) -> dict[str, Any]:
    """Parse and validate a NAVAll.txt download. Raises RegistryRejected instead of returning a bad registry."""
    try:
        parsed = parse_amfi_text_data(text, include_unpriced=True)
    except ProviderError as exc:
        raise RegistryRejected(f'AMFI feed could not be parsed: {exc}') from exc
    # One record per scheme code. AMFI does not repeat codes today; if it ever does, keep the newest NAV.
    by_code: dict[str, dict[str, Any]] = {}
    for record in parsed:
        code = record['scheme_code']
        current = by_code.get(code)
        if current is None or (_nav_day(record.get('date')) or '') >= (_nav_day(current.get('date')) or ''):
            by_code[code] = record
    records = list(by_code.values())
    amcs = {record.get('amc') for record in records if record.get('amc')}
    problems = []
    if len(records) < min_schemes():
        problems.append(f'only {len(records)} schemes (minimum {min_schemes()})')
    if len(amcs) < MIN_AMCS:
        problems.append(f'only {len(amcs)} AMCs (minimum {MIN_AMCS})')
    if previous_count and len(records) < previous_count * (1 - max_drop_pct() / 100):
        problems.append(f'scheme count fell from {previous_count} to {len(records)}, more than {max_drop_pct():g}%')
    if problems and not force:
        raise RegistryRejected('AMFI feed rejected: ' + '; '.join(problems))
    nav_days = [day for day in (_nav_day(record.get('date')) for record in records) if day]
    return {
        'schema': SCHEMA_VERSION,
        'version': hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16],
        'fetched_at': _now().isoformat(timespec='seconds'),
        'source_url': AMFI_NAV_URL,
        'as_of': max(nav_days) if nav_days else None,
        'count': len(records),
        'amc_count': len(amcs),
        'unpriced_count': sum(record.get('nav') is None for record in records),
        'duplicate_codes': len(parsed) - len(records),
        'forced': bool(problems),
        'warnings': problems,
        'records': records,
    }


def _meta(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != 'records'}


def _valid_payload(payload: Any) -> bool:
    return (isinstance(payload, dict) and payload.get('schema') == SCHEMA_VERSION and isinstance(payload.get('records'), list)
            and payload.get('count') == len(payload['records']) and bool(payload.get('version')))


# ---------------------------------------------------------------------------- storage

def _write_file(payload: dict[str, Any]) -> None:
    """Temporary file then rename, so a reader sees the old registry or the new one, never half of one."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix='.scheme_registry-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as temp_file:
            json.dump(payload, temp_file, ensure_ascii=False, separators=(',', ':'))
            temp_file.flush()
            os.fsync(temp_file.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_name, path)
                return
            except PermissionError:
                # Windows refuses to replace a file another process is reading at this moment.
                if attempt == 4:
                    raise
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def _read_file() -> dict[str, Any] | None:
    try:
        payload = json.loads(registry_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return payload if _valid_payload(payload) else None


def _cache_get(key: str) -> Any:
    try:
        return cache.get(key)
    except Exception:
        logger.exception('AMFI registry cache read failed for %s', key)
        return None


def _cache_set(key: str, value: Any, timeout: int | None) -> None:
    try:
        cache.set(key, value, timeout=timeout)
    except Exception:
        logger.exception('AMFI registry cache write failed for %s', key)


def _store(payload: dict[str, Any]) -> None:
    # The full registry goes in before the meta that points readers at it.
    try:
        _write_file(payload)
    except OSError:
        logger.exception('AMFI registry file write failed; the shared cache still holds it')
    _cache_set(PAYLOAD_KEY, payload, None)
    _cache_set(META_KEY, _meta(payload), None)


def _record_failure(error: str) -> None:
    _cache_set(BACKOFF_KEY, {'at': _now().isoformat(timespec='seconds'), 'error': error}, FAILURE_BACKOFF_SECONDS)
    meta = _cache_get(META_KEY)
    if isinstance(meta, dict):
        _cache_set(META_KEY, {**meta, 'last_failure': {'at': _now().isoformat(timespec='seconds'), 'error': error}}, None)


# ---------------------------------------------------------------------------- in-memory registry

@dataclass
class Registry:
    meta: dict[str, Any]
    records: list[dict[str, Any]]
    by_code: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_isin: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Per record: (display name, search text, search text without spaces, normalised display, normalised name).
    search_rows: list[tuple[str, str, str, str, str]] = field(default_factory=list)

    @property
    def version(self) -> str:
        return str(self.meta.get('version'))


_memory: Registry | None = None
_memory_lock = Lock()


def _index(payload: dict[str, Any]) -> Registry:
    from .services import _search_norm  # search text rules live with the search

    registry = Registry(meta=_meta(payload), records=payload['records'])
    for record in registry.records:
        record.setdefault('display_name', scheme_display_name(record))
        registry.by_code[str(record['scheme_code'])] = record
        for key in ('isin', 'isin_payout_growth', 'isin_reinvest'):
            isin = str(record.get(key) or '').upper()
            if isin and record not in registry.by_isin.setdefault(isin, []):
                registry.by_isin[isin].append(record)
        haystack = _search_norm(' '.join(str(record.get(key) or '') for key in (
            'display_name', 'plan', 'option', 'scheme_code', 'isin', 'isin_reinvest', 'amc', 'category')))
        registry.search_rows.append((record['display_name'], haystack, haystack.replace(' ', ''),
                                     _search_norm(record['display_name']), _search_norm(record.get('scheme_name'))))
    return registry


def _use(payload: dict[str, Any]) -> Registry:
    global _memory
    registry = _index(payload)
    with _memory_lock:
        _memory = registry
    return registry


def _is_stale(meta: dict[str, Any]) -> bool:
    try:
        fetched = datetime.fromisoformat(str(meta.get('fetched_at')))
    except ValueError:
        return True
    return _now() - fetched > timedelta(hours=refresh_hours())


def refresh_registry(*, force: bool = False, wait_for_lock: bool = False) -> dict[str, Any]:
    """Download AMFI and swap in the new registry if it validates. Returns a summary, never a half-applied state."""
    try:
        got_lock = cache.add(LOCK_KEY, True, timeout=REFRESH_LOCK_SECONDS)
    except Exception:
        logger.exception('AMFI registry refresh lock failed; refreshing without it')
        got_lock = True
    if not got_lock and not wait_for_lock:
        return {'status': 'busy', 'detail': 'Another refresh is running'}
    try:
        previous = current_meta()
        try:
            text = fetch_url(AMFI_NAV_URL, timeout=(5, 60)).text
            payload = build_registry(text, previous_count=(previous or {}).get('count'), force=force)
        except ProviderError as exc:
            kind = 'rejected' if isinstance(exc, RegistryRejected) else 'failed'
            logger.error('AMFI registry refresh %s; keeping the previous registry: %s', kind, exc)
            _record_failure(str(exc))
            return {'status': kind, 'detail': str(exc), 'registry': registry_status()}
        if previous and previous.get('count') and payload['count'] != previous['count']:
            logger.info('AMFI registry scheme count changed from %s to %s', previous['count'], payload['count'])
        _store(payload)
        _use(payload)
        try:
            cache.delete(BACKOFF_KEY)
        except Exception:
            pass
        logger.info('AMFI registry refreshed: %d schemes from %d AMCs, NAV date %s', payload['count'], payload['amc_count'], payload['as_of'])
        return {'status': 'refreshed', 'registry': registry_status()}
    finally:
        if got_lock:
            try:
                cache.delete(LOCK_KEY)
            except Exception:
                pass


def current_meta() -> dict[str, Any] | None:
    meta = _cache_get(META_KEY)
    if isinstance(meta, dict):
        return meta
    if _memory is not None:
        return _memory.meta
    payload = _read_file()
    return _meta(payload) if payload else None


def get_registry() -> Registry:
    """The newest validated registry this worker can reach, refreshing from AMFI when it is due."""
    meta = _cache_get(META_KEY)
    registry = _memory
    if registry is None or (isinstance(meta, dict) and meta.get('version') != registry.version):
        payload = _cache_get(PAYLOAD_KEY)
        if not _valid_payload(payload):
            payload = _read_file()
            if payload:
                # A restart, or a cache that lost it: put the file's copy back for the other workers.
                _cache_set(PAYLOAD_KEY, payload, None)
                if not isinstance(meta, dict) or meta.get('version') != payload['version']:
                    _cache_set(META_KEY, {**_meta(payload), **({'last_failure': meta['last_failure']} if isinstance(meta, dict) and meta.get('last_failure') else {})}, None)
        if payload:
            registry = _use(payload)
    if registry is None:
        # First run anywhere: there is nothing to fall back to, so this request waits for AMFI.
        result = refresh_registry(wait_for_lock=True)
        if _memory is None:
            raise ProviderError(result.get('detail') or 'AMFI scheme list is unavailable')
        return _memory
    if _is_stale(registry.meta) and _cache_get(BACKOFF_KEY) is None:
        result = refresh_registry()
        if result['status'] == 'refreshed' and _memory is not None:
            return _memory
    return registry


def registry_status() -> dict[str, Any]:
    """What a person needs to judge the search list: when it was updated, its size, and any refresh problem."""
    meta = current_meta() or {}
    last_failure = meta.get('last_failure')
    if last_failure and meta.get('fetched_at') and str(last_failure.get('at')) < str(meta.get('fetched_at')):
        last_failure = None
    return {
        'updatedAt': meta.get('fetched_at'), 'navDate': meta.get('as_of'), 'schemeCount': meta.get('count'),
        'amcCount': meta.get('amc_count'), 'unpricedCount': meta.get('unpriced_count'),
        'stale': _is_stale(meta) if meta else True, 'refreshHours': refresh_hours(),
        'lastRefreshError': last_failure, 'warnings': meta.get('warnings') or [],
    }


def registry_from_records(records: list[dict[str, Any]], **meta: Any) -> Registry:
    """An in-memory registry over given records, without validation. For tests and tools."""
    rows = [dict(record) for record in records]
    payload = {'schema': SCHEMA_VERSION, 'version': meta.pop('version', 'records'), 'fetched_at': _now().isoformat(timespec='seconds'),
               'count': len(rows), 'amc_count': len({row.get('amc') for row in rows}), 'records': rows, **meta}
    return _index(payload)


def reset_memory() -> None:
    """Forget this worker's in-memory copy (tests, or after changing the storage by hand)."""
    global _memory
    with _memory_lock:
        _memory = None
