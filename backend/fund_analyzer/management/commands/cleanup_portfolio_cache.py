"""Report, and optionally carry out, cleanup of the JSON holdings cache.

    python manage.py cleanup_portfolio_cache              dry run: lists what would go, changes nothing
    python manage.py cleanup_portfolio_cache --apply      removes it
    python manage.py cleanup_portfolio_cache --apply --archive-dir D:\\mns\\portfolio_archive --log-file D:\\mns\\logs\\portfolio_cache_cleanup.log

Meant for a daily or weekly scheduled task, never for a web request.
"""
import logging
import time
import traceback
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fund_analyzer.cache_maintenance import apply_plan, build_plan

logger = logging.getLogger('fund_analyzer.cache_maintenance')


class Command(BaseCommand):
    help = 'Report (default) or remove old and unused files in the portfolio holdings cache.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Remove files. Without this, nothing is changed.')
        parser.add_argument('--archive-dir', default=settings.FUND_ANALYZER_CACHE_ARCHIVE_DIR, help='Move official and unreadable files here instead of keeping them.')
        parser.add_argument('--log-file', default='', help='Append a summary of this run to this file.')
        parser.add_argument('--official-months', type=int, help='Official months to keep (never fewer than the official lookback).')
        parser.add_argument('--backup-months', type=int, help='Third-party months to keep (never fewer than the backup lookback).')
        parser.add_argument('--temp-hours', type=int, help='Age after which an abandoned temporary file is removed.')
        parser.add_argument('--status-days', type=int, help='Days after the last check before an expired status is removed.')
        parser.add_argument('--verbose-list', action='store_true', help='List every file, including protected ones.')

    def handle(self, *args, **options):
        started = time.monotonic()
        started_at = datetime.now().astimezone()
        log_lines = [f"=== {started_at.isoformat(timespec='seconds')} cleanup_portfolio_cache ({'APPLY' if options['apply'] else 'DRY RUN'})"]
        try:
            self._run(options, log_lines)
        except Exception as exc:
            log_lines.append(f'Result: FAILED - {exc}')
            if not isinstance(exc, CommandError):
                log_lines.append(traceback.format_exc().rstrip())
            raise
        finally:
            log_lines.append(f'Duration: {time.monotonic() - started:.1f}s')
            self._write_log(options['log_file'], log_lines)

    def _run(self, options, log_lines):
        archive_dir = Path(options['archive_dir']) if options['archive_dir'] else None
        root = Path(settings.FUND_ANALYZER_HOLDINGS_CACHE_DIR)
        if archive_dir and (root.resolve() in archive_dir.resolve().parents or archive_dir.resolve() == root.resolve()):
            raise CommandError('The archive folder must be outside the cache folder')
        plan = build_plan(
            official_months=options['official_months'], backup_months=options['backup_months'],
            temp_hours=options['temp_hours'], status_days=options['status_days'], archive=archive_dir is not None,
        )
        summary = plan.summary()
        mode = 'APPLY' if options['apply'] else 'DRY RUN (nothing changed)'
        self.stdout.write(f'Portfolio cache: {plan.root}\nMode: {mode}')
        self.stdout.write(f"Would remove {summary['removable_files']} files and {summary['removable_folders']} empty folders, "
                          f"reclaiming {summary['reclaimable_bytes'] / 1024:.1f} KB. Protected: {summary['kept_protected']}.")
        for key, totals in summary['by_kind'].items():
            self.stdout.write(f"  {key:22} {totals['files']:5} files  {totals['bytes'] / 1024:8.1f} KB")
        log_lines += [
            f'Cache: {plan.root}',
            f"Archive: {archive_dir or 'not set (official files past retention are kept)'}",
            f"Planned: {summary['removable_files']} files and {summary['removable_folders']} empty folders, "
            f"{summary['reclaimable_bytes'] / 1024:.1f} KB; protected {summary['kept_protected']}",
        ]
        for action in plan.actions:
            line = f'[{action.kind}] {action.category} {action.path.relative_to(plan.root)} ({action.reason})'
            if action.kind == 'keep':
                logger.info('Protected %s: %s', action.path, action.reason)
            log_lines.append(f'  {line}')
            if options['verbose_list'] or action.kind != 'keep':
                self.stdout.write(f'  {line}')
        if not options['apply']:
            log_lines.append('Result: dry run, nothing changed')
            return
        try:
            done = apply_plan(plan, archive_dir)
        except RuntimeError as exc:
            raise CommandError(str(exc))
        result = (f"Deleted {done['deleted']}, archived {done['archived']}, skipped {done['skipped']} (changed or in use), "
                  f"errors {done['errors']}, reclaimed {done['bytes'] / 1024:.1f} KB.")
        log_lines.append(f'Result: {result}')
        self.stdout.write(self.style.SUCCESS(result) if not done['errors'] else self.style.WARNING(result))

    def _write_log(self, log_file, lines):
        if not log_file:
            return
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write('\n'.join(lines) + '\n\n')
        except OSError as exc:
            # A log that can't be written must not hide the cleanup result.
            self.stderr.write(f'Could not write the cleanup log {log_file}: {exc}')
