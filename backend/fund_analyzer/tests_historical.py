"""Tests for matured schemes, unsupported sources and historical overlap. These use no database."""
from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIClient

from .holdings import _asset_class, analyze_holdings_overlap, scheme_status
from .portfolio_store import store_snapshot
from .providers import ProviderError, infer_plan_option, plan_option_conflict
from .serializers import CompareRequestSerializer
from .services import fetch_fund_data
from .tests_debt import HDFC_DEBT, RECENT, SBI_DEBT
from .tests_pipeline import PortfolioCacheMixin, holdings_result

# Shaped like AMFI's real record for scheme 146082: closed, filed under the old 'Income' category,
# last NAV on the day its 1,202-day term ended.
MATURED = {
    'scheme_code': '146082', 'scheme_name': 'IDFC FIXED TERM PLAN SERIES - 167 (1202 DAYS)', 'amc': 'Bandhan Mutual Fund',
    'plan': None, 'option': None, 'category': 'Income', 'scheme_type': 'Close', 'date': '26-Apr-2022', 'isin': 'INF194KB1298',
}
UNSUPPORTED = {'scheme_code': '777', 'scheme_name': 'Nowhere Debt Fund', 'amc': 'Nowhere Mutual Fund', 'category': 'Debt Scheme - Corporate Bond Fund', 'scheme_type': 'Open', 'date': '04-Nov-2026'}


def debt_month(weights, month, day='30'):
    return {**holdings_result(weights, f'{month}-{day}'), 'asset_class': 'debt'}


class SchemeStatusTests(SimpleTestCase):
    def test_a_closed_scheme_with_an_old_last_nav_has_matured(self):
        state = scheme_status(MATURED, today=date(2026, 11, 5))
        self.assertEqual((state['status'], state['last_nav_date']), ('MATURED', '2022-04-26'))

    def test_an_open_scheme_with_an_old_last_nav_is_discontinued_not_matured(self):
        self.assertEqual(scheme_status({**MATURED, 'scheme_type': 'Open'}, today=date(2026, 11, 5))['status'], 'DISCONTINUED')

    def test_a_scheme_with_a_current_nav_is_active_whatever_its_type(self):
        self.assertEqual(scheme_status({**MATURED, 'date': '04-Nov-2026'}, today=date(2026, 11, 5))['status'], 'ACTIVE')

    def test_a_record_with_no_nav_date_is_not_called_matured(self):
        self.assertEqual(scheme_status({'scheme_type': 'Close'})['status'], 'ACTIVE')


class AssetClassTests(SimpleTestCase):
    def test_the_income_category_is_debt_not_equity(self):
        self.assertEqual(_asset_class(MATURED), 'debt')

    def test_other_legacy_categories_keep_their_real_class(self):
        self.assertEqual(_asset_class({'category': 'Gilt'}), 'debt')
        self.assertEqual(_asset_class({'category': 'Balanced'}), 'hybrid')
        self.assertEqual(_asset_class({'category': 'Growth'}), 'equity')
        self.assertEqual(_asset_class({'category': 'Equity Scheme - Large Cap Fund'}), 'equity')

    def test_current_debt_and_hybrid_categories_are_unchanged(self):
        self.assertEqual(_asset_class(HDFC_DEBT), 'debt')
        self.assertEqual(_asset_class({'category': 'Hybrid Scheme - Multi Asset Allocation'}), 'hybrid')
        self.assertEqual(_asset_class({'category': 'Other Scheme - FoF Domestic'}), 'fof')


