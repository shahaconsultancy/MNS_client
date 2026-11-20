"""The NAV history store: cache first, only the missing days fetched, one fetch per scheme, strict backup checks.

A comparison asks each selected fund's history from the shared store (Redis on a server that has REDIS_URL).
A history that reaches AMFI's latest NAV date needs no request at all; an older one is extended with only the
days since. If a refresh fails, the stored history is served and marked stale, never silently.
"""
from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from . import providers, services
from .providers import ProviderError, SourceFailure
from .services import NAV_HISTORY_UNAVAILABLE, _metrics_or_reason, _nav_key, _valid_rows, compare_funds, fetch_fund_data

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'nav-store-tests'}}
TODAY = date.today()


def amfi_date(day: date) -> str:
    return day.strftime('%d-%b-%Y')


def rows(days: int = 900, last: date | None = None, start_nav: float = 100.0, step: float = 0.0004, code: str = '1001') -> list[dict]:
    last = last or TODAY - timedelta(days=1)
    first = last - timedelta(days=days)
    return [{'scheme_code': code, 'date': (first + timedelta(days=i)).isoformat(), 'nav': round(start_nav * (1 + step) ** i, 4)} for i in range(days + 1)]


def record(code: str = '1001', name: str = 'Alpha Flexi Cap Fund - Regular Plan - Growth', newest: date | None = None, **extra) -> dict:
    newest = newest or TODAY - timedelta(days=1)
    return {'scheme_code': code, 'scheme_name': name, 'amc': 'Alpha Mutual Fund', 'category': 'Equity Scheme - Flexi Cap Fund', 'plan': 'Regular Plan', 'option': 'Growth', 'nav': 120.0, 'date': amfi_date(newest), 'isin': 'INF000A01011', **extra}


def mfapi_payload(code: str = '1001', name: str = 'Alpha Flexi Cap Fund - Regular Plan - Growth', isin_growth: str = 'INF000A01011', history: list[dict] | None = None) -> dict:
    history = history or rows(code=code)
    data = [{'date': date.fromisoformat(row['date']).strftime('%d-%m-%Y'), 'nav': str(row['nav'])} for row in reversed(history)]
    return {'meta': {'scheme_code': int(code), 'scheme_name': name, 'fund_house': 'Alpha Mutual Fund', 'scheme_category': 'Equity Scheme - Flexi Cap Fund', 'isin_growth': isin_growth, 'isin_div_reinvestment': None}, 'data': data}


@override_settings(CACHES=LOCMEM)
class NavStoreTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_a_history_reaching_amfis_latest_date_is_used_without_any_request(self):
        with patch('fund_analyzer.services.fetch_amfi_history', return_value=rows()) as amfi:
            first = fetch_fund_data('1001', include_metadata=False, latest_records=[record()])
            second = fetch_fund_data('1001', include_metadata=False, latest_records=[record()])
        self.assertEqual(amfi.call_count, 1)
        self.assertEqual(first['metrics'], second['metrics'])
        self.assertFalse(second['history_stale'])
        self.assertEqual(second['history_as_of'], (TODAY - timedelta(days=1)).isoformat())

    def test_a_new_day_fetches_only_the_missing_days_and_gives_the_same_figures_as_a_full_fetch(self):
        full = rows()
        older, newest_rows = full[:-3], full[-3:]
        cache.set(_nav_key('1001'), {'source': 'AMFI', 'rows': older, 'meta': record(), 'last_date': older[-1]['date'], 'checked_at': time.time() - 86400})
        with patch('fund_analyzer.services.fetch_amfi_history', return_value=newest_rows) as amfi:
            result = fetch_fund_data('1001', include_metadata=False, latest_records=[record()])
        start = amfi.call_args.args[1]
        self.assertEqual(start, date.fromisoformat(older[-1]['date']) + timedelta(days=1))  # only the days since
        expected = _metrics_or_reason(services._priced_history(full, '1001'), 0.065, '1001')[0]
        self.assertEqual(result['metrics'], expected)  # formulas and figures unchanged
        self.assertEqual(cache.get(_nav_key('1001'))['last_date'], full[-1]['date'])

    def test_no_new_rows_yet_keeps_the_stored_history_and_waits_before_asking_again(self):
        stored = rows(last=TODAY - timedelta(days=2))
        cache.set(_nav_key('1001'), {'source': 'AMFI', 'rows': stored, 'meta': record(), 'last_date': stored[-1]['date'], 'checked_at': 0})
        with patch('fund_analyzer.services.fetch_amfi_history', side_effect=ProviderError('AMFI returned no history for scheme 1001')) as amfi, \
                patch('fund_analyzer.services.fetch_mfapi_data') as backup:
            first = fetch_fund_data('1001', include_metadata=False, latest_records=[record()])
            fetch_fund_data('1001', include_metadata=False, latest_records=[record()])
        self.assertEqual(amfi.call_count, 1)
        backup.assert_not_called()
        self.assertEqual((first['source'], first['history_stale']), ('AMFI', False))

    def test_a_failed_refresh_serves_the_stored_history_marked_stale(self):
        stored = rows(last=TODAY - timedelta(days=5))
        cache.set(_nav_key('1001'), {'source': 'AMFI', 'rows': stored, 'meta': record(), 'last_date': stored[-1]['date'], 'checked_at': 0})
        with patch('fund_analyzer.services.fetch_amfi_history', side_effect=ProviderError('AMFI NAV history is busy right now')), \
                patch('fund_analyzer.services.fetch_mfapi_data', side_effect=ProviderError('MFAPI down')):
            result = fetch_fund_data('1001', include_metadata=False, latest_records=[record()])
        self.assertTrue(result['history_stale'])
        self.assertEqual(result['history_as_of'], stored[-1]['date'])
        self.assertIn(stored[-1]['date'], result['history_note'])
        self.assertIsNotNone(result['metrics'])
        self.assertEqual(cache.get(_nav_key('1001'))['rows'], stored)  # never replaced by the failure

    def test_without_a_stored_history_both_sources_failing_raises(self):
        with patch('fund_analyzer.services.fetch_amfi_history', side_effect=ProviderError('busy')), \
                patch('fund_analyzer.services.fetch_mfapi_data', side_effect=ProviderError('down')):
            with self.assertRaisesRegex(ProviderError, 'Unable to load scheme 1001'):
                fetch_fund_data('1001', include_metadata=False, latest_records=[record()])

    def test_simultaneous_requests_for_one_scheme_fetch_it_once(self):
        calls = []

        def slow(code, start, end):
            calls.append(code)
            time.sleep(0.3)
            return rows()

        results = []
        with patch('fund_analyzer.services.fetch_amfi_history', side_effect=slow):
            threads = [threading.Thread(target=lambda: results.append(fetch_fund_data('1001', include_metadata=False, latest_records=[record()]))) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(result['metrics'] == results[0]['metrics'] for result in results))


