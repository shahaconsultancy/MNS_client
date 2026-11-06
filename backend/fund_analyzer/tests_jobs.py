"""Tests for on-demand overlap jobs, download locking and failure reasons. These use no database."""
import threading
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIClient

from . import holdings, jobs
from .adapters import DISCLOSURE_NOT_PUBLISHED, DOWNLOAD_BLOCKED, DOWNLOAD_FAILED, INVALID_WORKBOOK, PARSING_FAILED, SOURCE_NOT_SUPPORTED
from .holdings import _fund_reason_code, analyze_holdings_overlap
from .portfolio_store import store_snapshot
from .providers import ProviderError
from .tests_debt import HDFC_DEBT, RECENT, SBI_DEBT
from .tests_historical import debt_month
from .tests_pipeline import PortfolioCacheMixin


class _Inline:
    """Runs a job at once, so a test can see its finished state."""

    def submit(self, function, *args):
        function(*args)


class _Held:
    """Keeps a job queued, as a slow download would."""

    def __init__(self):
        self.submitted = []

    def submit(self, function, *args):
        self.submitted.append((function, args))


class JobTests(PortfolioCacheMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        jobs._active.clear()
        self.addCleanup(jobs._active.clear)

    def test_a_job_runs_through_its_stages_and_keeps_the_result(self):
        seen = []

        def compare(codes, month, progress=None):
            job = jobs.get_job(next(iter(jobs._active.values())))
            seen.append(('start', job['status']))
            for stage in ('fetching', 'validating', 'calculating'):
                progress(stage)
                seen.append((stage, jobs.get_job(job['job_id'])['message']))
            return {'pairs': [{'overlap_pct': 4.0}], 'debug': ['internal'], 'mode': 'current'}

        with patch.object(jobs, '_executor', _Inline()), patch('fund_analyzer.services.compare_holdings_overlap', side_effect=compare):
            started = jobs.start_job(['119018', '119598'])
        self.assertEqual(seen, [('start', 'running'), ('fetching', 'Fetching portfolio holdings...'), ('validating', 'Validating holdings...'), ('calculating', 'Calculating overlap...')])
        finished = jobs.get_job(started['job_id'])
        self.assertEqual((finished['status'], finished['stage'], finished['message']), ('done', 'done', 'Overlap ready'))
        self.assertEqual(finished['result']['pairs'], [{'overlap_pct': 4.0}])
        self.assertNotIn('debug', finished['result'])

    def test_the_same_request_joins_the_running_job_instead_of_starting_another(self):
        held = _Held()
        with patch.object(jobs, '_executor', held):
            first = jobs.start_job(['119018', '119598'])
            again = jobs.start_job(['119018', '119598'])
            other = jobs.start_job(['119018', '128629'])
            month = jobs.start_job(['119018', '119598'], '2026-09')
        self.assertEqual(first['job_id'], again['job_id'])
        self.assertEqual(len({first['job_id'], other['job_id'], month['job_id']}), 3)
        self.assertEqual(len(held.submitted), 3)

    def test_a_finished_job_is_not_reused(self):
        with patch.object(jobs, '_executor', _Inline()), patch('fund_analyzer.services.compare_holdings_overlap', return_value={'pairs': []}):
            first = jobs.start_job(['1', '2'])
            second = jobs.start_job(['1', '2'])
        self.assertNotEqual(first['job_id'], second['job_id'])

    def test_a_provider_failure_is_reported_not_hidden(self):
        with patch.object(jobs, '_executor', _Inline()), patch('fund_analyzer.services.compare_holdings_overlap', side_effect=ProviderError('AMFI down')):
            started = jobs.start_job(['1', '2'])
        failed = jobs.get_job(started['job_id'])
        self.assertEqual((failed['status'], failed['error']), ('failed', 'Portfolio holdings unavailable'))
        self.assertNotIn('result', failed)

    def test_an_unexpected_crash_fails_the_job_and_frees_the_request(self):
        with patch.object(jobs, '_executor', _Inline()), patch('fund_analyzer.services.compare_holdings_overlap', side_effect=RuntimeError('boom')):
            started = jobs.start_job(['1', '2'])
        self.assertEqual(jobs.get_job(started['job_id'])['status'], 'failed')
        self.assertEqual(jobs._active, {})

    def test_job_ids_cannot_reach_other_files(self):
        for bad in ('../status', '..\\x', 'a' * 31, 'G' * 32, ''):
            self.assertIsNone(jobs.get_job(bad), bad)

    def test_the_endpoints_start_and_report_a_job(self):
        client = APIClient()
        with patch.object(jobs, '_executor', _Inline()), patch('fund_analyzer.services.compare_holdings_overlap', return_value={'pairs': [], 'highest_overlap': {'available': False}}):
            started = client.post('/api/overlap/jobs', {'scheme_codes': ['1', '2']}, format='json')
        self.assertEqual(started.status_code, 202)
        done = client.get(f"/api/overlap/jobs/{started.data['job_id']}")
        self.assertEqual((done.status_code, done.data['status']), (200, 'done'))
        self.assertEqual(done.data['result']['highest_overlap'], {'available': False})
        self.assertEqual(client.get('/api/overlap/jobs/' + 'f' * 32).status_code, 404)

    def test_a_running_job_reports_its_stage_without_a_result(self):
        client = APIClient()
        with patch.object(jobs, '_executor', _Held()):
            started = client.post('/api/overlap/jobs', {'scheme_codes': ['1', '2']}, format='json')
        status = client.get(f"/api/overlap/jobs/{started.data['job_id']}").data
        self.assertEqual((status['status'], status['message']), ('queued', 'Waiting to start...'))
        self.assertNotIn('result', status)

    def test_the_endpoint_rejects_a_bad_request(self):
        self.assertEqual(APIClient().post('/api/overlap/jobs', {'scheme_codes': ['1', '1']}, format='json').status_code, 400)
        self.assertEqual(APIClient().post('/api/overlap/jobs', {'scheme_codes': ['1', '2'], 'portfolio_month': '2099-01'}, format='json').status_code, 400)


class StageTests(PortfolioCacheMixin, SimpleTestCase):
    def test_stages_are_reported_in_order_during_a_real_analysis(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', debt_month({'INE296A07SV1': 5.0}, '2026-09'))
        store_snapshot(SBI_DEBT, '146215', '2026-09', debt_month({'INE296A07SV1': 4.0}, '2026-09'))
        stages = []
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[HDFC_DEBT, SBI_DEBT]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
        ):
            result = analyze_holdings_overlap(['128629', '146215'], progress=stages.append)
        self.assertEqual(stages, ['fetching', 'validating', 'calculating'])
        self.assertEqual(result['pairs'][0]['overlap_pct'], 4.0)


class DownloadLockTests(SimpleTestCase):
    def setUp(self):
        holdings._portfolio_file_cache.clear()
        self.addCleanup(holdings._portfolio_file_cache.clear)

    def _run(self, urls):
        results, errors = [], []

        def worker(url):
            try:
                results.append(holdings._download_cached('amc', '2026-09', url))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(url,)) for url in urls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        return results, errors

    def test_concurrent_requests_for_one_file_download_it_once(self):
        calls, release = [], threading.Event()

        def download(_amc, url):
            calls.append(url)
            release.wait(2)
            return b'workbook'

        with patch.object(holdings, '_download_portfolio_file', side_effect=download):
            threading.Timer(0.3, release.set).start()
            results, errors = self._run(['https://amc.test/a.xlsx'] * 5)
        self.assertEqual((len(calls), results, errors), (1, [b'workbook'] * 5, []))

    def test_different_files_download_side_by_side(self):
        # Both downloads must be in flight at once. A single global lock would leave one waiting forever.
        both_started = threading.Barrier(2, timeout=3)

        def download(_amc, url):
            both_started.wait()
            return url.encode()

        with patch.object(holdings, '_download_portfolio_file', side_effect=download):
            results, errors = self._run(['https://amc.test/a.xlsx', 'https://amc.test/b.xlsx'])
        self.assertEqual((sorted(results), errors), ([b'https://amc.test/a.xlsx', b'https://amc.test/b.xlsx'], []))

    def test_a_failed_download_is_not_cached_and_can_be_retried(self):
        attempts = []

        def download(_amc, url):
            attempts.append(url)
            if len(attempts) == 1:
                raise ProviderError('403 Forbidden')
            return b'ok'

        with patch.object(holdings, '_download_portfolio_file', side_effect=download):
            with self.assertRaises(ProviderError):
                holdings._download_cached('amc', '2026-09', 'https://amc.test/a.xlsx')
            self.assertEqual(holdings._download_cached('amc', '2026-09', 'https://amc.test/a.xlsx'), b'ok')


