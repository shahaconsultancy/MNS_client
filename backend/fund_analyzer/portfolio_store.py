"""Verified holdings and source status, kept as JSON files. No database is used.

Layout under FUND_ANALYZER_HOLDINGS_CACHE_DIR:

    <amc>/<portfolio>/<YYYY-MM>.json    one verified month, kept permanently
    <amc>/<portfolio>/status.json       the last retrieval result and next check time

Files are written atomically, so a crash or a second process cannot leave a half
written file. A file that fails validation when read is treated as missing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from django.conf import settings

from .adapters import (
    AMC_SOURCES,
    DISCLOSURE_NOT_PUBLISHED,
    DOWNLOAD_BLOCKED,
    DOWNLOAD_FAILED,
    HOLDINGS_AVAILABLE,
    INVALID_WORKBOOK,
    PARSING_FAILED,
    SCHEME_NOT_FOUND,
    SOURCE_NOT_SUPPORTED,
    normalise_name,
)

logger = logging.getLogger(__name__)

SOURCE_DOWNLOAD = 'download'
SOURCE_IMPORT = 'import'
# Data from an independent third-party API. Never presented as AMC data, and never allowed to replace it.
SOURCE_CHARLIE = 'charlie_alpha'
VERIFIED = 'VERIFIED'
SCHEMA_VERSION = 1
ISIN_PATTERN = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')
MONTH_FILE = re.compile(r'^(\d{4}-(?:0[1-9]|1[0-2]))\.json$')
ASSET_EQUITY = 'equity'
ASSET_DEBT = 'debt'
ASSET_HYBRID = 'hybrid'
# A Fund of Funds is compared on the mutual fund units it holds. Those rows carry
# security class ASSET_FUND_UNIT, so they can never enter an equity or debt overlap.
ASSET_FOF = 'fof'
ASSET_FUND_UNIT = 'fund_unit'
MAX_EQUITY_WEIGHT = 101
# Debt schemes can hold slightly over 100% when net current assets are negative.
MAX_DEBT_WEIGHT = 115
ALIASES_FILE = Path(__file__).with_name('scheme_aliases.json')
# Keeps a read-then-write in one worker together. Atomic writes protect across processes.
_update_lock = Lock()
# Parsed JSON, kept until the file on disk changes. An atomic write always swaps in a new file,
# so its signature changes and the next read parses it again. Bounded so memory stays small.
_parsed: OrderedDict[str, list[Any]] = OrderedDict()
_parsed_lock = Lock()
PARSED_CACHE_ENTRIES = 2048

# How long to wait before asking a source again.
RETRY_DELAYS = {
    DOWNLOAD_BLOCKED: timedelta(hours=24),
    SOURCE_NOT_SUPPORTED: timedelta(days=7),
    SCHEME_NOT_FOUND: timedelta(days=1),
    INVALID_WORKBOOK: timedelta(days=1),
    PARSING_FAILED: timedelta(days=1),
    DISCLOSURE_NOT_PUBLISHED: timedelta(hours=6),
}
MAX_RETRY_DELAY = timedelta(hours=24)
NEEDS_ADMIN = (SOURCE_NOT_SUPPORTED, DOWNLOAD_BLOCKED, INVALID_WORKBOOK, SCHEME_NOT_FOUND, PARSING_FAILED)


def _slug(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', str(value or '').lower()).strip('-') or 'unknown'


def portfolio_key(record: dict[str, Any]) -> str:
    """The underlying scheme's identity. Direct and Regular plans share it."""
    return f"{_slug(record.get('amc'))}:{normalise_name(str(record.get('scheme_name') or ''))}"


def _portfolio_dir(record: dict[str, Any]) -> Path:
    # Slugs only contain letters, digits and hyphens, so a name can never leave the cache folder.
    return Path(settings.FUND_ANALYZER_HOLDINGS_CACHE_DIR) / _slug(record.get('amc')) / _slug(normalise_name(str(record.get('scheme_name') or '')))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write to a temporary file, then swap it in. Readers see the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f'.{path.stem}-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as temp_file:
            json.dump(payload, temp_file, ensure_ascii=False, indent=1)
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
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    for attempt in range(5):
        try:
            with open(path, encoding='utf-8') as file:
                payload = json.load(file)
            return payload if isinstance(payload, dict) else None
        except FileNotFoundError:
            return None
        except PermissionError:
            # Windows refuses a read at the instant another process replaces the file.
            time.sleep(0.02 * (attempt + 1))
        except (OSError, ValueError) as exc:
            logger.warning('Ignoring unreadable cache file %s: %s', path, exc)
            return None
    logger.warning('Cache file stayed locked: %s', path)
    return None


