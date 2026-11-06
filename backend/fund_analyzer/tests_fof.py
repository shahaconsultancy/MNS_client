"""Tests for Fund of Funds holdings and Direct FoF Overlap. These use no database."""
from unittest.mock import patch

from django.test import SimpleTestCase

from .holdings import (
    _is_fof_fund,
    _read_fund_unit_holdings,
    _share_class_identities,
    analyze_holdings_overlap,
    fetch_fund_holdings,
)
from .portfolio_store import read_snapshot, store_snapshot
from .tests_debt import HDFC_DEBT, RECENT, SBI_DEBT, sheet_bytes
from .tests_pipeline import HDFC_LARGE, PortfolioCacheMixin, holdings_result

FOF_CATEGORY = 'Other Scheme - FoF Domestic'
HDFC_FOF = {'scheme_code': '9001', 'scheme_name': 'HDFC Multi Asset Active FOF', 'amc': 'HDFC Mutual Fund', 'plan': 'Direct Plan', 'category': FOF_CATEGORY}
SBI_FOF = {'scheme_code': '9002', 'scheme_name': 'SBI Multi Asset Allocation FoF', 'amc': 'SBI Mutual Fund', 'plan': 'Direct Plan', 'category': FOF_CATEGORY}

# Real-looking Indian fund unit ISINs. Only the INF prefix and the shape matter here.
UNIT_1, UNIT_2, UNIT_3, UNIT_4 = 'INF179K01XQ0', 'INF204K01XL3', 'INF846K01EW2', 'INF109K012R6'