class FailureReasonTests(SimpleTestCase):
    def _reason(self, status):
        return _fund_reason_code({'available': False, 'status': status})

    def test_each_failure_has_its_own_reason(self):
        self.assertEqual(self._reason(SOURCE_NOT_SUPPORTED), 'SOURCE_NOT_SUPPORTED')
        self.assertEqual(self._reason(DOWNLOAD_BLOCKED), 'DOWNLOAD_BLOCKED')
        self.assertEqual(self._reason(DISCLOSURE_NOT_PUBLISHED), 'OFFICIAL_FILE_UNAVAILABLE')
        self.assertEqual(self._reason(DOWNLOAD_FAILED), 'OFFICIAL_FILE_UNAVAILABLE')
        self.assertEqual(self._reason(PARSING_FAILED), 'PARSER_UNSUPPORTED')
        self.assertEqual(self._reason(INVALID_WORKBOOK), 'PARSER_UNSUPPORTED')
        self.assertEqual(self._reason('NO_COMMON_REPORTING_MONTH'), 'HOLDINGS_UNAVAILABLE')

    def test_an_available_fund_has_no_failure_reason(self):
        self.assertIsNone(_fund_reason_code({'available': True, 'status': 'HOLDINGS_AVAILABLE'}))


class FundStatusMessageTests(PortfolioCacheMixin, SimpleTestCase):
    def test_a_blocked_source_is_named_in_the_fund_result(self):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[HDFC_DEBT, SBI_DEBT]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=ProviderError('403 Client Error: Forbidden')),
        ):
            result = analyze_holdings_overlap(['128629', '146215'])
        fund = result['funds'][0]
        self.assertEqual((fund['available'], fund['reason_code'], fund['status_message']), (False, 'DOWNLOAD_BLOCKED', 'Download blocked by the AMC website'))
        self.assertIsNone(result['highest_overlap']['percentage'])
        self.assertEqual(result['pair_reasons'][0]['reason_code'], 'DOWNLOAD_BLOCKED')