class PlanOptionTests(SimpleTestCase):
    MFAPI_NAME = 'IDFC Fixed Term Plan Series 167 REGULAR PLAN HALF YEARLY-IDCW (1202 days)'

    def test_names_state_their_plan_and_option(self):
        self.assertEqual(infer_plan_option(self.MFAPI_NAME), ('Regular Plan', 'IDCW'))
        self.assertEqual(infer_plan_option('HDFC Banking and PSU Debt Fund - Direct Plan - Growth Option'), ('Direct Plan', 'Growth'))
        self.assertEqual(infer_plan_option('SBI Corporate Bond Fund'), (None, None))

    def test_different_plan_or_option_is_a_conflict(self):
        direct_growth = {'scheme_name': 'X Fund', 'plan': 'Direct Plan', 'option': 'Growth Option'}
        self.assertIn('plan differs', plan_option_conflict(direct_growth, {'scheme_name': 'X Fund REGULAR PLAN GROWTH'}))
        self.assertIn('option differs', plan_option_conflict(direct_growth, {'scheme_name': 'X Fund DIRECT PLAN IDCW'}))

    def test_agreeing_or_silent_records_are_not_a_conflict(self):
        direct_growth = {'scheme_name': 'X Fund', 'plan': 'Direct Plan', 'option': 'Growth Option'}
        self.assertIsNone(plan_option_conflict(direct_growth, {'scheme_name': 'X Fund - Direct - Growth'}))
        # AMFI lists no plan or option for 146082, so MFAPI's name cannot contradict it.
        self.assertIsNone(plan_option_conflict(MATURED, {'scheme_name': self.MFAPI_NAME}))
        self.assertIsNone(plan_option_conflict({'scheme_name': 'X Fund', 'option': 'Dividend Option'}, {'scheme_name': 'X Fund IDCW'}))

    def _fund_with_backup(self, mfapi_name):
        record = {'scheme_code': '5', 'scheme_name': 'X Fund - Direct Plan - Growth', 'plan': 'Direct Plan', 'option': 'Growth Option', 'amc': 'X', 'nav': 11.0, 'date': '04-Nov-2026'}
        payload = {'meta': {'scheme_name': mfapi_name, 'fund_house': 'X'}, 'data': [{'date': '03-11-2026', 'nav': '10.9'}, {'date': '04-11-2026', 'nav': '11.0'}]}
        with patch('fund_analyzer.services.fetch_amfi_history', side_effect=ProviderError('AMFI history down')), patch('fund_analyzer.services.fetch_mfapi_data', return_value=payload):
            return fetch_fund_data('5', latest_records=[record], include_metadata=False)

    def test_backup_history_of_another_plan_is_refused_not_mixed(self):
        with self.assertRaisesRegex(ProviderError, 'Unable to load scheme 5'):
            self._fund_with_backup('X Fund - Regular Plan - IDCW')

    def test_backup_history_of_the_same_plan_is_used(self):
        fund = self._fund_with_backup('X Fund - Direct Plan - Growth')
        self.assertEqual((fund['source'], fund['plan']), ('MFAPI (backup)', 'Direct Plan'))


