"""Overlap across equity, debt and hybrid funds, compared security by security. No database."""
from unittest.mock import patch

from django.test import SimpleTestCase

from .holdings import _read_holdings, analyze_holdings_overlap, fetch_fund_holdings
from .portfolio_store import discovery_due, file_scheme_name, read_snapshot, store_snapshot
from .tests_debt import RECENT, sheet_bytes
from .tests_pipeline import PortfolioCacheMixin, holdings_result

EQUITY_A = {'scheme_code': '1', 'scheme_name': 'HDFC Large Cap Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Equity Scheme - Large Cap Fund'}
EQUITY_B = {'scheme_code': '2', 'scheme_name': 'SBI Large Cap Fund', 'amc': 'SBI Mutual Fund', 'category': 'Equity Scheme - Large Cap Fund'}
DEBT_A = {'scheme_code': '3', 'scheme_name': 'HDFC Corporate Bond Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Debt Scheme - Corporate Bond Fund'}
DEBT_B = {'scheme_code': '4', 'scheme_name': 'SBI Corporate Bond Fund', 'amc': 'SBI Mutual Fund', 'category': 'Debt Scheme - Corporate Bond Fund'}
HYBRID_A = {'scheme_code': '5', 'scheme_name': 'HDFC Aggressive Hybrid Fund', 'amc': 'HDFC Mutual Fund', 'category': 'Hybrid Schemes - Aggressive Hybrid Fund'}
HYBRID_B = {'scheme_code': '6', 'scheme_name': 'SBI Conservative Hybrid Fund', 'amc': 'SBI Mutual Fund', 'category': 'Hybrid Scheme - Conservative Hybrid Fund'}

BANK_STOCK, IT_STOCK = 'INE040A01034', 'INE009A01021'
BOND, OTHER_BOND = 'INE296A07SV1', 'INE296A07XX5'
TBILL = 'IN002026X198'


def stored(record, equity=None, debt=None, month='2026-09', **extra):
    """Save verified rows of both kinds, as a fresh read of an official file would."""
    equity, debt = equity or {}, debt or {}
    date = f'{month}-30'
    store_snapshot(record, record['scheme_code'], month, {
        **holdings_result({**equity, **debt}, date),
        'asset_class': 'hybrid' if 'Hybrid' in record['category'] else 'debt' if 'Debt' in record['category'] else 'equity',
        'security_classes': {**{isin: 'equity' for isin in equity}, **{isin: 'debt' for isin in debt}},
        'classes_read': ['equity', 'debt'],
        **extra,
    }, source_url=extra.get('source_url', ''))


