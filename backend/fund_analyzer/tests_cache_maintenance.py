"""The parsed-file cache and the cleanup command. No database, no network."""
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from . import portfolio_store as ps
from .cache_maintenance import LOCK_NAME, apply_plan, build_plan
from .portfolio_store import SOURCE_CHARLIE, SOURCE_IMPORT, _portfolio_dir, _write_json_atomic, discovery_due, read_snapshot, store_snapshot, stored_links
from .tests_pipeline import HDFC_LARGE, SBI_LARGE, PortfolioCacheMixin, holdings_result

TODAY = date(2026, 11, 6)        # latest completed month: October 2026
OLD = time.time() - 3 * 24 * 3600
MONTH_END = {12: 31, 11: 30, 10: 31, 9: 30, 8: 31, 7: 31, 6: 30, 5: 31, 4: 30, 3: 31, 2: 28, 1: 31}


def month_file(record, month, weights=None, source_type=ps.SOURCE_DOWNLOAD, codes=None):
    end = f'{month}-{MONTH_END[int(month[5:])]}'
    store_snapshot(record, codes or record['scheme_code'], month, holdings_result(weights or {'INE040A01034': 5.0}, end), source_type=source_type)
    path = _portfolio_dir(record) / f'{month}.json'
    os.utime(path, (OLD, OLD))
    return path


def age_folder(record):
    os.utime(_portfolio_dir(record), (OLD, OLD))


class ParsedCacheTests(PortfolioCacheMixin, SimpleTestCase):
    def test_a_file_is_read_from_disk_once_until_it_changes(self):
        month_file(HDFC_LARGE, '2026-09')
        with patch('fund_analyzer.portfolio_store._read_json', wraps=ps._read_json) as disk:
            for _ in range(5):
                self.assertIsNotNone(read_snapshot(HDFC_LARGE, '2026-09'))
                stored_links(HDFC_LARGE)
        self.assertEqual(disk.call_count, 1)

    def test_an_atomic_rewrite_is_seen_at_once(self):
        month_file(HDFC_LARGE, '2026-09', {'INE040A01034': 5.0})
        read_snapshot(HDFC_LARGE, '2026-09')
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE090A01021': 7.0}), payload=b'new file')
        self.assertEqual([row['isin'] for row in read_snapshot(HDFC_LARGE, '2026-09')['holdings']], ['INE090A01021'])

    def test_a_corrupted_file_counts_as_missing_and_is_never_remembered(self):
        path = month_file(HDFC_LARGE, '2026-09')
        self.assertIsNotNone(read_snapshot(HDFC_LARGE, '2026-09'))
        path.write_text('{"broken": ', encoding='utf-8')
        self.assertIsNone(read_snapshot(HDFC_LARGE, '2026-09'))
        self.assertEqual(stored_links(HDFC_LARGE), [])

    def test_an_edited_file_that_fails_validation_is_refused(self):
        path = month_file(HDFC_LARGE, '2026-09')
        data = json.loads(path.read_text(encoding='utf-8'))
        data['holdings'][0]['weight'] = 250
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertIsNone(read_snapshot(HDFC_LARGE, '2026-09'))

    def test_a_deleted_file_is_a_cache_miss(self):
        path = month_file(HDFC_LARGE, '2026-09')
        read_snapshot(HDFC_LARGE, '2026-09')
        path.unlink()
        self.assertIsNone(read_snapshot(HDFC_LARGE, '2026-09'))

    def test_a_caller_cannot_change_what_the_next_reader_sees(self):
        month_file(HDFC_LARGE, '2026-09')
        read_snapshot(HDFC_LARGE, '2026-09')['source_type'] = 'tampered'
        self.assertEqual(read_snapshot(HDFC_LARGE, '2026-09')['source_type'], ps.SOURCE_DOWNLOAD)

    def test_a_crash_during_a_write_leaves_the_old_file_whole(self):
        path = month_file(HDFC_LARGE, '2026-09', {'INE040A01034': 5.0})
        with patch('fund_analyzer.portfolio_store.os.replace', side_effect=OSError('power cut')):
            with self.assertRaises(OSError):
                _write_json_atomic(path, {'half': 'written'})
        self.assertEqual(read_snapshot(HDFC_LARGE, '2026-09')['holdings'][0]['weight'], 5.0)
        self.assertEqual([p.name for p in path.parent.iterdir() if p.suffix == '.tmp'], [])