class OverlapStatusTests(PortfolioCacheMixin, SimpleTestCase):
    def _analyze(self, records, codes=None, month=None, discover=None):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]) as listing,
            patch('fund_analyzer.holdings._download_cached', side_effect=AssertionError('no download expected')),
        ):
            result = analyze_holdings_overlap(codes or [record['scheme_code'] for record in records], month)
        self.last_listing = listing
        return result

    def _fund(self, result, code):
        return next(fund for fund in result['funds'] if fund['scheme_code'] == code)

    def test_unsupported_source_is_its_own_reason(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', debt_month({'INE296A07SV1': 5.0}, '2026-09'))
        result = self._analyze([UNSUPPORTED, HDFC_DEBT])
        fund = self._fund(result, '777')
        self.assertEqual((fund['status'], fund['reason_code'], fund['available']), ('SOURCE_NOT_SUPPORTED', 'SOURCE_NOT_SUPPORTED', False))
        self.assertEqual(result['pair_reasons'][0]['reason_code'], 'SOURCE_NOT_SUPPORTED')
        self.assertEqual(result['pairs'], [])
        self.assertIsNone(result['highest_overlap']['percentage'])

    def test_a_matured_scheme_is_historical_and_recent_months_are_not_searched_for_it(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', debt_month({'INE296A07SV1': 5.0}, '2026-09'))
        result = self._analyze([MATURED, HDFC_DEBT])
        fund = self._fund(result, '146082')
        self.assertEqual((fund['status'], fund['reason_code'], fund['scheme_status'], fund['last_nav_date']), ('HISTORICAL_HOLDINGS_UNAVAILABLE', 'HISTORICAL_HOLDINGS_UNAVAILABLE', 'MATURED', '2022-04-26'))
        self.assertEqual(fund['message'], 'Historical portfolio unavailable for IDFC FIXED TERM PLAN SERIES - 167 (1202 DAYS).')
        # The AMC's site was never asked about the matured scheme. HDFC's own listing was.
        asked = [call.args[1] for call in self.last_listing.call_args_list]
        self.assertIn('HDFC Banking and PSU Debt Fund', asked)
        self.assertNotIn('IDFC FIXED TERM PLAN SERIES - 167 (1202 DAYS)', asked)

    def test_a_matured_scheme_never_shows_zero(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', debt_month({'INE296A07SV1': 5.0}, '2026-09'))
        result = self._analyze([MATURED, HDFC_DEBT])
        high = result['highest_overlap']
        self.assertEqual((high['available'], high['percentage'], high['reason_code']), (False, None, 'HISTORICAL_HOLDINGS_UNAVAILABLE'))
        self.assertEqual(high['message'], 'Historical portfolio unavailable for IDFC FIXED TERM PLAN SERIES - 167 (1202 DAYS).')

    def test_one_fund_available_and_one_unavailable_are_reported_separately(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', debt_month({'INE296A07SV1': 5.0}, '2026-09'))
        result = self._analyze([MATURED, HDFC_DEBT])
        hdfc = self._fund(result, '128629')
        # HDFC's own September 2026 holdings are valid, whatever the other fund lacks.
        self.assertEqual((hdfc['available'], hdfc['status'], hdfc['portfolio_date'], hdfc['reason_code']), (True, 'HOLDINGS_AVAILABLE', '2026-09-30', None))
        self.assertEqual(result['pair_reasons'][0]['reason_code'], 'HISTORICAL_HOLDINGS_UNAVAILABLE')

    def test_a_fund_with_no_common_month_is_still_available_and_the_pair_says_so(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', debt_month({'INE296A07SV1': 5.0}, '2026-09'))
        store_snapshot(SBI_DEBT, '146215', '2026-08', debt_month({'INE296A07SV1': 4.0}, '2026-08', '31'))
        result = self._analyze([HDFC_DEBT, SBI_DEBT])
        self.assertEqual([fund['available'] for fund in result['funds']], [True, True])
        self.assertEqual([fund['portfolio_month'] for fund in result['funds']], ['2026-09', '2026-08'])
        gap = result['pair_reasons'][0]
        self.assertEqual((gap['reason_code'], gap['left_portfolio_month'], gap['right_portfolio_month']), ('NO_COMMON_REPORTING_MONTH', '2026-09', '2026-08'))
        self.assertEqual((result['pairs'], result['highest_overlap']['percentage'], result['highest_overlap']['reason_code']), ([], None, 'NO_COMMON_REPORTING_MONTH'))

    def test_a_genuine_zero_overlap_is_still_a_zero(self):
        store_snapshot(HDFC_DEBT, '128629', '2026-09', debt_month({'INE296A07SV1': 5.0}, '2026-09'))
        store_snapshot(SBI_DEBT, '146215', '2026-09', debt_month({'INE296A07XX5': 4.0}, '2026-09'))
        result = self._analyze([HDFC_DEBT, SBI_DEBT])
        self.assertEqual((result['highest_overlap']['percentage'], result['highest_overlap']['reason_code']), (0.0, 'NO_COMMON_SECURITIES'))
        self.assertEqual(result['pair_reasons'], [])


class HistoricalOverlapTests(PortfolioCacheMixin, SimpleTestCase):
    def _analyze(self, records, month, codes=None):
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=records),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', side_effect=AssertionError('the AMC site has no month this old')),
            patch('fund_analyzer.holdings._download_cached', side_effect=AssertionError('no download expected')),
        ):
            return analyze_holdings_overlap(codes or [record['scheme_code'] for record in records], month)

    def _store(self, record, code, weights, month, day='30'):
        store_snapshot(record, code, month, debt_month(weights, month, day))

    def test_same_month_historical_holdings_are_compared(self):
        self._store(HDFC_DEBT, '128629', {'INE296A07SV1': 5.0, 'INE476A16L21': 3.0}, '2022-04')
        self._store(SBI_DEBT, '146215', {'INE296A07SV1': 4.0, 'INE476A16L21': 6.0}, '2022-04')
        result = self._analyze([HDFC_DEBT, SBI_DEBT], '2022-04')
        # min(5, 4) + min(3, 6), both from April 2022.
        self.assertEqual(result['pairs'][0]['overlap_pct'], 7.0)
        self.assertEqual((result['mode'], result['requested_month'], result['portfolio_month']), ('historical', '2022-04', '2022-04'))
        self.assertEqual((result['pairs'][0]['portfolio_month'], result['pairs'][0]['portfolio_date']), ('2022-04', '2022-04-30'))

    def test_historical_mode_never_borrows_a_different_month(self):
        self._store(HDFC_DEBT, '128629', {'INE296A07SV1': 5.0}, '2022-04')
        self._store(SBI_DEBT, '146215', {'INE296A07SV1': 4.0}, '2022-04')
        # A newer portfolio exists for SBI, with a very different weight. It must not be used.
        self._store(SBI_DEBT, '146215', {'INE296A07SV1': 50.0}, '2026-09')
        self.assertEqual(self._analyze([HDFC_DEBT, SBI_DEBT], '2022-04')['pairs'][0]['overlap_pct'], 4.0)

    def test_no_common_historical_month_gives_no_figure(self):
        self._store(HDFC_DEBT, '128629', {'INE296A07SV1': 5.0}, '2022-04')
        self._store(SBI_DEBT, '146215', {'INE296A07SV1': 4.0}, '2022-03', '31')
        result = self._analyze([HDFC_DEBT, SBI_DEBT], '2022-04')
        self.assertEqual(result['pairs'], [])
        self.assertIsNone(result['highest_overlap']['percentage'])
        sbi = next(fund for fund in result['funds'] if fund['scheme_code'] == '146215')
        self.assertEqual((sbi['available'], sbi['reason_code']), (False, 'HISTORICAL_HOLDINGS_UNAVAILABLE'))
        self.assertEqual(result['highest_overlap']['reason_code'], 'HISTORICAL_HOLDINGS_UNAVAILABLE')

    def test_a_month_without_holdings_is_unavailable_not_zero(self):
        result = self._analyze([HDFC_DEBT, SBI_DEBT], '2022-04')
        self.assertEqual((result['highest_overlap']['available'], result['highest_overlap']['percentage']), (False, None))
        self.assertEqual(result['pair_reasons'][0]['reason_code'], 'HISTORICAL_HOLDINGS_UNAVAILABLE')

    def test_a_matured_scheme_can_use_its_own_validated_month(self):
        self._store(MATURED, '146082', {'INE296A07SV1': 6.0}, '2022-04')
        self._store(HDFC_DEBT, '128629', {'INE296A07SV1': 5.0}, '2022-04')
        result = self._analyze([MATURED, HDFC_DEBT], '2022-04')
        self.assertEqual(result['pairs'][0]['overlap_pct'], 5.0)
        self.assertEqual(next(fund for fund in result['funds'] if fund['scheme_code'] == '146082')['scheme_status'], 'MATURED')

    def test_a_month_after_the_scheme_stopped_reporting_is_refused(self):
        self._store(MATURED, '146082', {'INE296A07SV1': 6.0}, '2022-06')
        self._store(HDFC_DEBT, '128629', {'INE296A07SV1': 5.0}, '2022-06')
        result = self._analyze([MATURED, HDFC_DEBT], '2022-06')
        self.assertEqual(result['pairs'], [])
        self.assertEqual(next(fund for fund in result['funds'] if fund['scheme_code'] == '146082')['reason_code'], 'HISTORICAL_HOLDINGS_UNAVAILABLE')

    def test_without_a_chosen_month_a_matured_scheme_is_not_compared_with_current_holdings(self):
        self._store(MATURED, '146082', {'INE296A07SV1': 6.0}, '2022-04')
        self._store(HDFC_DEBT, '128629', {'INE296A07SV1': 5.0}, '2026-09')
        with (
            patch('fund_analyzer.holdings.fetch_amfi_latest', return_value=[MATURED, HDFC_DEBT]),
            patch('fund_analyzer.holdings._recent_portfolio_months', return_value=RECENT),
            patch('fund_analyzer.holdings._monthly_files_for_fund', return_value=[]),
        ):
            result = analyze_holdings_overlap(['146082', '128629'])
        self.assertEqual(result['pairs'], [])
        # The saved April 2022 portfolio is offered as a month the user can choose.
        self.assertEqual(next(fund for fund in result['funds'] if fund['scheme_code'] == '146082')['historical_months_available'], ['2022-04'])


class OverlapApiTests(SimpleTestCase):
    def test_portfolio_month_must_be_a_past_year_and_month(self):
        for bad in ('2022-13', '22-04', '2022-4', 'April 2022', '2099-01'):
            self.assertFalse(CompareRequestSerializer(data={'scheme_codes': ['1', '2'], 'portfolio_month': bad}).is_valid(), bad)
        self.assertTrue(CompareRequestSerializer(data={'scheme_codes': ['1', '2'], 'portfolio_month': '2022-04'}).is_valid())
        self.assertTrue(CompareRequestSerializer(data={'scheme_codes': ['1', '2']}).is_valid())

    def test_the_endpoint_passes_the_chosen_month_through(self):
        with patch('fund_analyzer.views.compare_holdings_overlap', return_value={'pairs': [], 'debug': []}) as compare:
            APIClient().post('/api/overlap', {'scheme_codes': ['146082', '128629'], 'portfolio_month': '2022-04'}, format='json')
            APIClient().post('/api/overlap', {'scheme_codes': ['146082', '128629']}, format='json')
        self.assertEqual([call.args for call in compare.call_args_list], [(['146082', '128629'], '2022-04'), (['146082', '128629'], None)])