def hybrid_file(name='HDFC Aggressive Hybrid Fund'):
    """Layout of a real hybrid file: an equity block, then debt, then the rest."""
    return sheet_bytes([
        [name],
        ['Monthly Portfolio Statement as on September 30, 2026'],
        ['ISIN', 'Coupon (%)', 'Name Of the Instrument', 'Industry+ /Rating', 'Quantity', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
        ['EQUITY & EQUITY RELATED'],
        ['(a) Listed / awaiting listing on Stock Exchanges'],
        [BANK_STOCK, None, 'HDFC Bank Ltd.', 'Banks', 100, 6000, 60.0],
        ['Sub Total', None, None, None, None, 6000, 60.0],
        ['DEBT INSTRUMENTS'],
        ['Non-Convertible debentures / Bonds'],
        [BOND, 7.1, 'Bajaj Finance Ltd.', 'CRISIL - AA+', 10, 2000, 20.0],
        [BOND, 7.1, 'Bajaj Finance Ltd.', 'CRISIL - AA+', 5, 500, 5.0],
        ['Government Securities (Central/State)'],
        ['IN0020210152', 6.67, '6.67% GOI MAT 151235', 'Sovereign', 10, 1000, 10.0],
        ['Sub Total', None, None, None, None, 3500, 35.0],
        ['OTHERS'],
        ['Alternative Investment Fund Units'],
        ['INF0RQ622028', None, 'Corporate Debt Market Development Fund', None, 1, 100, 1.0],
        ['TREPS - Tri-party Repo'],
        ['TREPS - Tri-party Repo', None, None, None, None, 200, 2.0],
        ['Net Current Assets'],
        ['Net Current Assets', None, None, None, None, 200, 2.0],
        ['Grand Total', None, None, None, None, 10000, 100],
    ])


class HybridParserTests(SimpleTestCase):
    def _read(self, record, payload):
        details = {}
        holdings = _read_holdings(record)(payload, 'x.xlsx', record['scheme_name'], 'hdfc_xlsx_by_scheme', details)
        return holdings, details

    def test_one_hybrid_file_gives_both_equity_and_debt_rows(self):
        holdings, details = self._read(HYBRID_A, hybrid_file())
        self.assertEqual(details['security_classes'][BANK_STOCK], 'equity')
        self.assertEqual(details['security_classes'][BOND], 'debt')
        self.assertEqual(details['classes_read'], ['equity', 'debt'])
        self.assertEqual(holdings[BANK_STOCK], 60.0)

    def test_duplicate_rows_of_one_isin_are_added_together(self):
        holdings, _ = self._read(HYBRID_A, hybrid_file())
        self.assertEqual(holdings[BOND], 25.0)

    def test_weights_stay_a_share_of_the_whole_scheme(self):
        holdings, details = self._read(HYBRID_A, hybrid_file())
        total = sum(holdings.values()) + sum(row['weight'] for row in details['other_holdings'])
        self.assertAlmostEqual(total, 100.0)
        self.assertLess(sum(weight for isin, weight in holdings.items() if details['security_classes'][isin] == 'debt'), 100)

    def test_rating_symbols_survive_in_a_hybrid_file(self):
        _, details = self._read(HYBRID_A, hybrid_file())
        self.assertEqual(details['holding_industries'][BOND], 'CRISIL - AA+')
        self.assertEqual(details['holding_industries'][BANK_STOCK], 'Banks')

    def test_fund_units_and_repo_are_never_eligible_securities(self):
        holdings, details = self._read(HYBRID_A, hybrid_file())
        self.assertNotIn('INF0RQ622028', holdings)
        self.assertEqual({row['instrument_type'] for row in details['other_holdings']}, {'Alternative Investment Fund Units', 'TREPS / Repo', 'Net Current Assets'})

    def test_file_of_another_scheme_is_rejected(self):
        with self.assertRaisesRegex(Exception, 'No sheet matched'):
            self._read(HYBRID_A, hybrid_file('HDFC Conservative Hybrid Fund'))

    def test_a_weight_that_disagrees_with_market_value_is_rejected(self):
        payload = sheet_bytes([
            ['HDFC Aggressive Hybrid Fund'], ['Monthly Portfolio Statement as on September 30, 2026'],
            ['ISIN', 'Name Of the Instrument', 'Industry+ /Rating', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
            ['EQUITY & EQUITY RELATED'], [BANK_STOCK, 'HDFC Bank Ltd.', 'Banks', 5000, 50.0],
            ['DEBT INSTRUMENTS'], [BOND, 'Bajaj Finance Ltd.', 'CRISIL - AAA', 1000, 45.0],
            ['Grand Total', None, None, 10000, 100],
        ])
        with self.assertRaisesRegex(Exception, 'do not match market value'):
            self._read(HYBRID_A, payload)

    def test_equity_fund_keeps_debt_rows_separately_labelled(self):
        holdings, details = self._read(EQUITY_A, hybrid_file('HDFC Large Cap Fund'))
        self.assertEqual(details['security_classes'][BOND], 'debt')
        self.assertEqual(holdings[BANK_STOCK], 60.0)

    def test_a_debt_fund_with_no_equity_section_counts_as_checked(self):
        payload = sheet_bytes([
            ['HDFC Corporate Bond Fund'], ['Monthly Portfolio Statement as on September 30, 2026'],
            ['ISIN', 'Name Of the Instrument', 'Industry+ /Rating', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
            ['DEBT INSTRUMENTS'], [BOND, 'Bajaj Finance Ltd.', 'CRISIL - AAA', 1000, 10.0],
            ['Grand Total', None, None, 10000, 100],
        ])
        _, details = self._read(DEBT_A, payload)
        self.assertEqual(sorted(details['classes_read']), ['debt', 'equity'])

    def test_a_row_nobody_can_classify_is_reported_not_ignored(self):
        payload = sheet_bytes([
            ['HDFC Aggressive Hybrid Fund'], ['Monthly Portfolio Statement as on September 30, 2026'],
            ['ISIN', 'Name Of the Instrument', 'Industry+ /Rating', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
            ['EQUITY & EQUITY RELATED'], [BANK_STOCK, 'HDFC Bank Ltd.', 'Banks', 5000, 50.0],
            ['DEBT INSTRUMENTS'], [BOND, 'Bajaj Finance Ltd.', 'CRISIL - AAA', 2000, 20.0],
            ['OTHERS'], ['INE999Z01011', 'Mystery Instrument', None, 2500, 25.0],
            ['Net Current Assets'], ['Net Current Assets', None, None, 500, 5.0],
            ['Grand Total', None, None, 10000, 100],
        ])
        _, details = self._read(HYBRID_A, payload)
        self.assertEqual(details['unclassified_weight'], 25.0)

    def test_reit_written_out_in_full_is_classified_and_never_debt(self):
        payload = sheet_bytes([
            ['HDFC Aggressive Hybrid Fund'], ['Monthly Portfolio Statement as on September 30, 2026'],
            ['ISIN', 'Name Of the Instrument', 'Industry+ /Rating', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
            ['EQUITY & EQUITY RELATED'], [BANK_STOCK, 'HDFC Bank Ltd.', 'Banks', 5000, 50.0],
            ['DEBT INSTRUMENTS'], [BOND, 'Bajaj Finance Ltd.', 'CRISIL - AAA', 4000, 40.0],
            ['Real Estate Investment Trust'], ['INE0FDU25010', 'Brookfield India Real Estate Trust', 'Realty', 800, 8.0],
            ['Net Current Assets'], ['Net Current Assets', None, None, 200, 2.0],
            ['Grand Total', None, None, 10000, 100],
        ])
        holdings, details = self._read(HYBRID_A, payload)
        self.assertNotIn('INE0FDU25010', holdings)
        self.assertEqual(next(row['instrument_type'] for row in details['other_holdings'] if row.get('isin') == 'INE0FDU25010'), 'REIT / InvIT Units')
        self.assertEqual(details['unclassified_weight'], 0.0)

    def test_equity_block_row_the_reader_did_not_take_is_reported(self):
        with patch('fund_analyzer.holdings._read_equity_holdings', return_value={BANK_STOCK: 60.0}):
            _, details = self._read(HYBRID_A, hybrid_file())
        self.assertEqual(details['classes_read'], ['equity', 'debt'])
        self.assertEqual(details['unclassified_weight'], 0.0)

    def test_segregated_portfolio_text_is_not_part_of_the_name_in_the_file(self):
        record = {**HYBRID_A, 'scheme_name': 'Nippon India Aggressive Hybrid Fund (Existing Number of Segregated Portfolios - 2)'}
        self.assertEqual(file_scheme_name(record), 'Nippon India Aggressive Hybrid Fund')


class UniversalOverlapTests(PortfolioCacheMixin, SimpleTestCase):
    def _analyze(self, *records, months=RECENT, downloads=None):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=list(records)),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=months),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
            patch('fund_analyzer.holdings._download_cached', **(downloads or {'side_effect': AssertionError('no download expected')})),
        ):
            return analyze_holdings_overlap([record['scheme_code'] for record in records])

    def test_equity_with_equity_uses_stocks_only(self):
        # Both hold the same T-bill. It must not change an equity comparison.
        stored(EQUITY_A, {BANK_STOCK: 5.0}, {TBILL: 2.0})
        stored(EQUITY_B, {BANK_STOCK: 3.0}, {TBILL: 2.0})
        pair = self._analyze(EQUITY_A, EQUITY_B)['pairs'][0]
        self.assertEqual((pair['overlap_pct'], pair['equity_overlap_pct'], pair['common_equity_count'], pair['common_count']), (3.0, 3.0, 1, 1))
        self.assertIsNone(pair['debt_overlap_pct'])
        self.assertIsNone(pair['common_debt_count'])

    def test_debt_with_debt_uses_debt_securities_only(self):
        stored(DEBT_A, {BANK_STOCK: 1.0}, {BOND: 5.0})
        stored(DEBT_B, {BANK_STOCK: 1.0}, {BOND: 4.0})
        pair = self._analyze(DEBT_A, DEBT_B)['pairs'][0]
        self.assertEqual((pair['overlap_pct'], pair['debt_overlap_pct'], pair['common_debt_count']), (4.0, 4.0, 1))
        self.assertIsNone(pair['equity_overlap_pct'])

    def test_equity_with_debt_and_no_matching_security_is_a_checked_zero(self):
        stored(EQUITY_A, {BANK_STOCK: 5.0})
        stored(DEBT_A, None, {BOND: 5.0})
        result = self._analyze(EQUITY_A, DEBT_A)
        pair, high = result['pairs'][0], result['highest_overlap']
        self.assertEqual((pair['overlap_pct'], pair['equity_overlap_pct'], pair['debt_overlap_pct']), (0, 0, 0))
        self.assertEqual(pair['message'], 'No common securities')
        self.assertEqual((high['available'], high['percentage'], high['message']), (True, 0.0, 'No common securities'))

    def test_equity_with_debt_counts_a_real_common_security(self):
        stored(EQUITY_A, {BANK_STOCK: 90.0}, {TBILL: 0.5})
        stored(DEBT_A, None, {TBILL: 3.0, BOND: 5.0})
        pair = self._analyze(EQUITY_A, DEBT_A)['pairs'][0]
        self.assertEqual((pair['overlap_pct'], pair['debt_overlap_pct'], pair['equity_overlap_pct'], pair['common_debt_count']), (0.5, 0.5, 0, 1))
        self.assertEqual(pair['common_holdings'][0]['overlap_weight'], 0.5)
        self.assertEqual(pair['common_holdings'][0]['security_class'], 'debt')

    def test_hybrid_with_equity_compares_stocks_only(self):
        stored(HYBRID_A, {BANK_STOCK: 6.0}, {TBILL: 9.0})
        stored(EQUITY_A, {BANK_STOCK: 4.0}, {TBILL: 2.0})
        pair = self._analyze(HYBRID_A, EQUITY_A)['pairs'][0]
        self.assertEqual((pair['overlap_pct'], pair['equity_overlap_pct']), (4.0, 4.0))
        self.assertIsNone(pair['debt_overlap_pct'])

    def test_hybrid_with_debt_compares_debt_only(self):
        stored(HYBRID_A, {BANK_STOCK: 6.0}, {BOND: 3.0})
        stored(DEBT_A, {BANK_STOCK: 1.0}, {BOND: 7.0})
        pair = self._analyze(HYBRID_A, DEBT_A)['pairs'][0]
        self.assertEqual((pair['overlap_pct'], pair['debt_overlap_pct']), (3.0, 3.0))
        self.assertIsNone(pair['equity_overlap_pct'])

    def test_hybrid_with_hybrid_gives_three_figures(self):
        stored(HYBRID_A, {BANK_STOCK: 6.0, IT_STOCK: 2.0}, {BOND: 3.0})
        stored(HYBRID_B, {BANK_STOCK: 4.0}, {BOND: 5.0, TBILL: 1.0})
        pair = self._analyze(HYBRID_A, HYBRID_B)['pairs'][0]
        self.assertEqual((pair['equity_overlap_pct'], pair['debt_overlap_pct'], pair['overlap_pct']), (4.0, 3.0, 7.0))
        self.assertEqual((pair['common_equity_count'], pair['common_debt_count'], pair['common_count']), (1, 1, 2))

    def test_kpi_ranks_on_the_combined_figure_across_fund_types(self):
        stored(EQUITY_A, {BANK_STOCK: 5.0})
        stored(HYBRID_A, {BANK_STOCK: 6.0}, {BOND: 3.0})
        stored(HYBRID_B, {BANK_STOCK: 4.0}, {BOND: 3.0})
        high = self._analyze(EQUITY_A, HYBRID_A, HYBRID_B)['highest_overlap']
        # Hybrid x Hybrid is 4 + 3 = 7, above Equity x Hybrid at 5.
        self.assertEqual((high['percentage'], high['fund_a_code'], high['fund_b_code'], high['compared_pairs'], high['total_pairs']), (7.0, '5', '6', 3, 3))
        self.assertFalse(high['partial_coverage'])

    def test_one_fund_with_missing_holdings_leaves_the_rest_compared(self):
        stored(EQUITY_A, {BANK_STOCK: 5.0})
        stored(EQUITY_B, {BANK_STOCK: 3.0})
        result = self._analyze(EQUITY_A, EQUITY_B, HYBRID_A)
        high = result['highest_overlap']
        self.assertEqual((high['percentage'], high['compared_pairs'], high['total_pairs']), (3.0, 1, 3))
        self.assertTrue(high['partial_coverage'])
        self.assertFalse(next(fund for fund in result['funds'] if fund['scheme_code'] == '5')['available'])

    def test_partial_coverage_across_eight_funds(self):
        records = [{**EQUITY_A, 'scheme_code': str(100 + index), 'scheme_name': f'HDFC Fund {index}'} for index in range(8)]
        for record in records[:3]:
            stored(record, {BANK_STOCK: 5.0})
        high = self._analyze(*records)['highest_overlap']
        self.assertEqual((high['compared_pairs'], high['total_pairs']), (3, 28))
        self.assertTrue(high['partial_coverage'])

    def test_different_reporting_months_are_never_paired(self):
        stored(HYBRID_A, {BANK_STOCK: 5.0}, month='2026-10')
        stored(HYBRID_B, {BANK_STOCK: 3.0}, month='2026-08')
        result = self._analyze(HYBRID_A, HYBRID_B)
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['highest_overlap']['reason_code'], 'NO_COMMON_REPORTING_MONTH')

    def test_latest_common_month_is_used_across_fund_types(self):
        stored(EQUITY_A, {BANK_STOCK: 9.0}, month='2026-10')
        stored(EQUITY_A, {BANK_STOCK: 5.0}, {TBILL: 1.0})
        stored(DEBT_A, None, {TBILL: 2.0})
        pair = self._analyze(EQUITY_A, DEBT_A)['pairs'][0]
        self.assertEqual((pair['portfolio_month'], pair['overlap_pct']), ('2026-09', 1.0))

    def test_two_bonds_from_one_company_stay_separate_across_fund_types(self):
        stored(HYBRID_A, {BANK_STOCK: 6.0}, {BOND: 3.0})
        stored(DEBT_A, None, {OTHER_BOND: 7.0})
        pair = self._analyze(HYBRID_A, DEBT_A)['pairs'][0]
        self.assertEqual(pair['overlap_pct'], 0)
        self.assertEqual(pair['message'], 'No common securities')

    def test_fund_units_never_count_in_any_combination(self):
        units = [{'isin': 'INF0RQ622028', 'name': 'CDMDF', 'weight': 0.4, 'instrument_type': 'Alternative Investment Fund Units'}]
        stored(HYBRID_A, {BANK_STOCK: 6.0}, {BOND: 3.0}, other_holdings=units)
        stored(HYBRID_B, {BANK_STOCK: 4.0}, {BOND: 5.0}, other_holdings=units)
        pair = self._analyze(HYBRID_A, HYBRID_B)['pairs'][0]
        self.assertEqual(pair['overlap_pct'], 7.0)
        self.assertEqual(pair['including_fund_units']['overlap_pct'], 7.4)

    def test_equity_pair_with_common_etf_units_shows_no_fund_unit_figure(self):
        units = [{'isin': 'INF204KB14I2', 'name': 'Liquid ETF', 'weight': 0.4, 'instrument_type': 'Mutual Fund Units / ETFs'}]
        stored(EQUITY_A, {BANK_STOCK: 5.0}, other_holdings=units)
        stored(EQUITY_B, {BANK_STOCK: 3.0}, other_holdings=units)
        self.assertNotIn('including_fund_units', self._analyze(EQUITY_A, EQUITY_B)['pairs'][0])

    def test_zero_overlap_and_unavailable_overlap_stay_different(self):
        stored(EQUITY_A, {BANK_STOCK: 5.0})
        stored(DEBT_A, None, {BOND: 5.0})
        checked = self._analyze(EQUITY_A, DEBT_A)['highest_overlap']
        self.assertEqual((checked['available'], checked['percentage'], checked['reason_code']), (True, 0.0, 'NO_COMMON_SECURITIES'))
        stored(EQUITY_B, {BANK_STOCK: 5.0})
        missing = self._analyze(EQUITY_B, DEBT_B)['highest_overlap']
        self.assertEqual((missing['available'], missing['percentage'], missing['reason_code']), (False, None, 'HOLDINGS_UNAVAILABLE'))

    def test_file_cached_before_labels_is_not_treated_as_holding_no_debt(self):
        # This file only ever recorded stocks, so 0% against a debt fund would be a guess.
        store_snapshot(EQUITY_A, '1', '2026-09', holdings_result({BANK_STOCK: 5.0}))
        stored(DEBT_A, None, {TBILL: 2.0})
        result = self._analyze(EQUITY_A, DEBT_A)
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['unavailable_pairs'][0]['reason'], 'HOLDINGS_UNAVAILABLE')
        self.assertEqual(result['highest_overlap']['message'], 'Holdings unavailable')

    def test_file_cached_before_labels_still_serves_equity_pairs(self):
        store_snapshot(EQUITY_A, '1', '2026-09', holdings_result({BANK_STOCK: 5.0}))
        store_snapshot(EQUITY_B, '2', '2026-09', holdings_result({BANK_STOCK: 3.0}))
        self.assertEqual(self._analyze(EQUITY_A, EQUITY_B)['pairs'][0]['overlap_pct'], 3.0)

    def test_file_cached_before_labels_is_re_read_from_its_official_source(self):
        store_snapshot(EQUITY_A, '1', '2026-09', holdings_result({BANK_STOCK: 60.0}), source_url='https://files.hdfcfund.com/a.xlsx')
        stored(DEBT_A, None, {BOND: 2.0})
        result = self._analyze(EQUITY_A, DEBT_A, downloads={'return_value': hybrid_file('HDFC Large Cap Fund')})
        self.assertEqual(result['pairs'][0]['overlap_pct'], 2.0)
        self.assertEqual(read_snapshot(EQUITY_A, '2026-09')['classes_read'], ['equity', 'debt'])

    def test_a_fund_with_no_partner_yet_is_not_made_to_wait_a_day(self):
        link = [{'url': 'https://x.test/a.xlsx', 'text': 'a', 'month': '2026-09'}]
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[HYBRID_A, HYBRID_B]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=lambda _source, name: link if name == HYBRID_A['scheme_name'] else []),
            patch('fund_analyzer.holdings._download_cached', side_effect=AssertionError('file should not be opened')),
        ):
            analyze_holdings_overlap(['5', '6'])
        self.assertTrue(discovery_due(HYBRID_A))

    def test_material_unclassified_rows_are_reported_as_incomplete(self):
        stored(HYBRID_A, {BANK_STOCK: 6.0}, {BOND: 3.0}, unclassified_weight=25.0)
        stored(HYBRID_B, {BANK_STOCK: 4.0}, {BOND: 5.0}, unclassified_weight=0.2)
        result = self._analyze(HYBRID_A, HYBRID_B)
        self.assertTrue(result['pairs'][0]['incomplete_coverage'])
        self.assertEqual(next(fund for fund in result['funds'] if fund['scheme_code'] == '5')['unclassified_pct'], 25.0)
        self.assertNotIn('unclassified_pct', next(fund for fund in result['funds'] if fund['scheme_code'] == '6'))

    def test_hybrid_fundamentals_show_both_kinds_without_rescaling(self):
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[{'url': 'https://x.test/a.xlsx', 'text': 'a', 'month': '2026-09'}]),
            patch('fund_analyzer.holdings._download_cached', return_value=hybrid_file()),
        ):
            result = fetch_fund_holdings('5', HYBRID_A)
        self.assertEqual(result['asset_class'], 'hybrid')
        self.assertEqual(result['allocation'], {'equity': 60.0, 'debt_securities': 35.0, 'other': 5.0})
        rows = {row['name']: row for row in result['holdings']}
        self.assertEqual((rows['HDFC Bank Ltd.']['instrument_type'], rows['HDFC Bank Ltd.']['sector']), ('Equity Shares', 'Banks'))
        self.assertEqual(rows['Bajaj Finance Ltd.']['rating'], 'CRISIL - AA+')
        self.assertTrue(rows['Corporate Debt Market Development Fund']['excluded_from_overlap'])
        self.assertAlmostEqual(sum(row['weight'] for row in result['sectors']), 100.0)

    def test_equity_fundamentals_ignore_debt_rows(self):
        stored(EQUITY_A, {BANK_STOCK: 60.0}, {TBILL: 2.0})
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
        ):
            result = fetch_fund_holdings('1', EQUITY_A)
        self.assertEqual([row['isin'] for row in result['holdings']], [BANK_STOCK])
        self.assertEqual(result['allocation']['indian_equity'], 60.0)
