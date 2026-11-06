"""Regression tests for defects found in the pre-production QA audit. No network, no database."""
from datetime import date
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from . import providers
from .providers import ProviderError
from .services import search_funds

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'qa-regressions'}}
RECORDS = [
    {'scheme_code': '1', 'scheme_name': 'BANDHAN Dynamic Term   Fund', 'amc': 'Bandhan Mutual Fund', 'category': 'Income/Debt Oriented Schemes - Dynamic Term Fund', 'plan': 'Direct Plan', 'option': 'Growth', 'isin': 'INF194K01AA1', 'date': '05-Nov-2026'},
    {'scheme_code': '2', 'scheme_name': 'Kotak Global Innovation Overseas Equity Active FOF', 'amc': 'Kotak Mahindra Mutual Fund', 'category': 'Overseas Fund of Funds', 'plan': 'Direct Plan', 'option': 'Growth', 'isin': 'INF174K01AA2', 'date': '05-Nov-2026'},
    {'scheme_code': '3', 'scheme_name': 'HDFC Large Cap Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Large Cap Fund', 'plan': 'Direct Plan', 'option': 'Growth Option', 'isin': 'INF179K01YV8', 'date': '05-Nov-2026'},
]


class SearchWordsTests(SimpleTestCase):
    """QA-3: search matched only the exact phrase, so word order and AMFI's double spaces hid real funds."""

    def search(self, query):
        with patch('fund_analyzer.services.fetch_amfi_latest', return_value=RECORDS):
            return [row['scheme_code'] for row in search_funds(query)['results']]

    def test_words_in_any_order_find_the_fund(self):
        self.assertEqual(self.search('kotak innovation fof'), ['2'])
        self.assertEqual(self.search('innovation kotak'), ['2'])

    def test_repeated_spaces_in_amfi_names_do_not_hide_a_fund(self):
        self.assertEqual(self.search('Bandhan Dynamic Term Fund'), ['1'])

    def test_exact_phrase_code_and_isin_still_work(self):
        self.assertEqual(self.search('HDFC Large Cap'), ['3'])
        self.assertEqual(self.search('3'), ['3'])
        self.assertEqual(self.search('INF179K01YV8'), ['3'])

    def test_the_typed_phrase_comes_before_names_that_only_contain_the_words(self):
        records = [
            {**RECORDS[2], 'scheme_code': '4', 'scheme_name': 'HDFC Large and Mid Cap Fund'},
            {**RECORDS[2], 'scheme_code': '5', 'scheme_name': 'Navi HDFC Large Cap Index'},
            RECORDS[2],
        ]
        with patch('fund_analyzer.services.fetch_amfi_latest', return_value=records):
            self.assertEqual([row['scheme_code'] for row in search_funds('HDFC Large Cap')['results']], ['3', '5', '4'])

    def test_every_word_must_match(self):
        self.assertEqual(self.search('kotak large'), [])


@override_settings(CACHES=LOCMEM)
class AmfiHistoryRetryTests(SimpleTestCase):
    """QA-1: a failing AMFI history download was retried on every request, adding 8 s to each Compare."""

    def setUp(self):
        cache.clear()

    def test_a_failure_is_remembered_so_the_next_request_skips_amfi(self):
        start, end = date(2021, 11, 1), date(2026, 11, 5)
        with patch('fund_analyzer.providers.fetch_url', side_effect=ProviderError('Read timed out')) as fetch:
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('119018', start, end)
            sent = fetch.call_count
            with self.assertRaisesRegex(ProviderError, 'failed recently'):
                providers.fetch_amfi_history('119018', start, end)
            self.assertEqual(fetch.call_count, sent)

    def test_other_schemes_are_still_tried(self):
        start, end = date(2021, 11, 1), date(2026, 11, 5)
        with patch('fund_analyzer.providers.fetch_url', side_effect=ProviderError('Read timed out')) as fetch:
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('119018', start, end)
            sent = fetch.call_count
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('128629', start, end)
            self.assertGreater(fetch.call_count, sent)

    def test_amfi_is_tried_again_after_the_wait(self):
        start, end = date(2021, 11, 1), date(2026, 11, 5)
        with patch('fund_analyzer.providers.fetch_url', side_effect=ProviderError('Read timed out')):
            with self.assertRaises(ProviderError):
                providers.fetch_amfi_history('119018', start, end)
        cache.delete('fund-analyzer:amfi:history-failed:119018')
        response = MagicMock()
        response.json.return_value = {'data': {'nav_groups': []}}
        with patch('fund_analyzer.providers.fetch_url', return_value=response) as fetch, patch('fund_analyzer.providers._history_rows', return_value=[{'date': '2026-11-05', 'nav': 10.0}]):
            self.assertEqual(providers.fetch_amfi_history('119018', start, end), [{'date': '2026-11-05', 'nav': 10.0}])
            self.assertTrue(fetch.called)


@override_settings(CACHES=LOCMEM, DEBUG=False)
class ErrorResponseTests(SimpleTestCase):
    """QA-2 and QA-4: provider text reached the client, and an unknown scheme answered 503."""

    INTERNAL = "HTTPSConnectionPool(host='www.amfiindia.com'): Read timed out at D:\\PROJECT\\backend"

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def assert_clean(self, response):
        self.assertEqual(response.status_code, 503)
        text = response.content.decode()
        for leak in ('HTTPSConnectionPool', 'amfiindia', 'D:\\\\', 'PROJECT'):
            self.assertNotIn(leak, text)
        self.assertIn('temporarily unavailable', response.json()['detail'])

    def test_search_failure_shows_a_plain_message(self):
        with patch('fund_analyzer.services.fetch_amfi_latest', side_effect=ProviderError(self.INTERNAL)):
            self.assert_clean(self.client.get('/api/search', {'q': 'hdfc'}))

    def test_compare_failure_shows_a_plain_message(self):
        with patch('fund_analyzer.views.compare_funds', side_effect=ProviderError(self.INTERNAL)):
            self.assert_clean(self.client.post('/api/compare', {'scheme_codes': ['1']}, format='json'))

    def test_fundamentals_failure_shows_a_plain_message(self):
        with patch('fund_analyzer.views.fetch_fundamentals', side_effect=ProviderError(self.INTERNAL)):
            self.assert_clean(self.client.post('/api/fundamentals', {'scheme_codes': ['1']}, format='json'))

    def test_the_provider_detail_still_reaches_the_log(self):
        with patch('fund_analyzer.services.fetch_amfi_latest', side_effect=ProviderError(self.INTERNAL)), self.assertLogs('fund_analyzer.views', 'WARNING') as logs:
            self.client.get('/api/search', {'q': 'hdfc'})
        self.assertIn('Read timed out', logs.output[0])

    def test_unknown_scheme_is_not_found(self):
        with patch('fund_analyzer.views.fetch_amfi_latest', return_value=RECORDS):
            response = self.client.get('/api/funds/999999999')
        self.assertEqual((response.status_code, response.json()['detail']), (404, 'Scheme not found'))

    def test_detail_still_works_when_the_scheme_list_is_down(self):
        with patch('fund_analyzer.views.fetch_amfi_latest', side_effect=ProviderError('down')), patch('fund_analyzer.views.fetch_fund_data', side_effect=ProviderError(self.INTERNAL)):
            self.assert_clean(self.client.get('/api/funds/3'))