class CleanupPolicyTests(PortfolioCacheMixin, SimpleTestCase):
    def plan(self, **options):
        return build_plan(Path(self.cache_dir), today=TODAY, **options)

    def actions(self, plan):
        return {(a.kind, a.category, a.path.name if a.category != 'folder' else 'folder') for a in plan.actions}

    def test_official_retention_boundary_and_archive_only(self):
        month_file(HDFC_LARGE, '2026-10')
        month_file(HDFC_LARGE, '2025-11')      # 11 months old: kept
        month_file(HDFC_LARGE, '2025-10')      # 12 months old: past retention
        kept = self.actions(self.plan(archive=False))
        self.assertIn(('keep', 'official', '2025-10.json'), kept)
        archived = self.actions(self.plan(archive=True))
        self.assertIn(('archive', 'official', '2025-10.json'), archived)
        self.assertNotIn('2025-11.json', {name for _, _, name in archived})
        self.assertNotIn('2026-10.json', {name for _, _, name in archived})

    def test_the_newest_official_month_is_kept_however_old(self):
        month_file(SBI_LARGE, '2024-03')
        month_file(SBI_LARGE, '2024-02')
        actions = self.actions(self.plan(archive=True))
        self.assertIn(('keep', 'official', '2024-03.json'), actions)
        self.assertIn(('archive', 'official', '2024-02.json'), actions)

    def test_retention_is_never_shorter_than_the_official_lookback(self):
        month_file(HDFC_LARGE, '2026-10')
        month_file(HDFC_LARGE, '2026-05')      # 5 months old, inside the 6-month lookback
        self.assertNotIn('2026-05.json', {a.path.name for a in self.plan(official_months=1, archive=True).actions if a.kind != 'keep'})

    def test_third_party_retention_boundary(self):
        month_file(HDFC_LARGE, '2026-09')
        month_file(HDFC_LARGE, '2026-05', source_type=SOURCE_CHARLIE)    # 5 months: kept
        month_file(HDFC_LARGE, '2026-04', source_type=SOURCE_CHARLIE)    # 6 months: deleted
        actions = self.actions(self.plan())
        self.assertIn(('delete', 'third_party', '2026-04.json'), actions)
        self.assertNotIn('2026-05.json', {name for kind, _, name in actions if kind == 'delete'})

    def test_staff_uploads_are_never_removed(self):
        month_file(HDFC_LARGE, '2026-09')
        month_file(HDFC_LARGE, '2024-01', source_type=SOURCE_IMPORT)
        self.assertIn(('keep', 'official', '2024-01.json'), self.actions(self.plan(archive=True)))

    def test_a_running_job_protects_its_funds(self):
        month_file(HDFC_LARGE, '2026-09')
        month_file(HDFC_LARGE, '2024-01')
        jobs = Path(self.cache_dir) / 'jobs'
        jobs.mkdir()
        (jobs / ('a' * 32 + '.json')).write_text(json.dumps({'status': 'running', 'scheme_codes': ['119018'], 'updated_at': time.time()}), encoding='utf-8')
        actions = self.plan(archive=True).actions
        self.assertFalse([a for a in actions if a.kind != 'keep' and a.category == 'official'])
        self.assertIn('used by a running analysis job', {a.reason for a in actions})

    def test_a_recently_changed_file_is_never_removed(self):
        month_file(HDFC_LARGE, '2026-09')
        path = month_file(HDFC_LARGE, '2024-01')
        os.utime(path, None)
        self.assertNotIn(('archive', 'official', '2024-01.json'), self.actions(self.plan(archive=True)))

    def test_an_unreadable_month_file_is_archived_or_kept_never_deleted(self):
        path = month_file(HDFC_LARGE, '2026-09')
        bad = path.with_name('2026-08.json')
        bad.write_text('{not json', encoding='utf-8')
        os.utime(bad, (OLD, OLD))
        self.assertIn(('keep', 'invalid', '2026-08.json'), self.actions(self.plan(archive=False)))
        self.assertIn(('archive', 'invalid', '2026-08.json'), self.actions(self.plan(archive=True)))

    def test_status_kept_during_its_retry_wait_and_removed_once_long_expired(self):
        now = datetime.now(timezone.utc)
        cooling = _portfolio_dir(HDFC_LARGE) / 'status.json'
        _write_json_atomic(cooling, {'status': 'SOURCE_NOT_SUPPORTED', 'last_checked_at': (now - timedelta(days=40)).isoformat(), 'next_check_at': (now + timedelta(days=1)).isoformat()})
        expired = _portfolio_dir(SBI_LARGE) / 'status.json'
        _write_json_atomic(expired, {'status': 'SOURCE_NOT_SUPPORTED', 'last_checked_at': (now - timedelta(days=40)).isoformat(), 'next_check_at': (now - timedelta(days=33)).isoformat()})
        for path in (cooling, expired):
            os.utime(path, (OLD, OLD))
            os.utime(path.parent, (OLD, OLD))
        plan = self.plan()
        removed = {(a.category, a.path) for a in plan.actions if a.kind == 'delete'}
        self.assertNotIn(('status', cooling), removed)
        self.assertIn(('status', expired), removed)
        self.assertIn(('folder', expired.parent), removed)
        self.assertFalse(discovery_due(HDFC_LARGE))
        apply_plan(plan)
        self.assertTrue(cooling.exists())
        self.assertFalse(expired.parent.exists())
        # With the record gone the fund is simply checked again next time, exactly as an expired wait allows.
        self.assertTrue(discovery_due(SBI_LARGE))

    def test_recent_status_with_no_holdings_is_kept(self):
        ps.record_status(HDFC_LARGE, 'SOURCE_NOT_SUPPORTED', 'no files')
        self.assertFalse([a for a in self.plan().actions if a.kind == 'delete'])

    def test_abandoned_temporary_files(self):
        folder = _portfolio_dir(HDFC_LARGE)
        folder.mkdir(parents=True)
        old, new = folder / '.2026-09-abc.tmp', folder / '.2026-09-def.tmp'
        for path in (old, new):
            path.write_text('partial', encoding='utf-8')
        os.utime(old, (time.time() - 25 * 3600,) * 2)
        os.utime(new, (time.time() - 2 * 3600,) * 2)
        names = {a.path.name for a in self.plan().actions if a.category == 'temp'}
        self.assertEqual(names, {'.2026-09-abc.tmp'})

    def test_finished_jobs_go_running_jobs_stay(self):
        jobs = Path(self.cache_dir) / 'jobs'
        jobs.mkdir()
        done, running = jobs / ('b' * 32 + '.json'), jobs / ('c' * 32 + '.json')
        done.write_text(json.dumps({'status': 'done', 'updated_at': OLD}), encoding='utf-8')
        running.write_text(json.dumps({'status': 'running', 'scheme_codes': [], 'updated_at': time.time()}), encoding='utf-8')
        os.utime(done, (OLD, OLD))
        os.utime(running, (OLD, OLD))
        kinds = {a.path.name: a.kind for a in self.plan().actions if a.category == 'job'}
        self.assertEqual(kinds, {done.name: 'delete', running.name: 'keep'})


