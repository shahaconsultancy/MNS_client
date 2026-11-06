"""Overlap jobs, so a slow first retrieval never holds a request open.

Update Analysis starts a job, then polls it. A job is one JSON file under the holdings cache
folder, written atomically, so there is no database and any worker process can answer a
status request. The same request made while its job is still running joins that job instead
of starting a second one, which also means one set of downloads.

Jobs run on threads inside the web process. That suits a server with a disk and a long-lived
process. On a platform that freezes the process once a response is sent, use /api/overlap.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

from django.conf import settings

from .portfolio_store import _read_json, _write_json_atomic
from .providers import ProviderError

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 60 * 60
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
STAGE_MESSAGES = {
    'queued': 'Waiting to start...',
    'fetching': 'Fetching portfolio holdings...',
    'backup': 'Retrieving alternative portfolio source...',
    'validating': 'Validating holdings...',
    'calculating': 'Calculating overlap...',
    'done': 'Overlap ready',
    'failed': 'Overlap could not be calculated',
}
RUNNING = ('queued', 'running')

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='overlap-job')
_lock = Lock()
_active: dict[tuple, str] = {}


def _job_dir() -> Path:
    return Path(settings.FUND_ANALYZER_HOLDINGS_CACHE_DIR) / 'jobs'


def _path(job_id: str) -> Path:
    return _job_dir() / f'{job_id}.json'


def get_job(job_id: str) -> dict[str, Any] | None:
    # The id becomes a file name, so only the exact shape this module issues is accepted.
    return _read_json(_path(job_id)) if JOB_ID.match(str(job_id)) else None


def _save(job: dict[str, Any]) -> None:
    _write_json_atomic(_path(job['job_id']), job)


def _update(job_id: str, **changes: Any) -> None:
    job = get_job(job_id)
    if job:
        job.update(changes, updated_at=time.time())
        job['message'] = changes.get('message') or STAGE_MESSAGES.get(job['stage'], job.get('message'))
        _save(job)


def _clean_old_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    try:
        for entry in _job_dir().iterdir():
            if entry.suffix == '.json' and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def start_job(scheme_codes: list[str], portfolio_month: str | None = None) -> dict[str, Any]:
    """Start the overlap for these funds, or join the identical job that is already running."""
    key = (tuple(str(code) for code in scheme_codes), portfolio_month)
    _clean_old_jobs()
    with _lock:
        existing = get_job(_active[key]) if key in _active else None
        if existing and existing['status'] in RUNNING:
            return existing
        job = {
            'job_id': uuid.uuid4().hex, 'status': 'queued', 'stage': 'queued', 'message': STAGE_MESSAGES['queued'],
            'scheme_codes': list(key[0]), 'portfolio_month': portfolio_month, 'created_at': time.time(), 'updated_at': time.time(),
        }
        _save(job)
        _active[key] = job['job_id']
    _executor.submit(_run, job['job_id'], list(key[0]), portfolio_month, key)
    return job


def _run(job_id: str, scheme_codes: list[str], portfolio_month: str | None, key: tuple) -> None:
    # Imported here: services imports holdings, and this module must load before either is needed.
    from .services import compare_holdings_overlap

    try:
        _update(job_id, status='running')
        result = compare_holdings_overlap(scheme_codes, portfolio_month, progress=lambda stage: _update(job_id, stage=stage))
        # File locations and error details are for the internal coverage report only.
        result.pop('debug', None)
        _update(job_id, status='done', stage='done', result=result)
    except ProviderError as exc:
        logger.warning('Overlap job %s failed: %s', job_id, exc)
        _update(job_id, status='failed', stage='failed', error='Portfolio holdings unavailable')
    except Exception:
        logger.exception('Overlap job %s crashed', job_id)
        _update(job_id, status='failed', stage='failed', error='Overlap could not be calculated')
    finally:
        with _lock:
            if _active.get(key) == job_id:
                del _active[key]
