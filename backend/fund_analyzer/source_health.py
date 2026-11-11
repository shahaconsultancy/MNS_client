"""AMC-level source health: why a disclosure source failed, at which stage, and when to ask it again.

A scheme's own status.json answers "did this fund's portfolio load". This module answers the
question one level up, "is this AMC's disclosure source working", so that one AMC-wide problem
(a moved page, a block, an outage) is recorded once, with evidence, and is not re-learnt fund by fund.

Health files live beside the holdings cache, as JSON, written atomically:

    <FUND_ANALYZER_HOLDINGS_CACHE_DIR>/_sources/<amc-key>.json

Nothing here bypasses an access restriction. A 403 or a bot challenge is recorded as
ACCESS_BLOCKED and the source is left alone for a day; verified holdings and the authorised
backup are what the analysis falls back on.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

logger = logging.getLogger(__name__)

# --- Error categories: what went wrong, independent of which fund asked ----------------------------
ACCESS_BLOCKED = 'ACCESS_BLOCKED'
SOURCE_URL_MOVED = 'SOURCE_URL_MOVED'
RATE_LIMITED = 'RATE_LIMITED'
SOURCE_TEMPORARILY_UNAVAILABLE = 'SOURCE_TEMPORARILY_UNAVAILABLE'
DISCOVERY_FAILED = 'DISCOVERY_FAILED'
DOWNLOAD_FAILED = 'DOWNLOAD_FAILED'
INVALID_WORKBOOK = 'INVALID_WORKBOOK'
SCHEME_MAPPING_FAILED = 'SCHEME_MAPPING_FAILED'
SCHEME_AMBIGUOUS = 'SCHEME_AMBIGUOUS'
HOLDINGS_PARSE_FAILED = 'HOLDINGS_PARSE_FAILED'
HOLDINGS_VALIDATION_FAILED = 'HOLDINGS_VALIDATION_FAILED'
PORTFOLIO_MONTH_UNAVAILABLE = 'PORTFOLIO_MONTH_UNAVAILABLE'

# Failures that describe the AMC's source as a whole. Every scheme of the AMC shares them.
AMC_WIDE_CATEGORIES = frozenset({ACCESS_BLOCKED, SOURCE_URL_MOVED, RATE_LIMITED, SOURCE_TEMPORARILY_UNAVAILABLE, DISCOVERY_FAILED})

# --- Retrieval stages ---------------------------------------------------------------------------
STAGE_LISTING = 'listing'
STAGE_DOWNLOAD = 'download'
STAGE_WORKBOOK = 'workbook'
STAGE_MAPPING = 'mapping'
STAGE_PARSE = 'parse'

# --- AMC health states --------------------------------------------------------------------------
WORKING = 'WORKING'
TEMPORARILY_UNAVAILABLE = 'TEMPORARILY_UNAVAILABLE'
STATE_ACCESS_BLOCKED = 'ACCESS_BLOCKED'
STATE_DISCOVERY_FAILED = 'DISCOVERY_FAILED'
PARSER_ISSUE = 'PARSER_ISSUE'
NOT_CHECKED = 'NOT_CHECKED'

STATE_FOR_CATEGORY = {
    ACCESS_BLOCKED: STATE_ACCESS_BLOCKED,
    SOURCE_URL_MOVED: STATE_DISCOVERY_FAILED,
    DISCOVERY_FAILED: STATE_DISCOVERY_FAILED,
    RATE_LIMITED: TEMPORARILY_UNAVAILABLE,
    SOURCE_TEMPORARILY_UNAVAILABLE: TEMPORARILY_UNAVAILABLE,
    DOWNLOAD_FAILED: TEMPORARILY_UNAVAILABLE,
    INVALID_WORKBOOK: PARSER_ISSUE,
    HOLDINGS_PARSE_FAILED: PARSER_ISSUE,
}

# How long the AMC's circuit stays open after an AMC-wide failure. No request is sent meanwhile.
ACCESS_BLOCKED_PAUSE = timedelta(hours=24)
DISCOVERY_FAILED_PAUSE = timedelta(hours=6)
RATE_LIMIT_DEFAULT_PAUSE = timedelta(minutes=15)
RATE_LIMIT_MAX_PAUSE = timedelta(hours=2)
TEMPORARY_BASE_PAUSE = timedelta(minutes=5)
TEMPORARY_MAX_PAUSE = timedelta(hours=2)
# Requests to one AMC host are spaced at least this far apart within a worker.
MIN_REQUEST_INTERVAL_SECONDS = 1.0
EVIDENCE_HISTORY = 10

# Pages served instead of the content by a CDN's bot manager or an interstitial challenge.
# Only markers of the denial or challenge page itself: Cloudflare loads challenge-platform scripts,
# and sites embed captcha widgets in forms, on perfectly normal pages (DSP's listing is one).
_BLOCK_MARKERS = re.compile(
    r'(?i)<title>\s*(access denied|attention required[^<]*|just a moment\.*|request rejected|403 forbidden)\s*</title>'
    r'|errors\.edgesuite\.net|window\._cf_chl_opt',
)

_lock = threading.Lock()
_last_request: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def evidence_from_response(response: Any, requested_url: str, started: float | None = None) -> dict[str, Any]:
    """The facts worth keeping about one HTTP exchange. Never cookies, tokens or request headers."""
    headers = getattr(response, 'headers', None) or {}
    final_url = str(getattr(response, 'url', '') or requested_url)
    history = getattr(response, 'history', None) or []
    server = str(headers.get('Server', '') or '')
    cdn = 'akamai' if headers.get('Akamai-GRN') or 'akamai' in server.lower() else 'cloudflare' if headers.get('cf-ray') or server.lower() == 'cloudflare' else ''
    try:
        size = int(headers.get('Content-Length') or 0) or None
    except (TypeError, ValueError):
        size = None
    status = getattr(response, 'status_code', None)
    return {
        'url': _safe_url(requested_url),
        'http_status': status if isinstance(status, int) else None,
        'content_type': str(headers.get('Content-Type', '') or '').split(';')[0].strip(),
        'size': size,
        'final_url': _safe_url(final_url) if final_url != requested_url else '',
        'redirects': len(history) if isinstance(history, list) else 0,
        'seconds': round(time.perf_counter() - started, 2) if started else None,
        'server': server[:40],
        'cdn': cdn,
        'retry_after': str(headers.get('Retry-After') or '')[:40] or None,
    }


def _safe_url(url: str) -> str:
    """Scheme, host and path only. A query string can carry a token, so it is dropped."""
    parts = urlsplit(str(url or ''))
    return f'{parts.scheme}://{parts.netloc}{parts.path}'[:500] if parts.netloc else ''


def looks_blocked(text: str) -> bool:
    return bool(_BLOCK_MARKERS.search(text[:20000] if text else ''))


def category_for_http(status: int | None, *, stage: str, body: str = '', guessed: bool = False) -> str:
    """The category of an HTTP answer, given what was being fetched."""
    if status in (401, 403, 407, 451) or (body and looks_blocked(body)):
        # A guessed file name that does not exist is a 403 on some file hosts, not a block.
        return PORTFOLIO_MONTH_UNAVAILABLE if guessed and status == 403 else ACCESS_BLOCKED
    if status == 429:
        return RATE_LIMITED
    if status in (404, 410):
        if stage == STAGE_LISTING:
            return SOURCE_URL_MOVED
        return PORTFOLIO_MONTH_UNAVAILABLE if guessed else DOWNLOAD_FAILED
    if status is not None and status >= 500:
        return SOURCE_TEMPORARILY_UNAVAILABLE
    return DOWNLOAD_FAILED


def category_for_exception(exc: BaseException, *, stage: str) -> str:
    """Timeouts and connection failures say nothing about the source being gone."""
    category = getattr(exc, 'category', None)
    if category:
        return category
    text = str(exc).lower()
    match = re.search(r'\b([1-5]\d\d) (?:client|server) error', text)
    if match:
        return category_for_http(int(match.group(1)), stage=stage)
    if re.search(r'timed out|timeout|connection|remote end closed|ssl|max retries', text):
        return SOURCE_TEMPORARILY_UNAVAILABLE
    return DOWNLOAD_FAILED


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, (parsedate_to_datetime(value) - _now()).total_seconds())
    except (TypeError, ValueError):
        return None


def backoff_with_jitter(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """Full-jitter exponential backoff, so many workers never retry in step."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def throttle(host: str) -> None:
    """Space requests to one AMC host. A polite per-worker limit, not a security control."""
    if not host:
        return
    with _lock:
        wait = _last_request.get(host, 0.0) + MIN_REQUEST_INTERVAL_SECONDS - time.monotonic()
        _last_request[host] = time.monotonic() + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


