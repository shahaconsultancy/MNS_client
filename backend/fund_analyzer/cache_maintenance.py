"""Keeps the JSON holdings cache small. Run by `manage.py cleanup_portfolio_cache`, never by a request.

Policy, in priority order. Anything protected is kept whatever its age:

    Protected   a fund's newest valid official month; every month inside the official or backup lookback;
                staff uploads; anything a running analysis job uses; anything changed in the last hour.
    Official    months older than the official retention are archived, never deleted outright. Without an
                archive folder they are kept, because an AMC may no longer offer them for download.
    Third-party months older than the backup retention are deleted. They are outside the backup window,
                so no analysis can use them.
    Unreadable  month files that fail validation are ignored by the app already. They are moved to the
                archive for inspection, or reported if there is no archive.
    Status      a fund with no holdings keeps its status while its retry wait lasts. After the wait is over
                and the last check is older than the status retention, it is deleted.
    Temporary   abandoned write files older than the temporary-file limit are deleted.
    Jobs        finished job files older than an hour are deleted. A running job is never touched.
    Folders     a fund or AMC folder is removed once it is empty and has not changed recently.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from django.conf import settings

import logging

logger = logging.getLogger(__name__)

from .portfolio_store import MONTH_FILE, SOURCE_CHARLIE, SOURCE_IMPORT, _read_json, _valid_snapshot

GRACE_SECONDS = 60 * 60
JOB_TTL_SECONDS = 60 * 60
STALE_JOB_SECONDS = 6 * 60 * 60
STALE_LOCK_SECONDS = 6 * 60 * 60
LOCK_NAME = '.cleanup.lock'
TEMP_FILE = re.compile(r'^\..+\.tmp$')
RUNNING = ('queued', 'running')


@dataclass
class Action:
    kind: str          # delete, archive, keep
    category: str      # official, third_party, invalid, status, temp, job, folder
    path: Path
    size: int
    reason: str


@dataclass
class Plan:
    root: Path
    actions: list[Action] = field(default_factory=list)

    def add(self, kind: str, category: str, path: Path, reason: str) -> None:
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        self.actions.append(Action(kind, category, path, size, reason))

    def summary(self) -> dict[str, Any]:
        totals: dict[str, dict[str, int]] = {}
        for action in self.actions:
            entry = totals.setdefault(f'{action.kind}:{action.category}', {'files': 0, 'bytes': 0})
            entry['files'] += 1
            entry['bytes'] += action.size
        return {
            'removable_files': sum(1 for a in self.actions if a.kind in ('delete', 'archive') and a.category != 'folder'),
            'removable_folders': sum(1 for a in self.actions if a.kind == 'delete' and a.category == 'folder'),
            'reclaimable_bytes': sum(a.size for a in self.actions if a.kind in ('delete', 'archive')),
            'kept_protected': sum(1 for a in self.actions if a.kind == 'keep'),
            'by_kind': dict(sorted(totals.items())),
        }


def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7])


def _latest_completed_month(today: date) -> str:
    return (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


def _running_job_codes(root: Path) -> set[str]:
    codes: set[str] = set()
    for path in (root / 'jobs').glob('*.json'):
        job = _read_json(path) or {}
        if job.get('status') in RUNNING and time.time() - float(job.get('updated_at') or 0) < STALE_JOB_SECONDS:
            codes.update(str(code) for code in job.get('scheme_codes') or [])
    return codes


def build_plan(root: Path | None = None, *, today: date | None = None, now: float | None = None,
               official_months: int | None = None, backup_months: int | None = None,
               temp_hours: int | None = None, status_days: int | None = None, archive: bool | None = None) -> Plan:
    """Decide what could go. Reads files, changes nothing."""
    root = Path(root or settings.FUND_ANALYZER_HOLDINGS_CACHE_DIR)
    today = today or date.today()
    now = now if now is not None else time.time()
    # Retention can never be shorter than what an analysis is allowed to read.
    official_keep = max(official_months or settings.FUND_ANALYZER_CACHE_OFFICIAL_RETENTION_MONTHS, settings.FUND_ANALYZER_PORTFOLIO_LOOKBACK_MONTHS)
    backup_keep = max(backup_months or settings.FUND_ANALYZER_CACHE_BACKUP_RETENTION_MONTHS, getattr(settings, 'FUND_ANALYZER_CHARLIE_ALPHA_LOOKBACK_MONTHS', 4))
    temp_seconds = (temp_hours or settings.FUND_ANALYZER_CACHE_TEMP_FILE_HOURS) * 3600
    status_age = timedelta(days=status_days or settings.FUND_ANALYZER_CACHE_STATUS_RETENTION_DAYS)
    can_archive = bool(settings.FUND_ANALYZER_CACHE_ARCHIVE_DIR) if archive is None else archive
    latest = _month_index(_latest_completed_month(today))
    active_codes = _running_job_codes(root)
    plan = Plan(root)
    if not root.is_dir():
        return plan

    def recent(path: Path) -> bool:
        try:
            return now - path.stat().st_mtime < GRACE_SECONDS
        except OSError:
            return True

    for path in root.rglob('*'):
        if path.is_file() and TEMP_FILE.match(path.name):
            if not recent(path) and now - path.stat().st_mtime >= temp_seconds:
                plan.add('delete', 'temp', path, f'abandoned write older than {temp_seconds // 3600}h')

    for path in (root / 'jobs').glob('*.json'):
        job = _read_json(path) or {}
        running = job.get('status') in RUNNING and now - float(job.get('updated_at') or 0) < STALE_JOB_SECONDS
        if running:
            plan.add('keep', 'job', path, 'job is running')
        elif now - path.stat().st_mtime >= JOB_TTL_SECONDS:
            plan.add('delete', 'job', path, 'finished job older than 1h')

    removed_files: dict[Path, int] = {}
    for folder in sorted(p for p in root.glob('*/*') if p.is_dir() and p.parent.name != 'jobs'):
        months = sorted((p for p in folder.iterdir() if p.is_file() and MONTH_FILE.match(p.name)), reverse=True)
        snapshots = {p: _read_json(p) for p in months}
        valid = {p: s for p, s in snapshots.items() if _valid_snapshot(s, p.stem)}
        in_use = any(active_codes & {str(c) for c in (s or {}).get('scheme_codes') or []} for s in snapshots.values())
        newest_official = next((p for p in months if p in valid and valid[p].get('source_type') != SOURCE_CHARLIE), None)
        gone = 0
        for path in months:
            age = latest - _month_index(path.stem)
            snapshot = snapshots[path]
            if in_use:
                plan.add('keep', 'official', path, 'used by a running analysis job')
            elif recent(path):
                plan.add('keep', 'official', path, 'changed in the last hour')
            elif path not in valid:
                if can_archive:
                    plan.add('archive', 'invalid', path, 'fails validation, so the app ignores it')
                    gone += 1
                else:
                    plan.add('keep', 'invalid', path, 'fails validation; kept for inspection (no archive folder set)')
            elif snapshot.get('source_type') == SOURCE_CHARLIE:
                if age >= backup_keep:
                    plan.add('delete', 'third_party', path, f'third-party month {age} months old, retention {backup_keep}')
                    gone += 1
            elif snapshot.get('source_type') == SOURCE_IMPORT:
                if age >= official_keep:
                    plan.add('keep', 'official', path, 'staff upload, never removed automatically')
            elif age >= official_keep:
                if path == newest_official:
                    plan.add('keep', 'official', path, "the fund's newest official month, kept for historical analysis")
                elif can_archive:
                    plan.add('archive', 'official', path, f'official month {age} months old, retention {official_keep}')
                    gone += 1
                else:
                    plan.add('keep', 'official', path, f'past retention ({age} months) but no archive folder set; may not be downloadable again')
        removed_files.update({folder: gone})

        status = folder / 'status.json'
        status_goes = False
        if status.exists() and not months and not in_use and not recent(status):
            record = _read_json(status) or {}
            try:
                wait_over = datetime.fromisoformat(record.get('next_check_at', '')) <= datetime.now(timezone.utc)
                checked = datetime.fromisoformat(record.get('last_checked_at', ''))
            except ValueError:
                wait_over, checked = True, datetime.min.replace(tzinfo=timezone.utc)
            if wait_over and datetime.now(timezone.utc) - checked >= status_age:
                plan.add('delete', 'status', status, f'no holdings, retry wait over, last check {checked.date()}')
                status_goes = True
        others = [p for p in folder.iterdir() if p not in months and p != status]
        left = len(months) - gone + (0 if status_goes or not status.exists() else 1) + len(others)
        if left == 0 and not recent(folder):
            plan.add('delete', 'folder', folder, 'empty after cleanup')
    return plan


def apply_plan(plan: Plan, archive_dir: Path | None = None) -> dict[str, Any]:
    """Carry out a plan made a moment ago. Each item is checked again just before it is removed."""
    root = plan.root
    lock = root / LOCK_NAME
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
            lock.unlink(missing_ok=True)
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
    except FileExistsError:
        raise RuntimeError('Another cache cleanup is running')
    done = {'deleted': 0, 'archived': 0, 'skipped': 0, 'errors': 0, 'bytes': 0}
    active_codes = _running_job_codes(root)
    # Removing a file updates its folder's time. A folder this run emptied is not "recently changed" by anyone else.
    emptied_here: set[Path] = set()
    try:
        for action in plan.actions:
            if action.kind == 'keep':
                continue
            path = action.path
            try:
                if not path.exists():
                    continue
                if action.category == 'folder':
                    if any(path.iterdir()) or (path not in emptied_here and time.time() - path.stat().st_mtime < GRACE_SECONDS):
                        done['skipped'] += 1
                        continue
                    path.rmdir()
                    amc = path.parent
                    if amc != root and amc.name != 'jobs' and not any(amc.iterdir()):
                        amc.rmdir()
                    done['deleted'] += 1
                    continue
                # Things can change between planning and now: a job may have started, or a file been rewritten.
                if time.time() - path.stat().st_mtime < GRACE_SECONDS:
                    done['skipped'] += 1
                    continue
                if action.category in ('official', 'third_party', 'invalid'):
                    snapshot = _read_json(path) or {}
                    if active_codes & {str(c) for c in snapshot.get('scheme_codes') or []}:
                        done['skipped'] += 1
                        continue
                if action.kind == 'archive':
                    if archive_dir is None:
                        done['skipped'] += 1
                        continue
                    target = Path(archive_dir) / path.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        target = target.with_name(f'{target.stem}.{int(time.time())}{target.suffix}')
                    shutil.move(str(path), str(target))
                    done['archived'] += 1
                else:
                    path.unlink()
                    done['deleted'] += 1
                emptied_here.add(path.parent)
                done['bytes'] += action.size
            except OSError as exc:
                # Reported, never fatal: one locked file must not stop the rest of the cleanup.
                logger.warning('Cleanup could not remove %s: %s', path, exc)
                done['errors'] += 1
    finally:
        lock.unlink(missing_ok=True)
    return done


def describe(plan: Plan) -> str:
    return json.dumps(plan.summary(), indent=1)