def _signature(path: Path) -> tuple[int, int, int]:
    info = os.stat(path)
    return info.st_mtime_ns, info.st_size, info.st_ino


def _read_entry(path: Path) -> list[Any] | None:
    """[signature, payload, checks] for a JSON file, parsed once per version of the file."""
    key = str(path)
    try:
        signature = _signature(path)
    except FileNotFoundError:
        with _parsed_lock:
            _parsed.pop(key, None)
        return None
    except OSError:
        payload = _read_json(path)
        return None if payload is None else [None, payload, {}]
    with _parsed_lock:
        entry = _parsed.get(key)
        if entry and entry[0] == signature:
            _parsed.move_to_end(key)
            return entry
    payload = _read_json(path)
    if payload is None:
        return None
    entry = [signature, payload, {}]
    try:
        # Only remember it if the file did not change while it was being read.
        if _signature(path) == signature:
            with _parsed_lock:
                _parsed[key] = entry
                while len(_parsed) > PARSED_CACHE_ENTRIES:
                    _parsed.popitem(last=False)
    except OSError:
        pass
    return entry


def clear_read_cache() -> None:
    with _parsed_lock:
        _parsed.clear()


def _valid_snapshot(payload: dict[str, Any] | None, month: str) -> bool:
    """Only a complete, sensible month may be used. Anything else counts as missing."""
    if not payload or payload.get('schema_version') != SCHEMA_VERSION or payload.get('validation_status') != VERIFIED or payload.get('month') != month:
        return False
    rows = payload.get('holdings')
    if not isinstance(rows, list) or not rows or str(payload.get('reporting_date'))[:7] != month:
        return False
    total = 0.0
    equity_total = 0.0
    default_class = ASSET_DEBT if payload.get('asset_class') == ASSET_DEBT else ASSET_EQUITY
    for row in rows:
        if not isinstance(row, dict) or not ISIN_PATTERN.match(str(row.get('isin'))):
            return False
        weight = row.get('weight')
        # A Fund of Funds holding one fund can show a little over 100% when net current assets are negative.
        limit = MAX_DEBT_WEIGHT if row.get('security_class') == ASSET_FUND_UNIT else 100
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not 0 <= weight <= limit:
            return False
        total += weight
        if (row.get('security_class') or default_class) == ASSET_EQUITY:
            equity_total += weight
    # Fund units, repo and cash. Net current assets can be negative, so weight has no lower limit.
    for row in payload.get('other_holdings') or []:
        if not isinstance(row, dict) or not isinstance(row.get('weight'), (int, float)) or not row.get('name') or not row.get('instrument_type'):
            return False
    return equity_total <= MAX_EQUITY_WEIGHT and total <= MAX_DEBT_WEIGHT


def is_eligible_equity(record: dict[str, Any]) -> bool:
    category = re.sub(r'\s+', ' ', str(record.get('category') or '')).strip().lower()
    return category.startswith('equity scheme') or category in ('elss', 'growth')


def file_scheme_name(record: dict[str, Any]) -> str:
    """The name to look for inside AMC files, following any alias for a renamed scheme."""
    name = record.get('scheme_name') or ''
    entry = _read_entry(ALIASES_FILE)
    aliases = entry[1] if entry else {}
    amc_aliases = aliases.get(record.get('amc') or '')
    name = (amc_aliases.get(name) if isinstance(amc_aliases, dict) else None) or name
    # AMFI adds this to the scheme name. The AMC's own file does not use it.
    return re.sub(r'(?i)\s*\(existing number of segregated portfolios?[^)]*\)', '', name).strip()


def read_snapshot(record: dict[str, Any], month: str) -> dict[str, Any] | None:
    entry = _read_entry(_portfolio_dir(record) / f'{month}.json')
    if entry is None:
        return None
    signature, payload, checks = entry
    # Every read is still validated. The answer is remembered only for this exact version of the file.
    if month not in checks or signature is None:
        checks[month] = _valid_snapshot(payload, month)
    # A copy, so a caller can never change what the next reader sees.
    return dict(payload) if checks[month] else None


