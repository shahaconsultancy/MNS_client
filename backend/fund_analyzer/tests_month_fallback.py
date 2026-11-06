"""Month selection: 6 months of official files first, then a 4-month backup for the specific missing month.

No network, no database. "Bajaj" is B, a fund whose AMC has no readable official source.
"""
from unittest.mock import patch

from django.test import override_settings

from . import charlie_alpha as ca
from .holdings import _backup_window, _holdings_from_charlie, analyze_holdings_overlap
from .portfolio_store import read_snapshot
from .tests_charlie import ALL_SOURCES, A, B, C, E1, E2, E3, BackupTestCase, FakeProvider, complete, fund, official, payload

# The official window on 6 November 2026: October back to May. The backup window is October back to July.
SIX_MONTHS = {'2026-10', '2026-09', '2026-08', '2026-07', '2026-06', '2026-05'}
MONTH_END = {'2026-10': '2026-10-31', '2026-09': '2026-09-30', '2026-08': '2026-08-31', '2026-07': '2026-07-31', '2026-06': '2026-06-30', '2026-05': '2026-05-31'}
D = fund('1004', 'Delta Large Cap Fund')   # an official AMC fund, like any other


def backup(record, months, weights=None):
    """The provider's month-end portfolios for this fund."""
    return {MONTH_END[month]: payload(record, MONTH_END[month], complete(weights or {E1: 30.0, E3: 20.0})) for month in months}