class CleanupCommandTests(PortfolioCacheMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        month_file(HDFC_LARGE, '2026-09')
        self.third = month_file(HDFC_LARGE, '2025-01', source_type=SOURCE_CHARLIE)

    def run_command(self, *args):
        out = StringIO()
        with patch('fund_analyzer.cache_maintenance.date') as fake_date:
            fake_date.today.return_value = TODAY
            call_command('cleanup_portfolio_cache', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_is_the_default_and_changes_nothing(self):
        before = sorted(p for p in Path(self.cache_dir).rglob('*'))
        output = self.run_command()
        self.assertIn('DRY RUN', output)
        self.assertIn('2025-01.json', output)
        self.assertEqual(sorted(p for p in Path(self.cache_dir).rglob('*')), before)

    def test_apply_removes_and_reports_reclaimed_space(self):
        output = self.run_command('--apply')
        self.assertFalse(self.third.exists())
        self.assertIn('Deleted 1', output)
        self.assertIsNotNone(read_snapshot(HDFC_LARGE, '2026-09'))

    def test_official_files_go_to_the_archive_not_the_bin(self):
        old = month_file(HDFC_LARGE, '2024-05')
        with tempfile.TemporaryDirectory() as archive:
            self.run_command('--apply', '--archive-dir', archive)
            self.assertFalse(old.exists())
            moved = list(Path(archive).rglob('2024-05.json'))
            self.assertEqual(len(moved), 1)
            self.assertEqual(json.loads(moved[0].read_text(encoding='utf-8'))['month'], '2024-05')

    def test_each_run_is_appended_to_the_log_with_its_time_and_counts(self):
        with tempfile.TemporaryDirectory() as logs:
            log = Path(logs) / 'nested' / 'cleanup.log'
            self.run_command('--log-file', str(log))
            self.run_command('--apply', '--log-file', str(log))
            text = log.read_text(encoding='utf-8')
        self.assertEqual(text.count('=== '), 2)
        self.assertRegex(text, r'=== \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
        self.assertIn('(DRY RUN)', text)
        self.assertIn('Result: dry run, nothing changed', text)
        self.assertIn('Result: Deleted 1, archived 0, skipped 0 (changed or in use), errors 0', text)
        self.assertIn('[delete] third_party', text)
        self.assertIn('Duration:', text)

    def test_a_failed_run_is_logged_as_failed(self):
        (Path(self.cache_dir) / LOCK_NAME).write_text('1', encoding='utf-8')
        from django.core.management.base import CommandError
        with tempfile.TemporaryDirectory() as logs:
            log = Path(logs) / 'cleanup.log'
            with self.assertRaises(CommandError):
                self.run_command('--apply', '--log-file', str(log))
            self.assertIn('Result: FAILED - Another cache cleanup is running', log.read_text(encoding='utf-8'))

    def test_a_locked_file_is_counted_as_an_error_and_the_rest_continues(self):
        plan = build_plan(Path(self.cache_dir), today=TODAY)
        real_unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path.name == '2025-01.json':
                raise PermissionError('in use')
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', autospec=True, side_effect=locked):
            done = apply_plan(plan)
        self.assertEqual((done['errors'], done['deleted']), (1, 0))
        self.assertTrue(self.third.exists())

    def test_two_cleanups_cannot_run_at_once(self):
        (Path(self.cache_dir) / LOCK_NAME).write_text('1', encoding='utf-8')
        from django.core.management.base import CommandError
        with self.assertRaisesRegex(CommandError, 'Another cache cleanup'):
            self.run_command('--apply')
        self.assertTrue(self.third.exists())

    def test_a_file_rewritten_after_planning_is_skipped(self):
        plan = build_plan(Path(self.cache_dir), today=TODAY)
        os.utime(self.third, None)
        self.assertEqual(apply_plan(plan)['skipped'], 1)
        self.assertTrue(self.third.exists())

    def test_overlap_is_unchanged_after_cleanup(self):
        month_file(SBI_LARGE, '2026-09', {'INE040A01034': 3.0})
        self.run_command('--apply')
        from .holdings import analyze_holdings_overlap
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[HDFC_LARGE, SBI_LARGE]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value={'2026-10', '2026-09', '2026-08'}),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
        ):
            result = analyze_holdings_overlap(['119018', '119598'])
        self.assertEqual((result['pairs'][0]['overlap_pct'], result['common_month']), (3.0, '2026-09'))