def store_snapshot(record: dict[str, Any], code: str, month: str, result: dict[str, Any], *, source_url: str = '', payload: bytes | None = None, source_type: str = SOURCE_DOWNLOAD, validation: dict[str, Any] | None = None) -> None:
    """Save a verified month. A failure here must never stop the holdings being used."""
    file_hash = hashlib.sha256(payload).hexdigest() if payload else ''
    try:
        with _update_lock:
            existing = read_snapshot(record, month)
            codes = sorted({*(existing.get('scheme_codes', []) if existing else []), str(code)})
            path = _portfolio_dir(record) / f'{month}.json'
            if existing:
                same_rows = (existing.get('classes_read') or []) == (result.get('classes_read') or [])
                unchanged = file_hash and existing.get('file_hash') == file_hash and same_rows
                # A download never replaces what staff uploaded on purpose.
                keep_upload = existing.get('source_type') == SOURCE_IMPORT and source_type == SOURCE_DOWNLOAD
                # Third-party data never replaces official data for the same month. Official data replaces it.
                keep_official = source_type == SOURCE_CHARLIE and existing.get('source_type') != SOURCE_CHARLIE
                if unchanged or keep_upload or keep_official:
                    if codes != existing.get('scheme_codes'):
                        _write_json_atomic(path, {**existing, 'scheme_codes': codes})
                    return
            names, industries = result.get('names') or {}, result.get('industries') or {}
            instrument_types, issuers = result.get('instrument_types') or {}, result.get('issuers') or {}
            security_classes = result.get('security_classes') or {}
            snapshot = {
                'schema_version': SCHEMA_VERSION,
                'asset_class': result.get('asset_class') or ASSET_EQUITY,
                'amc': record.get('amc') or '',
                'portfolio_key': portfolio_key(record),
                'scheme_name': record.get('scheme_name') or '',
                'scheme_codes': codes,
                'month': month,
                'reporting_date': str(result['portfolio_date'])[:10],
                'source_type': source_type,
                'source_url': (source_url or '')[:1000],
                'file_hash': file_hash,
                'validation_status': VERIFIED,
                **({'third_party': True, 'validation': validation or {}} if source_type == SOURCE_CHARLIE else {}),
                'benchmark': result.get('benchmark') or '',
                'risk_level': result.get('risk_level') or '',
                **({'other_holdings': result['other_holdings']} if result.get('other_holdings') else {}),
                **({'classes_read': result['classes_read']} if result.get('classes_read') else {}),
                **({'unclassified_weight': round(result['unclassified_weight'], 2)} if result.get('unclassified_weight') is not None else {}),
                'imported_at': datetime.now(timezone.utc).isoformat(),
                'holdings': [
                    {
                        'isin': isin, 'name': names.get(isin) or '', 'industry': industries.get(isin) or '', 'weight': weight,
                        **({'instrument_type': instrument_types[isin]} if instrument_types.get(isin) else {}),
                        **({'issuer': issuers[isin]} if issuers.get(isin) else {}),
                        **({'security_class': security_classes[isin]} if security_classes.get(isin) else {}),
                    }
                    for isin, weight in result['holdings'].items()
                ],
            }
            if not _valid_snapshot(snapshot, month):
                logger.warning('Not caching invalid holdings for %s %s', snapshot['portfolio_key'], month)
                return
            _write_json_atomic(path, snapshot)
    except (OSError, KeyError, TypeError, ValueError):
        logger.exception('Portfolio holdings could not be cached for %s %s', record.get('scheme_name'), month)


