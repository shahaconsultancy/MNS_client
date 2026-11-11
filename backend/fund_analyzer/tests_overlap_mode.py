"""Overlap execution mode: background jobs where a long-lived process exists, one request on Vercel.

Vercel freezes a function once it has replied and may answer the next request from another instance
with its own /tmp, so a job started there is lost and its poll answers 404 ("Overlap job not found or
expired"). There the job endpoint answers 405 and the page uses POST /api/overlap, which runs the very
same calculation in one request. Nothing about the calculation itself differs between the two paths.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from . import charlie_alpha as ca, holdings, jobs, source_health as sh
from .portfolio_store import clear_read_cache
from .providers import SourceFailure
from .tests_charlie import FakeProvider, complete, make_isin, payload
from .tests_pipeline import LOCMEM, portfolio_file

BACKEND = Path(__file__).resolve().parents[1]
HDFCB, ICICIB, REL, INFY = 'INE040A01034', 'INE090A01021', 'INE002A01018', 'INE009A01021'
FIRST = {'scheme_code': '901', 'scheme_name': 'First Flexi Cap Fund', 'amc': 'First Mutual Fund', 'plan': 'Direct Plan', 'option': 'Growth', 'category': 'Equity Scheme - Flexi Cap Fund', 'date': '07-Nov-2026', 'nav': 10.0, 'isin': make_isin('INF901A0101')}
SECOND = {'scheme_code': '902', 'scheme_name': 'Second Large Cap Fund', 'amc': 'Second Mutual Fund', 'plan': 'Direct Plan', 'option': 'Growth', 'category': 'Equity Scheme - Large Cap Fund', 'date': '07-Nov-2026', 'nav': 10.0, 'isin': make_isin('INF902A0101')}
THIRD = {'scheme_code': '903', 'scheme_name': 'Third Small Cap Fund', 'amc': 'Third Mutual Fund', 'plan': 'Direct Plan', 'option': 'Growth', 'category': 'Equity Scheme - Small Cap Fund', 'date': '07-Nov-2026', 'nav': 10.0, 'isin': make_isin('INF903A0101')}
DIRECTORY = {'First Mutual Fund': 'https://www.first.test/portfolio', 'Second Mutual Fund': 'https://www.second.test/portfolio', 'Third Mutual Fund': 'https://www.third.test/portfolio'}


def setting_in_a_fresh_process(**env) -> str:
    """FUND_ANALYZER_OVERLAP_JOBS_ENABLED as settings.py computes it, in a process with exactly this environment."""
    clean = {key: value for key, value in os.environ.items() if key not in ('VERCEL', 'FUND_ANALYZER_OVERLAP_JOBS_ENABLED')}
    code = ("import os,sys; sys.path.insert(0, '.'); os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; "
            "from django.conf import settings; print(settings.FUND_ANALYZER_OVERLAP_JOBS_ENABLED)")
    return subprocess.run([sys.executable, '-c', code], cwd=BACKEND, env={**clean, **env}, capture_output=True, text=True, timeout=60).stdout.strip()


class ModeDetectionTests(SimpleTestCase):
    def test_vercel_runs_overlap_in_one_request(self):
        self.assertEqual(setting_in_a_fresh_process(VERCEL='1'), 'False')

    def test_local_development_keeps_background_jobs(self):
        self.assertEqual(setting_in_a_fresh_process(), 'True')

    def test_an_explicit_setting_wins_either_way(self):
        self.assertEqual(setting_in_a_fresh_process(VERCEL='1', FUND_ANALYZER_OVERLAP_JOBS_ENABLED='true'), 'True')
        self.assertEqual(setting_in_a_fresh_process(FUND_ANALYZER_OVERLAP_JOBS_ENABLED='false'), 'False')


@override_settings(CACHES=LOCMEM)
class JobEndpointModeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    @override_settings(FUND_ANALYZER_OVERLAP_JOBS_ENABLED=False)
    def test_serverless_job_request_is_refused_with_405_and_starts_nothing(self):
        with patch('fund_analyzer.views.start_job') as start:
            reply = self.client.post('/api/overlap/jobs', {'scheme_codes': ['901', '902']}, format='json')
        start.assert_not_called()
        self.assertEqual((reply.status_code, reply.json()['mode']), (405, 'synchronous'))

    @override_settings(FUND_ANALYZER_OVERLAP_JOBS_ENABLED=True)
    def test_local_job_request_starts_a_job(self):
        job = {'job_id': 'a' * 32, 'status': 'queued', 'stage': 'queued', 'message': 'Waiting to start...'}
        with patch('fund_analyzer.views.start_job', return_value=job) as start:
            reply = self.client.post('/api/overlap/jobs', {'scheme_codes': ['901', '902']}, format='json')
        start.assert_called_once_with(['901', '902'], None)
        self.assertEqual((reply.status_code, reply.json()['job_id']), (202, 'a' * 32))

    def test_a_missing_job_still_answers_404_so_the_page_can_fall_back_once(self):
        reply = self.client.get(f"/api/overlap/jobs/{'b' * 32}")
        self.assertEqual((reply.status_code, reply.json()['status']), (404, 'missing'))


@override_settings(CACHES=LOCMEM, FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=True)
class SameResultBothWaysTests(SimpleTestCase):
    """Three funds: two official (October and September published), one only in the backup for September."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        cache.clear()
        clear_read_cache()
        ca.clear_memory()
        self.addCleanup(ca.clear_memory)
        sh._last_request.clear()
        self.client = APIClient()

    def links(self, record):
        slug = record['amc'].split()[0].lower()
        return [{'url': f'https://www.{slug}.test/{month}.xlsx', 'text': f'Monthly Portfolio {label}', 'month': month, 'portfolio_date': date}
                for month, label, date in (('2026-10', 'October 2026', '2026-10-31'), ('2026-09', 'September 2026', '2026-09-30'))]

    def files(self):
        rows = {FIRST['scheme_code']: [(HDFCB, 10.0), (ICICIB, 5.0), (INFY, 4.0)], SECOND['scheme_code']: [(HDFCB, 6.0), (REL, 3.0)]}
        result = {}
        for record in (FIRST, SECOND):
            for link in self.links(record):
                reported = 'October 31, 2026' if link['month'] == '2026-10' else 'September 30, 2026'
                result[link['url']] = portfolio_file(record['scheme_name'], rows[record['scheme_code']], reporting_date=reported)
        return result

    def run_both_ways(self):
        files = self.files()
        only_third = {make_isin(f'INE88{n}A0101'): 9.25 for n in range(8)}
        provider = FakeProvider({'903': {'2026-09-30': payload(THIRD, '2026-09-30', complete({HDFCB: 8.0, ICICIB: 6.0, REL: 7.0, **only_third}))}})
        listings = {'first-mutual-fund': self.links(FIRST), 'second-mutual-fund': self.links(SECOND)}

        def load(amc_key, page_url, parser_key):
            if amc_key in listings:
                return listings[amc_key]
            raise SourceFailure('No monthly portfolio files found', category=sh.DISCOVERY_FAILED)

        def fresh_cache():
            # Each path starts from nothing, so neither can borrow the other's saved months.
            folder = tempfile.mkdtemp(dir=self.folder)
            cache.clear()
            clear_read_cache()
            ca.clear_memory()
            return override_settings(FUND_ANALYZER_HOLDINGS_CACHE_DIR=folder)

        common = (
            patch('fund_analyzer.adapters.fetch_amfi_disclosure_directory', return_value=DIRECTORY),
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[FIRST, SECOND, THIRD]),
            patch.object(holdings, '_load_page_links', side_effect=load),
            patch.object(holdings, '_download_cached', side_effect=lambda key, month, url, **kw: files[url]),
            patch.object(ca, '_get_json', provider),
        )
        body = {'scheme_codes': ['901', '902', '903']}
        for starter in common:
            starter.start()
            self.addCleanup(starter.stop)
        with fresh_cache(), override_settings(FUND_ANALYZER_OVERLAP_JOBS_ENABLED=False):
            self.assertEqual(self.client.post('/api/overlap/jobs', body, format='json').status_code, 405)
            synchronous = self.client.post('/api/overlap', body, format='json')
        self.assertEqual(synchronous.status_code, 200, synchronous.content)
        with fresh_cache(), override_settings(FUND_ANALYZER_OVERLAP_JOBS_ENABLED=True), patch.object(jobs._executor, 'submit', side_effect=lambda fn, *args: fn(*args)):
            started = self.client.post('/api/overlap/jobs', body, format='json')
            polled = self.client.get(f"/api/overlap/jobs/{started.json()['job_id']}")
        self.assertEqual(polled.json()['status'], 'done', polled.content)
        return synchronous.json(), polled.json()['result']

    def test_both_paths_give_identical_pairs_months_and_source_labels(self):
        synchronous, from_job = self.run_both_ways()
        self.assertEqual(synchronous['pairs'], from_job['pairs'])
        self.assertEqual(synchronous['funds'], from_job['funds'])
        self.assertEqual(synchronous['highest_overlap'], from_job['highest_overlap'])
        self.assertNotIn('debug', synchronous)
        self.assertNotIn('debug', from_job)

    def test_three_funds_give_three_same_month_pairs_with_correct_sources(self):
        result, _ = self.run_both_ways()
        funds = {fund['scheme_code']: fund for fund in result['funds']}
        self.assertEqual({code: (fund['holdings_source'], fund['holdings_source_label']) for code, fund in funds.items()}, {
            '901': ('official_amc', 'Official AMC'),
            '902': ('official_amc', 'Official AMC'),
            '903': ('charlie_alpha', 'Third-party API — Charlie Alpha'),
        })
        pairs = {(pair['left']['scheme_code'], pair['right']['scheme_code']): pair for pair in result['pairs']}
        self.assertEqual(set(pairs), {('901', '902'), ('901', '903'), ('902', '903')})
        # One month common to all funds; both sides of every pair use it. Official files are used for
        # September because the backup covers only the fund with no official file.
        self.assertEqual({pair['portfolio_month'] for pair in pairs.values()}, {'2026-09'})
        self.assertTrue(all(pair['month_basis'] == 'COMMON_TO_ALL' and pair['left_portfolio_date'] == pair['right_portfolio_date'] == '2026-09-30' for pair in pairs.values()))
        self.assertEqual(round(pairs[('901', '903')]['overlap_pct'], 2), 13.0)  # min(10,8) + min(5,6)
        self.assertEqual(round(pairs[('901', '902')]['overlap_pct'], 2), 6.0)   # min(10,6)
        self.assertEqual(round(pairs[('902', '903')]['overlap_pct'], 2), 9.0)   # min(6,8) + min(3,7)
        self.assertEqual((pairs[('901', '902')]['holdings_source'], pairs[('901', '903')]['holdings_source_label']), ('official_amc', 'Official AMC and Third-party API — Charlie Alpha'))

    @override_settings(FUND_ANALYZER_OVERLAP_JOBS_ENABLED=False)
    def test_a_genuine_calculation_failure_keeps_its_own_reason(self):
        from .providers import ProviderError

        with patch('fund_analyzer.views.compare_holdings_overlap', side_effect=ProviderError('AMFI scheme list unavailable')):
            reply = self.client.post('/api/overlap', {'scheme_codes': ['901', '902']}, format='json')
        self.assertEqual((reply.status_code, reply.json()['detail'], reply.json()['available']), (503, 'Portfolio holdings unavailable', False))