def fof_file(name, units, date='September 30, 2026', extra_rows=(), cash=2.0):
    """Layout of an AMC monthly file for a Fund of Funds: unit rows under a Mutual Fund Units heading."""
    rows = [
        [name],
        [f'Monthly Portfolio Statement as on {date}'],
        ['ISIN', 'Coupon (%)', 'Name Of the Instrument', 'Industry+ /Rating', 'Quantity', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
        ['MUTUAL FUND UNITS'],
    ]
    for isin, unit_name, weight in units:
        rows.append([isin, None, unit_name, None, 1000, weight * 1000, weight])
    rows.extend(extra_rows)
    rows.append(['TREPS - Tri-party Repo'])
    rows.append(['TREPS - Tri-party Repo', None, None, None, None, cash * 1000, cash])
    total = sum(weight for _, _, weight in units) + sum(row[-1] for row in extra_rows if len(row) > 1) + cash
    rows.append(['Grand Total', None, None, None, None, total * 1000, total])
    return sheet_bytes(rows)


def stored_fof(record, code, units, month='2026-09', date='2026-09-30'):
    """A verified month as the JSON cache keeps it. units is {isin: weight}."""
    store_snapshot(record, code, month, {
        **holdings_result(units, date), 'asset_class': 'fof',
        'security_classes': {isin: 'fund_unit' for isin in units}, 'classes_read': ['fund_unit'],
        'instrument_types': {isin: 'Mutual Fund Units / ETFs' for isin in units},
    })


class FofClassificationTests(SimpleTestCase):
    def test_amfi_fof_categories_are_fund_of_funds(self):
        self.assertTrue(_is_fof_fund({'category': 'Other Scheme - FoF Domestic'}))
        self.assertTrue(_is_fof_fund({'category': 'Other Scheme - FoF Overseas'}))

    def test_a_name_alone_never_makes_a_fund_of_funds(self):
        self.assertFalse(_is_fof_fund({'category': 'Equity Scheme - Large Cap Fund', 'scheme_name': 'Some Fund of Funds'}))
        self.assertFalse(_is_fof_fund({'category': 'Other Scheme - Index Funds'}))
        self.assertFalse(_is_fof_fund({'category': None}))


class FofParserTests(SimpleTestCase):
    def _read(self, payload, name='HDFC Multi Asset Active FOF'):
        diagnostics = {}
        return _read_fund_unit_holdings(payload, 'x.xlsx', name, 'hdfc_xlsx_by_scheme', diagnostics), diagnostics

    def test_units_are_read_with_isin_name_and_percent_of_nav(self):
        holdings, details = self._read(fof_file('HDFC Multi Asset Active FOF', [(UNIT_1, 'Alpha Gold ETF', 40.0), (UNIT_2, 'Beta Liquid Fund', 58.0)]))
        self.assertEqual(holdings, {UNIT_1: 40.0, UNIT_2: 58.0})
        self.assertEqual(details['holding_names'], {UNIT_1: 'Alpha Gold ETF', UNIT_2: 'Beta Liquid Fund'})
        self.assertEqual(details['security_classes'], {UNIT_1: 'fund_unit', UNIT_2: 'fund_unit'})
        self.assertEqual(details['classes_read'], ['fund_unit'])
        self.assertEqual(details['reporting_date'], '2026-09-30')

    def test_a_single_fund_unit_over_100_percent_is_read_when_net_assets_are_negative(self):
        # SBI Gold Fund's September 2026 file: one unit at 100.16% beside net payables of -0.45%.
        payload = fof_file('HDFC Multi Asset Active FOF', [(UNIT_1, 'Alpha Gold ETF', 100.16)], extra_rows=[['Net Receivable / Payable', None, 'Net Receivable / Payable', None, None, -450.0, -0.45]], cash=0.29)
        holdings, details = self._read(payload)
        self.assertEqual(holdings, {UNIT_1: 100.16})
        self.assertEqual(next(row for row in details['other_holdings'] if 'Net Receivable' in row['name'])['weight'], -0.45)

    def test_a_fund_unit_weight_that_does_not_match_its_market_value_is_rejected(self):
        # Weights are only trusted when they agree with market value over the grand total.
        payload = sheet_bytes([
            ['HDFC Multi Asset Active FOF'], ['Monthly Portfolio Statement as on September 30, 2026'],
            ['ISIN', 'Coupon (%)', 'Name Of the Instrument', 'Industry+ /Rating', 'Quantity', 'Market/ Fair Value (Rs. in Lacs.)', '% to NAV'],
            ['MUTUAL FUND UNITS'], [UNIT_1, None, 'Alpha Gold ETF', None, 10, 5000.0, 90.0], ['Grand Total', None, None, None, None, 10000.0, 100.0],
        ])
        with self.assertRaisesRegex(Exception, 'do not match market value'):
            self._read(payload)

    def test_repo_is_not_a_fund_unit(self):
        holdings, details = self._read(fof_file('HDFC Multi Asset Active FOF', [(UNIT_1, 'Alpha Gold ETF', 98.0)]))
        self.assertEqual(list(holdings), [UNIT_1])
        repo = next(row for row in details['other_holdings'] if row['instrument_type'] == 'TREPS / Repo')
        self.assertEqual(repo['weight'], 2.0)

    def test_cdmdf_stays_visible_but_is_not_an_underlying_fund(self):
        aif = [['Alternative Investment Fund Units'], ['INF0RQ622028', None, 'Corporate Debt Market Development Fund', None, 10, 360.0, 0.36]]
        holdings, details = self._read(fof_file('HDFC Multi Asset Active FOF', [(UNIT_1, 'Alpha Gold ETF', 97.64)], extra_rows=aif))
        self.assertNotIn('INF0RQ622028', holdings)
        cdmdf = next(row for row in details['other_holdings'] if row.get('isin') == 'INF0RQ622028')
        self.assertEqual(cdmdf['instrument_type'], 'Alternative Investment Fund Units')

    def test_a_file_without_fund_units_is_unreadable_not_empty_zero(self):
        payload = sheet_bytes([
            ['HDFC Multi Asset Active FOF'], ['Monthly Portfolio Statement as on September 30, 2026'],
            ['ISIN', 'Name Of the Instrument', '% to NAV'], ['EQUITY & EQUITY RELATED'], ['INE040A01034', 'HDFC Bank', 5.0],
        ])
        with self.assertRaisesRegex(Exception, 'No mutual fund unit holdings'):
            self._read(payload)

    def test_a_non_fund_isin_under_no_fund_heading_is_not_taken_for_a_unit(self):
        # Equal-looking names prove nothing: only the INF prefix or a fund-unit heading does.
        bond = [['Debt Instruments'], ['INE296A07SV1', None, 'Alpha Liquid Fund Ltd', None, 10, 1000.0, 1.0]]
        holdings, _ = self._read(fof_file('HDFC Multi Asset Active FOF', [(UNIT_1, 'Alpha Gold ETF', 97.0)], extra_rows=bond))
        self.assertEqual(list(holdings), [UNIT_1])


class FofOverlapTests(PortfolioCacheMixin, SimpleTestCase):
    def _analyze(self, records, *, codes=None, links=None, download=None):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **(links or {'return_value': []})),
            patch('fund_analyzer.holdings._download_cached', **(download or {'side_effect': AssertionError('no download expected')})),
        ):
            return analyze_holdings_overlap(codes or [record['scheme_code'] for record in records])

    def _live(self, files):
        def links(_source, name):
            return [{'url': f'https://x.test/{name}.xlsx', 'text': name, 'month': '2026-09'}]

        return {'side_effect': links}, {'side_effect': lambda _key, _month, url: files[url.rsplit('/', 1)[-1][:-5]]}

    def test_two_fund_of_funds_use_the_sum_of_minimum_weights_of_common_units(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0, UNIT_2: 20.0, UNIT_3: 10.0})
        stored_fof(SBI_FOF, '9002', {UNIT_1: 25.0, UNIT_2: 25.0, UNIT_4: 5.0})
        result = self._analyze([HDFC_FOF, SBI_FOF])
        pair = result['pairs'][0]
        # min(30, 25) + min(20, 25). UNIT_3 and UNIT_4 are held by one fund each.
        self.assertEqual(pair['overlap_pct'], 45.0)
        self.assertEqual((pair['overlap_type'], pair['overlap_label']), ('DIRECT_FOF', 'Direct FoF Overlap'))
        self.assertEqual((pair['common_count'], pair['common_fund_unit_count']), (2, 2))
        self.assertEqual((pair['portfolio_month'], pair['portfolio_date']), ('2026-09', '2026-09-30'))
        # Largest contribution first: UNIT_1 adds 25, UNIT_2 adds 20.
        self.assertEqual([row['isin'] for row in pair['common_holdings']], [UNIT_1, UNIT_2])
        self.assertIsNone(pair['message'])

    def test_direct_fof_overlap_is_not_an_equity_or_debt_figure(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_1: 25.0})
        pair = self._analyze([HDFC_FOF, SBI_FOF])['pairs'][0]
        self.assertEqual((pair['equity_overlap_pct'], pair['debt_overlap_pct']), (None, None))
        self.assertEqual((pair['common_equity_count'], pair['common_debt_count']), (None, None))
        self.assertNotIn('including_fund_units', pair)

    def test_summary_reports_percentage_count_and_date_for_the_fof_pair(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_1: 25.0})
        high = self._analyze([HDFC_FOF, SBI_FOF])['highest_overlap']
        self.assertEqual((high['available'], high['percentage']), (True, 25.0))
        self.assertEqual((high['overlap_type'], high['overlap_label']), ('DIRECT_FOF', 'Direct FoF Overlap'))
        self.assertEqual((high['portfolio_month'], high['portfolio_date']), ('2026-09', '2026-09-30'))

    def test_fof_overlap_is_calculated_from_the_official_files(self):
        files = {
            HDFC_FOF['scheme_name']: fof_file(HDFC_FOF['scheme_name'], [(UNIT_1, 'Alpha Gold ETF', 60.0), (UNIT_2, 'Beta Liquid Fund', 38.0)]),
            SBI_FOF['scheme_name']: fof_file(SBI_FOF['scheme_name'], [(UNIT_1, 'Alpha Gold ETF', 45.0), (UNIT_3, 'Gamma Bond Fund', 53.0)]),
        }
        links, download = self._live(files)
        result = self._analyze([HDFC_FOF, SBI_FOF], links=links, download=download)
        self.assertEqual(result['pairs'][0]['overlap_pct'], 45.0)
        self.assertEqual(result['highest_overlap']['portfolio_date'], '2026-09-30')

    def test_fof_overlap_survives_the_json_cache(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_1: 25.0})
        self.assertIsNotNone(read_snapshot(HDFC_FOF, '2026-09'))
        with patch('fund_analyzer.holdings.cache.get', return_value=None):
            result = self._analyze([HDFC_FOF, SBI_FOF])
        self.assertEqual(result['pairs'][0]['overlap_pct'], 25.0)

    def test_a_single_unit_fof_over_100_percent_is_kept_in_the_json_cache(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 100.16})
        self.assertIsNotNone(read_snapshot(HDFC_FOF, '2026-09'))
        stored_fof(SBI_FOF, '9002', {UNIT_1: 50.0})
        self.assertEqual(self._analyze([HDFC_FOF, SBI_FOF])['pairs'][0]['overlap_pct'], 50.0)

    def test_no_common_unit_is_a_zero_with_a_reason_not_a_missing_figure(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_4: 25.0})
        high = self._analyze([HDFC_FOF, SBI_FOF])['highest_overlap']
        self.assertEqual(high['percentage'], 0.0)
        self.assertEqual((high['message'], high['reason_code']), ('No common underlying funds', 'NO_COMMON_UNDERLYING_FUNDS'))

    def test_similar_names_are_not_matched_without_the_same_isin(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_4: 25.0})
        for record, code, isin in ((HDFC_FOF, '9001', UNIT_1), (SBI_FOF, '9002', UNIT_4)):
            snapshot = read_snapshot(record, '2026-09')
            self.assertEqual(snapshot['holdings'][0]['isin'], isin)
        # Both funds hold a fund the AMC calls the same thing, under different ISINs.
        from .portfolio_store import _portfolio_dir
        for record, isin in ((HDFC_FOF, UNIT_1), (SBI_FOF, UNIT_4)):
            path = _portfolio_dir(record) / '2026-09.json'
            path.write_text(path.read_text(encoding='utf-8').replace(f'Stock {isin[-2:]}', 'Gold Fund - Direct Plan'), encoding='utf-8')
        result = self._analyze([HDFC_FOF, SBI_FOF])
        self.assertEqual(result['pairs'][0]['overlap_pct'], 0.0)
        self.assertEqual(result['pairs'][0]['common_holdings'], [])

    def test_duplicate_fund_unit_rows_are_added_up_once_per_isin(self):
        # The same unit listed twice in one file is one holding of 10%, not two.
        dup = fof_file(HDFC_FOF['scheme_name'], [(UNIT_1, 'Alpha Gold ETF', 6.0), (UNIT_1, 'Alpha Gold ETF', 4.0), (UNIT_2, 'Beta Liquid Fund', 88.0)])
        other = fof_file(SBI_FOF['scheme_name'], [(UNIT_1, 'Alpha Gold ETF', 8.0), (UNIT_3, 'Gamma Bond Fund', 90.0)])
        links, download = self._live({HDFC_FOF['scheme_name']: dup, SBI_FOF['scheme_name']: other})
        pair = self._analyze([HDFC_FOF, SBI_FOF], links=links, download=download)['pairs'][0]
        self.assertEqual(pair['overlap_pct'], 8.0)
        self.assertEqual(pair['common_fund_unit_count'], 1)
        self.assertEqual((pair['common_holdings'][0]['left_weight'], pair['common_holdings'][0]['right_weight']), (10.0, 8.0))

    def test_two_isins_of_one_amfi_scheme_code_are_one_underlying_fund(self):
        underlying = {'scheme_code': '700', 'scheme_name': 'Alpha Liquid Fund', 'amc': 'X Mutual Fund', 'isin': UNIT_1, 'isin_reinvest': UNIT_2, 'category': 'Debt Scheme - Liquid Fund'}
        self.assertEqual(_share_class_identities([underlying]), {UNIT_1: 'AMFI:700', UNIT_2: 'AMFI:700'})
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_2: 25.0})
        pair = self._analyze([HDFC_FOF, SBI_FOF, underlying], codes=['9001', '9002'])['pairs'][0]
        self.assertEqual(pair['overlap_pct'], 25.0)
        self.assertEqual((pair['common_holdings'][0]['isin'], pair['common_holdings'][0]['right_isin'], pair['common_holdings'][0]['matched_by']), (UNIT_1, UNIT_2, 'AMFI share class'))

    def test_isins_amfi_does_not_tie_together_are_not_merged(self):
        direct = {'scheme_code': '701', 'scheme_name': 'Alpha Liquid Fund - Direct', 'amc': 'X Mutual Fund', 'isin': UNIT_1, 'isin_reinvest': None}
        regular = {'scheme_code': '702', 'scheme_name': 'Alpha Liquid Fund - Regular', 'amc': 'X Mutual Fund', 'isin': UNIT_2, 'isin_reinvest': None}
        self.assertEqual(_share_class_identities([direct, regular]), {})
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_2: 25.0})
        pair = self._analyze([HDFC_FOF, SBI_FOF, direct, regular], codes=['9001', '9002'])['pairs'][0]
        self.assertEqual(pair['overlap_pct'], 0.0)

    def test_fof_with_equity_has_no_figure_and_says_why(self):
        other = {**HDFC_LARGE, 'scheme_code': '2', 'scheme_name': 'HDFC Mid Cap Fund'}
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        store_snapshot(other, '2', '2026-09', holdings_result({'INE040A01034': 5.0}))
        result = self._analyze([HDFC_FOF, other])
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['unavailable_pairs'][0]['reason'], 'FOF_LOOKTHROUGH_UNAVAILABLE')
        high = result['highest_overlap']
        self.assertEqual((high['available'], high['percentage'], high['reason_code']), (False, None, 'FOF_LOOKTHROUGH_UNAVAILABLE'))
        self.assertIn('Look-through overlap is unavailable', high['message'])

    def test_fof_with_debt_has_no_figure_and_says_why(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        store_snapshot(SBI_DEBT, '146215', '2026-09', {**holdings_result({'INE296A07SV1': 4.0}), 'asset_class': 'debt'})
        result = self._analyze([HDFC_FOF, SBI_DEBT])
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['highest_overlap']['reason_code'], 'FOF_LOOKTHROUGH_UNAVAILABLE')
        self.assertIsNone(result['highest_overlap']['percentage'])

    def test_fof_even_sharing_a_unit_with_a_debt_fund_is_not_compared(self):
        # The debt fund holds the same units, but that is not a look-through of the FoF.
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        store_snapshot(SBI_DEBT, '146215', '2026-09', {
            **holdings_result({'INE296A07SV1': 4.0}), 'asset_class': 'debt',
            'other_holdings': [{'isin': UNIT_1, 'name': 'Alpha Gold ETF', 'weight': 3.0, 'instrument_type': 'Mutual Fund Units / ETFs'}],
        })
        self.assertEqual(self._analyze([HDFC_FOF, SBI_DEBT])['pairs'], [])

    def test_equity_and_debt_pairs_are_unchanged_beside_a_fof(self):
        other = {**HDFC_LARGE, 'scheme_code': '2', 'scheme_name': 'HDFC Mid Cap Fund'}
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(other, '2', '2026-09', holdings_result({'INE040A01034': 4.0}))
        result = self._analyze([HDFC_FOF, HDFC_LARGE, other])
        self.assertEqual(len(result['pairs']), 1)
        self.assertEqual((result['pairs'][0]['overlap_pct'], result['pairs'][0]['overlap_type']), (4.0, 'SECURITIES'))
        self.assertEqual(len(result['unavailable_pairs']), 2)
        self.assertEqual(result['highest_overlap']['percentage'], 4.0)

    def test_fof_pair_never_outranks_a_security_pair_in_the_summary(self):
        other = {**HDFC_LARGE, 'scheme_code': '2', 'scheme_name': 'HDFC Mid Cap Fund'}
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 60.0})
        stored_fof(SBI_FOF, '9002', {UNIT_1: 55.0})
        store_snapshot(HDFC_LARGE, '119018', '2026-09', holdings_result({'INE040A01034': 5.0}))
        store_snapshot(other, '2', '2026-09', holdings_result({'INE040A01034': 4.0}))
        result = self._analyze([HDFC_FOF, SBI_FOF, HDFC_LARGE, other])
        pairs = {pair['overlap_type']: pair['overlap_pct'] for pair in result['pairs']}
        self.assertEqual(pairs, {'DIRECT_FOF': 55.0, 'SECURITIES': 4.0})
        self.assertEqual((result['highest_overlap']['overlap_type'], result['highest_overlap']['percentage']), ('SECURITIES', 4.0))

    def test_missing_holdings_show_unavailable_not_zero(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        result = self._analyze([HDFC_FOF, SBI_FOF])
        high = result['highest_overlap']
        self.assertEqual((high['available'], high['percentage']), (False, None))
        self.assertEqual((high['message'], high['reason_code']), ('Holdings unavailable', 'HOLDINGS_UNAVAILABLE'))
        self.assertEqual(result['pairs'], [])

    def test_different_months_are_not_compared(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0}, month='2026-10', date='2026-10-31')
        stored_fof(SBI_FOF, '9002', {UNIT_1: 25.0}, month='2026-08', date='2026-08-31')
        high = self._analyze([HDFC_FOF, SBI_FOF])['highest_overlap']
        self.assertIsNone(high['percentage'])
        self.assertEqual((high['message'], high['reason_code']), ('No common reporting month', 'NO_COMMON_REPORTING_MONTH'))

    def test_latest_common_month_is_used_for_fof(self):
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 90.0}, month='2026-10', date='2026-10-31')
        stored_fof(HDFC_FOF, '9001', {UNIT_1: 30.0})
        stored_fof(SBI_FOF, '9002', {UNIT_1: 25.0})
        high = self._analyze([HDFC_FOF, SBI_FOF])['highest_overlap']
        self.assertEqual((high['percentage'], high['portfolio_month'], high['portfolio_date']), (25.0, '2026-09', '2026-09-30'))

    def test_fundamentals_show_underlying_units_and_leave_cdmdf_excluded(self):
        aif = [['Alternative Investment Fund Units'], ['INF0RQ622028', None, 'Corporate Debt Market Development Fund', None, 10, 360.0, 0.36]]
        files = {HDFC_FOF['scheme_name']: fof_file(HDFC_FOF['scheme_name'], [(UNIT_1, 'Alpha Gold ETF', 60.0), (UNIT_2, 'Beta Liquid Fund', 37.64)], extra_rows=aif)}
        links, download = self._live(files)
        with (
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', **links),
            patch('fund_analyzer.holdings._download_cached', **download),
        ):
            result = fetch_fund_holdings('9001', HDFC_FOF)
        self.assertEqual(result['asset_class'], 'fof')
        self.assertEqual(result['allocation'], {'fund_units': 97.64, 'other': 2.36})
        self.assertEqual([row['isin'] for row in result['holdings'][:2]], [UNIT_1, UNIT_2])
        cdmdf = next(row for row in result['holdings'] if row.get('isin') == 'INF0RQ622028')
        self.assertTrue(cdmdf['excluded_from_overlap'])