def stored_links(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The verified months available for this portfolio, newest first."""
    links = []
    try:
        names = sorted((entry.name for entry in os.scandir(_portfolio_dir(record))), reverse=True)
    except OSError:
        return []
    for name in names:
        match = MONTH_FILE.match(name)
        if not match:
            continue
        snapshot = read_snapshot(record, match.group(1))
        if snapshot:
            links.append({
                'url': f'stored://{match.group(1)}',
                'snapshot_id': str(_portfolio_dir(record) / name),
                'month': match.group(1),
                'portfolio_date': snapshot['reporting_date'],
                'stored': True,
                # Lets callers try official sources first within a month.
                'third_party': snapshot.get('source_type') == SOURCE_CHARLIE,
            })
    return links


def load_snapshot(snapshot_id: str) -> dict[str, Any]:
    entry = _read_entry(Path(snapshot_id))
    payload = entry[1] if entry else None
    rows = (payload or {}).get('holdings') or []
    return {
        'holdings': {row['isin']: row['weight'] for row in rows},
        'names': {row['isin']: row['name'] for row in rows if row.get('name')},
        'industries': {row['isin']: row['industry'] for row in rows if row.get('industry')},
        'instrument_types': {row['isin']: row['instrument_type'] for row in rows if row.get('instrument_type')},
        'issuers': {row['isin']: row['issuer'] for row in rows if row.get('issuer')},
        'other_holdings': (payload or {}).get('other_holdings') or [],
        'security_classes': {row['isin']: row['security_class'] for row in rows if row.get('security_class')},
        # A file saved before securities were labelled holds its fund's own kind only.
        'classes_read': (payload or {}).get('classes_read') or [ASSET_DEBT if (payload or {}).get('asset_class') == ASSET_DEBT else ASSET_EQUITY],
        'unclassified_weight': (payload or {}).get('unclassified_weight'),
        'source_url': (payload or {}).get('source_url') or '',
        'source_type': (payload or {}).get('source_type') or SOURCE_DOWNLOAD,
        'validation': (payload or {}).get('validation') or None,
        'imported_at': (payload or {}).get('imported_at'),
        'benchmark': (payload or {}).get('benchmark') or None,
        'risk_level': (payload or {}).get('risk_level') or None,
        'portfolio_date': (payload or {}).get('reporting_date'),
    }


def _read_status(record: dict[str, Any]) -> dict[str, Any]:
    entry = _read_entry(_portfolio_dir(record) / 'status.json')
    return dict(entry[1]) if entry else {}


def discovery_due(record: dict[str, Any]) -> bool:
    """False while a source was checked recently, or is backing off after a failure."""
    next_check = _read_status(record).get('next_check_at')
    if not next_check:
        return True
    try:
        return datetime.fromisoformat(next_check) <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def source_status(record: dict[str, Any]) -> str | None:
    return _read_status(record).get('status')


def _previous_month() -> str:
    return (date.today().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


def record_status(record: dict[str, Any], status: str, detail: str = '', latest_month: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    try:
        current = _read_status(record)
        failures = 0 if status == HOLDINGS_AVAILABLE else int(current.get('failures') or 0) + 1
        if status == HOLDINGS_AVAILABLE:
            # Nothing newer can exist until next month's disclosure, so check less often.
            delay = timedelta(hours=24) if latest_month and latest_month >= _previous_month() else timedelta(hours=6)
        elif status == DOWNLOAD_FAILED:
            delay = min(timedelta(minutes=15) * (2 ** min(failures, 8)), MAX_RETRY_DELAY)
        else:
            delay = RETRY_DELAYS.get(status, MAX_RETRY_DELAY)
        _write_json_atomic(_portfolio_dir(record) / 'status.json', {
            'amc': record.get('amc') or '',
            'portfolio_key': portfolio_key(record),
            'scheme_name': record.get('scheme_name') or '',
            'status': status,
            'detail': detail[:2000],
            'failures': failures,
            'latest_month': latest_month or current.get('latest_month') or '',
            'last_checked_at': now.isoformat(),
            'last_success_at': now.isoformat() if status == HOLDINGS_AVAILABLE else current.get('last_success_at'),
            'next_check_at': (now + delay).isoformat(),
        })
    except (OSError, TypeError, ValueError):
        logger.exception('Portfolio status could not be recorded for %s', record.get('scheme_name'))


def coverage_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Internal coverage figures. A fund counts as verified only with a real parsed file."""
    schemes = {portfolio_key(record): record for record in records if is_eligible_equity(record)}
    amcs: dict[str, dict[str, Any]] = {}
    action = []
    failures: Counter = Counter()
    verified = 0
    for record in schemes.values():
        amc = record.get('amc') or 'Unknown'
        entry = amcs.setdefault(amc, {'amc': amc, 'eligible': 0, 'verified': 0, 'latest_month': None, 'last_import': None, 'dedicated_adapter': amc in AMC_SOURCES, 'statuses': Counter()})
        entry['eligible'] += 1
        links = stored_links(record)
        if links:
            verified += 1
            entry['verified'] += 1
            entry['latest_month'] = max(entry['latest_month'] or '', links[0]['month'])
            imported = (read_snapshot(record, links[0]['month']) or {}).get('imported_at')
            entry['last_import'] = max(entry['last_import'] or '', imported or '') or None
        status = _read_status(record)
        status_name = status.get('status') or 'NOT_CHECKED'
        entry['statuses'][status_name] += 1
        if status.get('status') and status_name != HOLDINGS_AVAILABLE:
            failures[status_name] += 1
        if not links and status_name in NEEDS_ADMIN:
            action.append({'amc': amc, 'scheme_name': record.get('scheme_name'), 'status': status_name, 'detail': status.get('detail'), 'last_checked_at': status.get('last_checked_at')})
    ranked = sorted(amcs.values(), key=lambda entry: (-entry['verified'], entry['amc']))
    return {
        'total_eligible_schemes': len(schemes),
        'verified_schemes': verified,
        'schemes_without_verified_holdings': len(schemes) - verified,
        'amcs_with_verified_holdings': sum(1 for entry in ranked if entry['verified']),
        'failures_by_status': dict(failures),
        'amcs': [{**entry, 'statuses': dict(entry['statuses'])} for entry in ranked],
        'funds_requiring_action': sorted(action, key=lambda item: (item['amc'], str(item['scheme_name']))),
    }
