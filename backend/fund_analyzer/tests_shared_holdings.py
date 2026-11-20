"""Holdings files shared through Redis: a new Vercel instance starts with an empty /tmp, and must not download
and parse the AMC files again for holdings another instance already verified."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from . import portfolio_store
from .portfolio_store import clear_read_cache, forget_shared, read_snapshot, store_snapshot, stored_links
from .tests_pipeline import holdings_result

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'shared-holdings-tests'}}
DSP = {'scheme_code': '119250', 'scheme_name': 'DSP Large Cap Fund', 'amc': 'DSP Mutual Fund', 'plan': 'Direct Plan', 'option': 'Growth', 'category': 'Equity Scheme - Large Cap Fund'}
HDFCB, REL = 'INE040A01034', 'INE002A01018'


class SharedHoldingsTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='mns-shared-holdings-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        override = override_settings(CACHES=LOCMEM, FUND_ANALYZER_HOLDINGS_CACHE_DIR=self.folder, FUND_ANALYZER_HOLDINGS_SHARED_CACHE=True)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        clear_read_cache()

    def new_instance(self):
        """What a fresh serverless instance sees: an empty folder, the same Redis."""
        shutil.rmtree(self.folder, ignore_errors=True)
        Path(self.folder).mkdir()
        clear_read_cache()

    def test_verified_holdings_survive_a_new_instance(self):
        store_snapshot(DSP, '119250', '2026-10', holdings_result({HDFCB: 8.0, REL: 5.0}, '2026-10-31'), source_url='https://example.test/oct.xlsx', payload=b'oct')
        store_snapshot(DSP, '119250', '2026-09', holdings_result({HDFCB: 7.0}, '2026-09-30'), source_url='https://example.test/sep.xlsx', payload=b'sep')
        before = read_snapshot(DSP, '2026-10')
        self.new_instance()
        self.assertEqual([link['month'] for link in stored_links(DSP)], ['2026-10', '2026-09'])
        self.assertEqual(read_snapshot(DSP, '2026-10'), before)
        self.assertTrue((portfolio_store._portfolio_dir(DSP) / '2026-10.json').exists())  # written back to the folder

    def test_status_is_shared_too(self):
        portfolio_store.record_status(DSP, 'HOLDINGS_AVAILABLE', latest_month='2026-10')
        before = portfolio_store._read_status(DSP)
        self.new_instance()
        self.assertEqual(portfolio_store._read_status(DSP), before)

    def test_a_file_removed_by_cleanup_is_not_written_back(self):
        store_snapshot(DSP, '119250', '2026-10', holdings_result({HDFCB: 8.0}, '2026-10-31'), source_url='https://example.test/oct.xlsx', payload=b'oct')
        path = portfolio_store._portfolio_dir(DSP) / '2026-10.json'
        path.unlink()
        forget_shared(path)
        self.new_instance()
        self.assertIsNone(read_snapshot(DSP, '2026-10'))
        self.assertEqual(stored_links(DSP), [])

    def test_switched_off_nothing_is_shared(self):
        with override_settings(FUND_ANALYZER_HOLDINGS_SHARED_CACHE=False):
            store_snapshot(DSP, '119250', '2026-10', holdings_result({HDFCB: 8.0}, '2026-10-31'), source_url='https://example.test/oct.xlsx', payload=b'oct')
            self.new_instance()
            self.assertIsNone(read_snapshot(DSP, '2026-10'))


class GuessedFileMissTests(SimpleTestCase):
    """HDFC's listing answers 403, so its file names are guessed. A guess that is not there is not asked again for a while."""

    def setUp(self):
        override = override_settings(CACHES=LOCMEM)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()

    def test_a_missing_guess_is_remembered_with_the_same_reason(self):
        from unittest.mock import patch

        from . import holdings
        from .providers import SourceFailure

        url = 'https://files.hdfcfund.com/s3fs-public/2026-11/Monthly%20HDFC%20Large%20Cap%20Fund%20-%2031%20October%202026.xlsx'
        missing = SourceFailure('404 Client Error: Not Found', category='SOURCE_URL_MOVED', stage='download', evidence={'http_status': 404})
        with patch('fund_analyzer.holdings._download_portfolio_file', side_effect=missing) as download:
            for _ in range(3):
                with self.assertRaises(SourceFailure) as raised:
                    holdings._download_cached('hdfc', '2026-10', url, guessed=True)
            self.assertEqual(download.call_count, 1)
        self.assertEqual((raised.exception.category, raised.exception.evidence['http_status']), ('SOURCE_URL_MOVED', 404))

    def test_a_timeout_or_a_listed_file_is_not_remembered(self):
        from unittest.mock import patch

        from . import holdings
        from .providers import SourceFailure

        url = 'https://files.hdfcfund.com/s3fs-public/2026-11/other.xlsx'
        slow = SourceFailure('Read timed out', category='SOURCE_TEMPORARILY_UNAVAILABLE', stage='download', evidence={'error': 'ReadTimeout'})
        with patch('fund_analyzer.holdings._download_portfolio_file', side_effect=slow) as download:
            for _ in range(2):
                with self.assertRaises(SourceFailure):
                    holdings._download_cached('hdfc', '2026-10', url, guessed=True)
            self.assertEqual(download.call_count, 2)
        listed = SourceFailure('404', category='SOURCE_URL_MOVED', stage='download', evidence={'http_status': 404})
        with patch('fund_analyzer.holdings._download_portfolio_file', side_effect=listed) as download:
            for _ in range(2):
                with self.assertRaises(SourceFailure):
                    holdings._download_cached('sbi', '2026-10', url + '?listed', guessed=False)
            self.assertEqual(download.call_count, 2)