# --- Source registry fingerprint ----------------------------------------------------------------
# Raise this whenever shared discovery, download or archive handling changes. Failures recorded
# under an older version are retried at once instead of waiting out their back-off.
# 4: access-denied detection narrowed (Cloudflare's ordinary challenge-platform script is not a block).
RETRIEVAL_VERSION = 4


def source_fingerprint(source: dict[str, Any] | None) -> str:
    """Changes when this AMC's verified source configuration or the shared retrieval code changes."""
    relevant = {key: value for key, value in (source or {}).items() if key not in ('notes', 'verified_at', 'evidence')}
    text = json.dumps({'v': RETRIEVAL_VERSION, 'source': relevant}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# --- Health store -------------------------------------------------------------------------------
def _health_dir() -> Path:
    return Path(settings.FUND_ANALYZER_HOLDINGS_CACHE_DIR) / '_sources'


def _health_path(amc_key: str) -> Path:
    slug = re.sub(r'[^a-z0-9]+', '-', str(amc_key or '').lower()).strip('-') or 'unknown'
    return _health_dir() / f'{slug}.json'


def read_health(amc_key: str) -> dict[str, Any]:
    from .portfolio_store import _read_entry

    entry = _read_entry(_health_path(amc_key))
    return dict(entry[1]) if entry else {}


def all_health() -> list[dict[str, Any]]:
    from .portfolio_store import _read_entry

    try:
        paths = sorted(_health_dir().glob('*.json'))
    except OSError:
        return []
    rows = []
    for path in paths:
        entry = _read_entry(path)
        if entry:
            rows.append(dict(entry[1]))
    return rows


def _write_health(amc_key: str, payload: dict[str, Any]) -> None:
    from .portfolio_store import _write_json_atomic

    try:
        _write_json_atomic(_health_path(amc_key), payload)
    except OSError:
        logger.exception('AMC source health could not be saved for %s', amc_key)


def circuit_open(amc_key: str, source: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The open circuit for this AMC, or None if it may be asked now.

    A circuit opened under an older source configuration or older retrieval code is ignored: the
    repair is exactly what should be tried next, without anyone deleting files.
    """
    health = read_health(amc_key)
    until = health.get('circuit_open_until')
    if not until:
        return None
    if source is not None and health.get('fingerprint') and health['fingerprint'] != source_fingerprint(source):
        return None
    try:
        if datetime.fromisoformat(until) > _now():
            return health
    except (TypeError, ValueError):
        return None
    return None


def _pause_for(category: str, failures: int, retry_after: float | None) -> timedelta | None:
    if category == ACCESS_BLOCKED:
        return ACCESS_BLOCKED_PAUSE
    if category in (SOURCE_URL_MOVED, DISCOVERY_FAILED):
        return DISCOVERY_FAILED_PAUSE
    if category == RATE_LIMITED:
        return min(timedelta(seconds=retry_after), RATE_LIMIT_MAX_PAUSE) if retry_after else RATE_LIMIT_DEFAULT_PAUSE
    if category == SOURCE_TEMPORARILY_UNAVAILABLE:
        # Two misses in a row open the circuit; one slow answer does not.
        if failures < 2:
            return None
        return min(TEMPORARY_BASE_PAUSE * (2 ** min(failures - 2, 6)), TEMPORARY_MAX_PAUSE)
    return None


def record_failure(amc_key: str, *, amc: str = '', source: dict[str, Any] | None = None, stage: str, category: str, detail: str = '', evidence: dict[str, Any] | None = None, open_circuit: bool = True) -> dict[str, Any]:
    """Record an AMC-level failure with its evidence. Opens the circuit when the category calls for it.

    open_circuit=False records the state without pausing requests, for a source that answers but
    is out of date: asking it again costs nothing and its files are still usable history.
    """
    with _lock:
        health = read_health(amc_key)
        now = _now()
        same = health.get('last_failure', {}).get('category') == category
        failures = int(health.get('consecutive_failures') or 0) + 1 if same or health.get('state') != WORKING else 1
        retry_after = retry_after_seconds((evidence or {}).get('retry_after'))
        pause = _pause_for(category, failures, retry_after) if category in AMC_WIDE_CATEGORIES and open_circuit else None
        failure = {'at': now.isoformat(), 'stage': stage, 'category': category, 'detail': str(detail)[:500], 'evidence': evidence or {}}
        state = STATE_FOR_CATEGORY.get(category, health.get('state') or NOT_CHECKED)
        if category not in AMC_WIDE_CATEGORIES and health.get('state') == WORKING:
            # One scheme's file or sheet failing does not make the AMC's source broken.
            state = WORKING
        payload = {
            **health,
            'amc': amc or health.get('amc') or '',
            'key': amc_key,
            'state': state,
            'source_url': _safe_url((source or {}).get('url') or health.get('source_url') or ''),
            'discovery_method': (source or {}).get('parser') or health.get('discovery_method') or '',
            'fingerprint': source_fingerprint(source) if source else health.get('fingerprint'),
            'last_checked_at': now.isoformat(),
            'consecutive_failures': failures,
            'last_failure': failure,
            'history': ([failure] + list(health.get('history') or []))[:EVIDENCE_HISTORY],
            'counters': _bump(health.get('counters'), f'{stage}:{category}'),
            'circuit_open_until': (now + pause).isoformat() if pause else health.get('circuit_open_until') if same else None,
        }
        _write_health(amc_key, payload)
    logger.warning('AMC source %s failed at %s: %s %s %s', amc_key, stage, category, (evidence or {}).get('http_status') or '', str(detail)[:200])
    return payload


def record_success(amc_key: str, *, amc: str = '', source: dict[str, Any] | None = None, stage: str, months: list[str] | None = None, evidence: dict[str, Any] | None = None) -> None:
    """A stage worked. A working listing closes the circuit; later failures start counting afresh."""
    with _lock:
        health = read_health(amc_key)
        now = _now()
        latest = max([*(months or []), health.get('latest_month') or ''])
        payload = {
            **health,
            'amc': amc or health.get('amc') or '',
            'key': amc_key,
            'state': WORKING if stage in (STAGE_LISTING, STAGE_DOWNLOAD, STAGE_PARSE) else health.get('state') or WORKING,
            'source_url': _safe_url((source or {}).get('url') or health.get('source_url') or ''),
            'discovery_method': (source or {}).get('parser') or health.get('discovery_method') or '',
            'fingerprint': source_fingerprint(source) if source else health.get('fingerprint'),
            'last_checked_at': now.isoformat(),
            f'last_{stage}_success_at': now.isoformat(),
            'last_success_at': now.isoformat(),
            'latest_month': latest or None,
            'consecutive_failures': 0,
            'circuit_open_until': None,
            'counters': _bump(health.get('counters'), f'{stage}:ok'),
            **({'last_success_evidence': evidence} if evidence else {}),
        }
        # Avoid rewriting the file on every cached read: only when something worth keeping changed.
        if health and health.get('state') == payload['state'] and health.get('latest_month') == payload['latest_month'] and not health.get('circuit_open_until') and _recent(health.get(f'last_{stage}_success_at')):
            return
        _write_health(amc_key, payload)


def _recent(value: str | None, minutes: int = 30) -> bool:
    try:
        return bool(value) and _now() - datetime.fromisoformat(value) < timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return False


def _bump(counters: dict[str, int] | None, key: str) -> dict[str, int]:
    updated = dict(counters or {})
    updated[key] = int(updated.get(key) or 0) + 1
    return updated


def reset_circuit(amc_key: str) -> None:
    """Close the circuit, for example after the source configuration was repaired."""
    with _lock:
        health = read_health(amc_key)
        if health:
            _write_health(amc_key, {**health, 'circuit_open_until': None, 'consecutive_failures': 0})


def recovered_since(amc_key: str, moment: str | None) -> bool:
    """Has this AMC's source worked after the given time? Then a scheme's AMC-wide failure is stale."""
    health = read_health(amc_key)
    success = health.get('last_success_at')
    if not success or not moment:
        return False
    try:
        return datetime.fromisoformat(success) > datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        return False