@override_settings(CACHES=LOCMEM)
class BackupIdentityTests(SimpleTestCase):
    """MFAPI's copy is used only when it is exactly the requested scheme, plan and option."""

    def setUp(self):
        cache.clear()
        starter = patch('fund_analyzer.services.fetch_amfi_history', side_effect=ProviderError('AMFI NAV history is busy right now'))
        starter.start()
        self.addCleanup(starter.stop)

    def load(self, payload, amfi=None):
        with patch('fund_analyzer.services.fetch_mfapi_data', return_value=payload):
            return fetch_fund_data('1001', include_metadata=False, latest_records=[amfi or record()])

    def test_the_exact_scheme_is_accepted(self):
        result = self.load(mfapi_payload())
        self.assertEqual(result['source'], 'MFAPI (backup)')
        self.assertIsNotNone(result['metrics'])

    def test_another_scheme_code_is_rejected(self):
        with self.assertRaises(ProviderError):
            self.load(mfapi_payload(code='1002'))

    def test_another_isin_is_rejected(self):
        with self.assertRaises(ProviderError):
            self.load(mfapi_payload(isin_growth='INF999Z01999'))

    def test_another_plan_or_option_is_rejected(self):
        with self.assertRaises(ProviderError):
            self.load(mfapi_payload(name='Alpha Flexi Cap Fund - Direct Plan - Growth'))
        with self.assertRaises(ProviderError):
            self.load(mfapi_payload(name='Alpha Flexi Cap Fund - Regular Plan - IDCW'))
        self.assertIsNone(cache.get(_nav_key('1001')))  # a rejected copy is never stored

    def test_uti_dividend_yield_regular_growth_is_not_given_the_idcw_history(self):
        """103026 is Regular Growth in AMFI; MFAPI holds an IDCW history under that code."""
        amfi = record('103026', 'UTI Dividend Yield Fund - Regular Plan - Growth Option', isin='INF789F01JN2')
        payload = mfapi_payload('103026', 'UTI Dividend Yield Fund - Regular Plan - IDCW', isin_growth='INF789F01JO0')
        with patch('fund_analyzer.services.fetch_amfi_latest', return_value=[amfi, record()]), \
                patch('fund_analyzer.services.fetch_mfapi_data', side_effect=lambda code: payload if code == '103026' else mfapi_payload()), \
                patch('fund_analyzer.services.fetch_amfi_ter_snapshot', return_value={}):
            result = compare_funds(['1001', '103026'])
        funds = {fund['scheme_code']: fund for fund in result['funds']}
        self.assertEqual(funds['103026']['metrics_unavailable'], NAV_HISTORY_UNAVAILABLE)
        self.assertIsNotNone(funds['1001']['metrics'])