class MonthFallbackTests(BackupTestCase):
    def analyze(self, records, provider, **_ignored):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=SIX_MONTHS),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
            patch('fund_analyzer.holdings._download_cached', side_effect=AssertionError('no official download expected')),
            patch.object(ca, '_get_json', provider),
        ):
            return analyze_holdings_overlap([record['scheme_code'] for record in records])

    def holdings_requests(self, provider):
        return [(path.rsplit('/', 1)[-1], as_of) for path, as_of in provider.calls if path.startswith('/mf/holdings/')]

    def assert_one_month_per_pair(self, result):
        for pair in result['pairs']:
            self.assertEqual(pair['left_portfolio_date'][:7], pair['portfolio_month'])
            self.assertEqual(pair['right_portfolio_date'][:7], pair['portfolio_month'])

    def test_1_all_funds_have_october_official_holdings(self):
        for record in (A, C, D):
            official(record, {'2026-10': {E1: 40.0}, '2026-09': {E1: 10.0}})
        provider = FakeProvider()
        result = self.analyze([A, C, D], provider)
        self.assertEqual((result['common_month'], result['compared_pairs']), ('2026-10', 3))
        self.assertEqual(provider.calls, [])
        self.assertEqual({fund['holdings_source'] for fund in result['funds']}, {'official_amc'})

    def test_2_october_missing_so_all_official_september_is_used(self):
        official(A, {'2026-10': {E1: 50.0}, '2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 35.0}})
        provider = FakeProvider()
        result = self.analyze([A, C], provider)
        self.assertEqual((result['common_month'], result['pairs'][0]['overlap_pct']), ('2026-09', 35.0))
        self.assertEqual(provider.calls, [])

    def test_3_official_october_for_two_funds_and_backup_october_for_bajaj(self):
        official(A, {'2026-10': {E1: 40.0}, '2026-09': {E1: 10.0}})
        official(C, {'2026-10': {E1: 35.0}, '2026-09': {E1: 10.0}})
        provider = FakeProvider({'1002': backup(B, ['2026-10', '2026-09'])})
        result = self.analyze([A, B, C], provider)
        self.assertEqual(result['common_month'], '2026-10')
        self.assertEqual({p['portfolio_month'] for p in result['pairs']}, {'2026-10'})
        self.assertEqual(self.fund_of(result, '1002')['holdings_source'], 'charlie_alpha')
        self.assertEqual((self.fund_of(result, '1001')['holdings_source'], self.fund_of(result, '1003')['holdings_source']), ('official_amc', 'official_amc'))
        self.assertEqual(self.holdings_requests(provider), [('1002', '2026-10-31')])

    def test_4_bajaj_backup_has_september_only_so_everyone_uses_september(self):
        official(A, {'2026-10': {E1: 50.0}, '2026-09': {E1: 40.0}})
        official(C, {'2026-10': {E1: 45.0}, '2026-09': {E1: 35.0}})
        provider = FakeProvider({'1002': backup(B, ['2026-09'])})
        result = self.analyze([A, B, C], provider)
        by_pair = {(p['left']['scheme_code'], p['right']['scheme_code']): (p['portfolio_month'], p['overlap_pct'], p['holdings_source']) for p in result['pairs']}
        # SBI and HDFC use their September files, not October, because Bajaj can only be compared in September.
        self.assertEqual(by_pair, {('1001', '1002'): ('2026-09', 30.0, 'mixed'), ('1001', '1003'): ('2026-09', 35.0, 'official_amc'), ('1002', '1003'): ('2026-09', 30.0, 'mixed')})
        self.assertEqual((result['common_month'], result['pairs_different_months'], result['partial_coverage']), ('2026-09', False, False))
        self.assertEqual(self.holdings_requests(provider), [('1002', '2026-09-30')])
        self.assert_one_month_per_pair(result)

    def test_5_official_august_for_every_fund_needs_no_backup(self):
        official(A, {'2026-10': {E1: 50.0}, '2026-08': {E1: 40.0}})
        official(C, {'2026-10': {E1: 45.0}, '2026-08': {E1: 30.0}})
        official(D, {'2026-08': {E1: 20.0}})
        provider = FakeProvider({'1004': backup(D, ['2026-10'])})
        result = self.analyze([A, C, D], provider)
        self.assertEqual(result['common_month'], '2026-08')
        self.assertEqual(provider.calls, [])
        self.assertEqual({fund['holdings_source'] for fund in result['funds']}, {'official_amc'})

    def test_6_official_may_and_backup_september_uses_september_for_the_missing_month(self):
        official(A, {'2026-10': {E1: 50.0}, '2026-09': {E1: 40.0}})
        official(C, {'2026-10': {E1: 45.0}, '2026-09': {E1: 35.0}})
        official(D, {'2026-05': {E1: 5.0}})
        provider = FakeProvider({'1004': backup(D, ['2026-09'], {E1: 25.0, E2: 30.0})})
        result = self.analyze([A, C, D], provider)
        self.assertEqual(result['common_month'], '2026-09')
        self.assertEqual(self.fund_of(result, '1004')['holdings_source'], 'charlie_alpha')
        self.assertEqual({(p['left']['scheme_code'], p['right']['scheme_code']): p['overlap_pct'] for p in result['pairs']}, {('1001', '1003'): 35.0, ('1001', '1004'): 25.0, ('1003', '1004'): 25.0})
        self.assertNotIn(('1004', '2026-10-31'), self.holdings_requests(provider))
        self.assert_one_month_per_pair(result)

    def test_6b_a_complete_official_month_beats_a_newer_backup_month(self):
        # Everyone also has official May. A complete official comparison is preferred over third-party data.
        official(A, {'2026-09': {E1: 40.0}, '2026-05': {E1: 9.0}})
        official(C, {'2026-09': {E1: 35.0}, '2026-05': {E1: 8.0}})
        official(D, {'2026-05': {E1: 5.0}})
        provider = FakeProvider({'1004': backup(D, ['2026-09'])})
        result = self.analyze([A, C, D], provider)
        self.assertEqual(result['common_month'], '2026-05')
        self.assertEqual(provider.calls, [])

    def test_7_backup_has_nothing_from_october_to_july(self):
        official(A, {'2026-09': {E1: 40.0}})
        # June and May are inside the official window but outside the 4-month backup window.
        provider = FakeProvider({'1002': backup(B, ['2026-06', '2026-05'])})
        result = self.analyze([A, B], provider)
        self.assertEqual((result['pairs'], result['highest_overlap']['percentage'], result['highest_overlap']['message']), ([], None, ALL_SOURCES))
        self.assertEqual(self.holdings_requests(provider), [])
        self.assertFalse(self.fund_of(result, '1002')['available'])

    def test_8_one_pair_available_and_the_others_not(self):
        official(A, {'2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 35.0}})
        result = self.analyze([A, B, C], FakeProvider({}))
        self.assertEqual([(p['left']['scheme_code'], p['right']['scheme_code']) for p in result['pairs']], [('1001', '1003')])
        self.assertEqual((result['compared_pairs'], result['total_pairs'], result['partial_coverage']), (1, 3, True))
        self.assertTrue(result['highest_overlap']['partial_coverage'])

    def test_9_both_providers_unavailable(self):
        official(A, {'2026-09': {E1: 40.0}})
        result = self.analyze([A, B], FakeProvider(error=ca.CharlieError(ca.TIMEOUT)))
        self.assertEqual((result['pairs'], result['highest_overlap']['percentage'], result['highest_overlap']['message']), ([], None, ALL_SOURCES))

    def test_10_backup_switched_off(self):
        official(A, {'2026-10': {E1: 50.0}, '2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 35.0}})
        official(D, {'2026-05': {E1: 5.0}})
        provider = FakeProvider({'1004': backup(D, ['2026-09']), '1002': backup(B, ['2026-09'])})
        with override_settings(FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=False):
            result = self.analyze([A, B, C, D], provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual([(p['left']['scheme_code'], p['right']['scheme_code']) for p in result['pairs']], [('1001', '1003')])
        self.assertEqual(result['lookback']['backup_months'], [])
        self.assertFalse(any(fund.get('holdings_source') == 'charlie_alpha' for fund in result['funds']))

    def test_11_no_pair_ever_mixes_two_months(self):
        official(A, {'2026-10': {E1: 50.0}})
        official(C, {'2026-09': {E1: 20.0}})
        provider = FakeProvider({'1002': backup(B, ['2026-10', '2026-09'])})
        result = self.analyze([A, B, C], provider)
        self.assert_one_month_per_pair(result)
        self.assertNotIn(('1001', '1003'), {(p['left']['scheme_code'], p['right']['scheme_code']) for p in result['pairs']})
        self.assertTrue(result['pairs_different_months'])

    def test_12_a_saved_backup_month_is_reused_and_official_data_keeps_priority(self):
        official(A, {'2026-09': {E1: 40.0}})
        first = FakeProvider({'1002': backup(B, ['2026-09'])})
        self.analyze([A, B], first)
        self.assertEqual(read_snapshot(B, '2026-09')['source_type'], 'charlie_alpha')
        again = FakeProvider({'1002': backup(B, ['2026-09'])})
        result = self.analyze([A, B], again)
        self.assertEqual(self.holdings_requests(again), [])
        self.assertEqual(result['pairs'][0]['overlap_pct'], 30.0)
        # An official file for the same month always wins over the saved copy.
        official(B, {'2026-09': {E1: 12.0}})
        third = FakeProvider({'1002': backup(B, ['2026-09'])})
        result = self.analyze([A, B], third)
        self.assertEqual((result['pairs'][0]['overlap_pct'], self.fund_of(result, '1002')['holdings_source']), (12.0, 'official_amc'))
        self.assertEqual(third.calls, [])

    def test_the_backup_window_is_its_own_setting(self):
        with patch('fund_analyzer.holdings._recent_portfolio_months', return_value=SIX_MONTHS):
            self.assertEqual(_backup_window(), {'2026-10', '2026-09', '2026-08', '2026-07'})
            with override_settings(FUND_ANALYZER_CHARLIE_ALPHA_LOOKBACK_MONTHS=2):
                self.assertEqual(_backup_window(), {'2026-10', '2026-09'})
            self.assertEqual(_backup_window({'2026-05'}), set())

    def test_a_backup_month_outside_the_window_is_refused_even_if_asked_directly(self):
        with patch('fund_analyzer.holdings._recent_portfolio_months', return_value=SIX_MONTHS):
            with self.assertRaises(ca.CharlieError):
                _holdings_from_charlie('1002', '2026-06', {'as_of': '2026-06-30'}, {}, B)

    def test_the_response_states_both_windows(self):
        official(A, {'2026-09': {E1: 40.0}})
        official(C, {'2026-09': {E1: 35.0}})
        lookback = self.analyze([A, C], FakeProvider())['lookback']
        self.assertEqual(lookback['official_months'], sorted(SIX_MONTHS, reverse=True))
        self.assertEqual(lookback['backup_months'], ['2026-10', '2026-09', '2026-08', '2026-07'])
