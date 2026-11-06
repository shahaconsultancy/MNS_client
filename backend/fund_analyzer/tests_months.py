"""Tests for choosing reporting months across many selected funds. These use no database."""
from unittest.mock import patch

from django.test import SimpleTestCase

from .holdings import analyze_holdings_overlap
from .portfolio_store import store_snapshot
from .tests_pipeline import PortfolioCacheMixin, holdings_result

RECENT = {'2026-10', '2026-09', '2026-08'}
MONTH_END = {'2026-10': '2026-10-31', '2026-09': '2026-09-30', '2026-08': '2026-08-31'}
ISIN = 'INE040A01034'


def fund(code, name, amc='HDFC Mutual Fund'):
    return {'scheme_code': code, 'scheme_name': name, 'amc': amc, 'plan': 'Direct Plan', 'category': 'Equity Scheme - Large Cap Fund'}


A, B, C, D = fund('1', 'Alpha Equity Fund'), fund('2', 'Beta Equity Fund'), fund('3', 'Gamma Equity Fund'), fund('4', 'Delta Equity Fund')


def store(record, weights_by_month):
    """Save verified months for a fund. The same stock is held, at a different weight each month."""
    for month, weight in weights_by_month.items():
        store_snapshot(record, record['scheme_code'], month, holdings_result({ISIN: weight}, MONTH_END[month]))