class DateValidationTests(SimpleTestCase):
    def test_undated_future_and_repeated_rows_are_left_out_and_prices_are_unchanged(self):
        good = rows(days=10)
        bad = [{'date': 'not a date', 'nav': 1.0}, {'date': (TODAY + timedelta(days=30)).isoformat(), 'nav': 1.0}, dict(good[3]), {'date': good[4]['date'], 'nav': 'x'}]
        self.assertEqual(_valid_rows([*good, *bad], '1001'), good)


@override_settings(CACHES=LOCMEM)
class AmfiCooldownTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_retry_after_sets_how_long_amfi_is_left_alone(self):
        self.assertEqual(providers._busy_seconds({'retry_after': '300'}), 300)
        self.assertEqual(providers._busy_seconds({'retry_after': '5'}), providers.AMFI_HISTORY_BUSY_SECONDS)
        self.assertEqual(providers._busy_seconds({'retry_after': '86400'}), providers.AMFI_HISTORY_BUSY_MAX_SECONDS)
        self.assertEqual(providers._busy_seconds({}), providers.AMFI_HISTORY_BUSY_SECONDS)
        refusal = SourceFailure('429 Client Error: Too Many Requests', evidence={'http_status': 429, 'retry_after': '300'})
        with patch('fund_analyzer.providers.fetch_url', side_effect=refusal), patch.object(cache, 'set', wraps=cache.set) as setter:
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('1001', TODAY - timedelta(days=30), TODAY)
        busy = [call for call in setter.call_args_list if call.args[0] == providers.AMFI_HISTORY_BUSY_KEY]
        self.assertEqual(busy[0].kwargs['timeout'], 300)

    def test_no_more_than_three_history_requests_run_at_once(self):
        running, peak, guard = [0], [0], threading.Lock()

        class Reply:
            def json(self):
                return {'data': {'nav_groups': [{'historical_records': [{'date': TODAY.isoformat(), 'nav': '10'}]}]}}

        def fetch(*args, **kwargs):
            with guard:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with guard:
                running[0] -= 1
            return Reply()

        with patch('fund_analyzer.providers.fetch_url', side_effect=fetch):
            threads = [threading.Thread(target=providers.fetch_amfi_history, args=(str(code), TODAY - timedelta(days=30), TODAY)) for code in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertLessEqual(peak[0], 3)


@override_settings(CACHES=LOCMEM)
class CompareTimingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_compare_reports_where_the_time_went_and_the_cache_hits(self):
        records = [record('1001'), record('1002', 'Beta Flexi Cap Fund - Regular Plan - Growth')]
        with patch('fund_analyzer.services.fetch_amfi_latest', return_value=records), \
                patch('fund_analyzer.services.fetch_amfi_history', side_effect=lambda code, start, end: rows(code=code)), \
                patch('fund_analyzer.services.fetch_amfi_ter_snapshot', return_value={}):
            cold = APIClient().post('/api/compare', {'scheme_codes': ['1001', '1002']}, format='json')
            warm = APIClient().post('/api/compare', {'scheme_codes': ['1001', '1002']}, format='json')
        self.assertEqual((cold.status_code, warm.status_code), (200, 200))
        self.assertIn('cache_miss;desc="2"', cold['Server-Timing'])
        self.assertIn('cache_hit;desc="2"', warm['Server-Timing'])
        self.assertNotIn('amfi;dur', warm['Server-Timing'])
        self.assertNotIn('timings', warm.json())
        self.assertEqual(cold.json()['funds'][0]['metrics'], warm.json()['funds'][0]['metrics'])
        self.assertFalse(warm.json()['funds'][0]['history_stale'])


@override_settings(CACHES=LOCMEM)
class AmfiSlowTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_three_timeouts_send_the_next_funds_straight_to_the_backup(self):
        start, end = TODAY - timedelta(days=30), TODAY  # one request a scheme
        with patch('fund_analyzer.providers.fetch_url', side_effect=ProviderError('Read timed out. (read timeout=8)')) as fetch:
            for code in ('1', '2', '3'):
                with self.assertRaises(ProviderError):
                    providers.fetch_amfi_history(code, start, end)
            sent = fetch.call_count
            with self.assertRaisesRegex(ProviderError, 'busy'):
                providers.fetch_amfi_history('4', start, end)
            self.assertEqual(fetch.call_count, sent)

    def test_one_or_two_timeouts_do_not(self):
        start, end = TODAY - timedelta(days=30), TODAY
        with patch('fund_analyzer.providers.fetch_url', side_effect=ProviderError('Read timed out')) as fetch:
            for code in ('1', '2'):
                with self.assertRaises(ProviderError):
                    providers.fetch_amfi_history(code, start, end)
            sent = fetch.call_count
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('3', start, end)
            self.assertGreater(fetch.call_count, sent)