class FundCountTests(SimpleTestCase):
    def test_any_reasonable_number_of_funds_can_be_selected(self):
        from .serializers import MAX_FUNDS_PER_REQUEST, CompareRequestSerializer
        for count in (2, 8, 9, 25, MAX_FUNDS_PER_REQUEST):
            self.assertTrue(CompareRequestSerializer(data={'scheme_codes': [str(n) for n in range(count)]}).is_valid(), count)

    def test_only_an_absurd_request_is_refused(self):
        from .serializers import MAX_FUNDS_PER_REQUEST, CompareRequestSerializer
        self.assertFalse(CompareRequestSerializer(data={'scheme_codes': [str(n) for n in range(MAX_FUNDS_PER_REQUEST + 1)]}).is_valid())


class ManyFundsOverlapTests(PortfolioCacheMixin, SimpleTestCase):
    def test_twelve_selected_funds_give_every_pair_with_the_right_overlap(self):
        # Fund n holds the same bond at weight n + 1, so each pair overlaps by the smaller weight.
        records = [{**HDFC_DEBT, 'scheme_code': str(900 + n), 'scheme_name': f'Sample Debt Fund {n}'} for n in range(12)]
        for n, record in enumerate(records):
            store_snapshot(record, record['scheme_code'], '2026-09', debt_month({'INE296A07SV1': float(n + 1)}, '2026-09'))
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
        ):
            result = analyze_holdings_overlap([record['scheme_code'] for record in records])
        self.assertEqual(len(result['pairs']), 66)
        by_pair = {(pair['left']['scheme_code'], pair['right']['scheme_code']): pair['overlap_pct'] for pair in result['pairs']}
        self.assertEqual(by_pair[('900', '911')], 1.0)
        self.assertEqual(by_pair[('905', '908')], 6.0)
        self.assertEqual(result['highest_overlap']['percentage'], 11.0)
        self.assertEqual(result['pair_reasons'], [])