class MonthSelectionTests(PortfolioCacheMixin, SimpleTestCase):
    def _analyze(self, records, links=None, download=None):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **(links or {'return_value': []})),
            patch('fund_analyzer.holdings._download_cached', **(download or {'side_effect': AssertionError('no download expected')})),
        ):
            return analyze_holdings_overlap([record['scheme_code'] for record in records])

    def _by_pair(self, result):
        return {(pair['left']['scheme_code'], pair['right']['scheme_code']): pair for pair in result['pairs']}

    def test_a_month_common_to_every_fund_is_used_for_every_pair(self):
        store(A, {'2026-10': 50.0, '2026-09': 40.0, '2026-08': 30.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0, '2026-08': 10.0})
        store(C, {'2026-09': 10.0, '2026-08': 5.0})
        result = self._analyze([A, B, C])
        pairs = self._by_pair(result)
        # September is the latest month all three reported. Each figure comes from September's weights.
        self.assertEqual({key: pair['overlap_pct'] for key, pair in pairs.items()}, {('1', '2'): 20.0, ('1', '3'): 10.0, ('2', '3'): 10.0})
        self.assertEqual({pair['portfolio_month'] for pair in pairs.values()}, {'2026-09'})
        self.assertEqual({pair['month_basis'] for pair in pairs.values()}, {'COMMON_TO_ALL'})
        self.assertEqual((result['common_month'], result['portfolio_month'], result['pairs_different_months']), ('2026-09', '2026-09', False))

    def test_the_common_month_wins_even_when_one_pair_has_a_newer_month(self):
        # A and B both reported October (45 is their October overlap), but C did not.
        store(A, {'2026-10': 50.0, '2026-09': 40.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        store(C, {'2026-09': 10.0})
        pair = self._by_pair(self._analyze([A, B, C]))[('1', '2')]
        self.assertEqual((pair['overlap_pct'], pair['portfolio_month']), (20.0, '2026-09'))

    def test_without_a_common_month_each_pair_uses_its_own_latest_common_month(self):
        store(A, {'2026-10': 50.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        store(C, {'2026-09': 10.0})
        result = self._analyze([A, B, C])
        pairs = self._by_pair(result)
        self.assertEqual((pairs[('1', '2')]['overlap_pct'], pairs[('1', '2')]['portfolio_month']), (45.0, '2026-10'))
        self.assertEqual((pairs[('2', '3')]['overlap_pct'], pairs[('2', '3')]['portfolio_month']), (10.0, '2026-09'))
        self.assertEqual({pair['month_basis'] for pair in pairs.values()}, {'PAIRWISE'})
        self.assertEqual((result['common_month'], result['portfolio_month']), (None, None))
        self.assertTrue(result['pairs_different_months'])
        self.assertEqual(result['pair_months'], ['2026-10', '2026-09'])

    def test_no_pair_ever_mixes_two_months(self):
        store(A, {'2026-10': 50.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        store(C, {'2026-09': 10.0})
        for pair in self._analyze([A, B, C])['pairs']:
            self.assertEqual(pair['left_portfolio_date'][:7], pair['portfolio_month'])
            self.assertEqual(pair['right_portfolio_date'][:7], pair['portfolio_month'])

    def test_a_pair_with_no_common_month_is_unavailable_never_zero(self):
        store(A, {'2026-10': 50.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        store(C, {'2026-09': 10.0})
        result = self._analyze([A, B, C])
        self.assertNotIn(('1', '3'), self._by_pair(result))
        gap = next(item for item in result['pair_reasons'] if (item['left'], item['right']) == ('1', '3'))
        self.assertEqual((gap['reason_code'], gap['left_portfolio_month'], gap['right_portfolio_month']), ('NO_COMMON_REPORTING_MONTH', '2026-10', '2026-09'))

    def test_coverage_counts_only_pairs_that_were_compared(self):
        store(A, {'2026-10': 50.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        store(C, {'2026-09': 10.0})
        result = self._analyze([A, B, C])
        high = result['highest_overlap']
        self.assertEqual((result['compared_pairs'], result['total_pairs'], result['partial_coverage']), (2, 3, True))
        self.assertEqual((high['compared_pairs'], high['total_pairs'], high['partial_coverage']), (2, 3, True))
        # The highest is taken from the pairs that were evaluated, with its own month.
        self.assertEqual((high['percentage'], high['portfolio_month'], high['pairs_different_months']), (45.0, '2026-10', True))
        self.assertEqual((high['fund_a_code'], high['fund_b_code']), ('1', '2'))

    def test_full_coverage_is_not_reported_as_partial(self):
        store(A, {'2026-09': 40.0})
        store(B, {'2026-09': 20.0})
        store(C, {'2026-09': 10.0})
        result = self._analyze([A, B, C])
        self.assertEqual((result['compared_pairs'], result['total_pairs'], result['partial_coverage']), (3, 3, False))
        self.assertFalse(result['highest_overlap']['partial_coverage'])

    def test_a_fund_with_no_holdings_leaves_its_pairs_unavailable_and_the_rest_consistent(self):
        store(A, {'2026-10': 50.0, '2026-09': 40.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        nowhere = fund('4', 'Delta Equity Fund', amc='Nowhere Mutual Fund')
        result = self._analyze([A, B, nowhere])
        self.assertEqual([(key, pair['overlap_pct'], pair['portfolio_month']) for key, pair in self._by_pair(result).items()], [(('1', '2'), 45.0, '2026-10')])
        self.assertEqual((result['compared_pairs'], result['total_pairs'], result['partial_coverage']), (1, 3, True))
        self.assertEqual({(item['left'], item['right']): item['reason_code'] for item in result['pair_reasons']}, {('1', '4'): 'SOURCE_NOT_SUPPORTED', ('2', '4'): 'SOURCE_NOT_SUPPORTED'})

    def test_a_fund_whose_files_fail_does_not_force_the_others_into_mixed_months(self):
        # D has files for both months, but neither can be read. A and B alone share October, yet
        # A, B and C all share September, so September is used for every comparable pair.
        store(A, {'2026-10': 50.0, '2026-09': 40.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        store(C, {'2026-09': 10.0})

        def links(_source, name):
            return [{'url': f'https://x.test/{name}-{month}.xlsx', 'text': name, 'month': month} for month in ('2026-10', '2026-09')] if name == D['scheme_name'] else []

        result = self._analyze([A, B, C, D], links={'side_effect': links}, download={'return_value': b'this is not a workbook'})
        pairs = self._by_pair(result)
        self.assertEqual({key: (pair['overlap_pct'], pair['portfolio_month']) for key, pair in pairs.items()}, {('1', '2'): (20.0, '2026-09'), ('1', '3'): (10.0, '2026-09'), ('2', '3'): (10.0, '2026-09')})
        self.assertFalse(result['pairs_different_months'])
        self.assertEqual((result['common_month'], result['compared_pairs'], result['total_pairs'], result['partial_coverage']), ('2026-09', 3, 6, True))
        self.assertEqual(next(f for f in result['funds'] if f['scheme_code'] == '4')['available'], False)

    def test_a_fund_that_shares_no_month_does_not_block_the_others(self):
        store(A, {'2026-10': 50.0, '2026-09': 40.0})
        store(B, {'2026-10': 45.0, '2026-09': 20.0})
        store(C, {'2026-08': 10.0})
        result = self._analyze([A, B, C])
        self.assertEqual({key: pair['portfolio_month'] for key, pair in self._by_pair(result).items()}, {('1', '2'): '2026-10'})
        self.assertEqual(result['common_month'], '2026-10')
        self.assertTrue(next(f for f in result['funds'] if f['scheme_code'] == '3')['available'])

    def test_two_funds_behave_as_before(self):
        store(A, {'2026-10': 50.0, '2026-09': 40.0})
        store(B, {'2026-09': 20.0})
        result = self._analyze([A, B])
        self.assertEqual((result['pairs'][0]['overlap_pct'], result['pairs'][0]['portfolio_month'], result['portfolio_month']), (20.0, '2026-09', '2026-09'))
