"""Fund search over AMFI's current NAVAll.txt, where plan and option are their own columns. No network."""
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from . import scheme_registry
from .providers import ProviderError, parse_amfi_text_data, scheme_display_name
from .scheme_registry import RegistryRejected, build_registry, get_registry, refresh_registry, registry_from_records, registry_status
from .services import search_funds

# Lines as AMFI publishes them (trimmed to a few AMCs).
NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Mid Cap Fund)

Edelweiss Mutual Fund

140228;INF843K01AO4;-;Edelweiss Mid Cap Fund;Direct Plan;Growth;124.956;06-Nov-2026
140227;INF843K01AP1;INF843K01AQ9;Edelweiss Mid Cap Fund;Direct Plan;IDCW;91.115;06-Nov-2026
140225;INF843K01013;-;Edelweiss Mid Cap Fund;Regular Plan;Growth;105.332;06-Nov-2026
140226;INF843K01021;INF843K01039;Edelweiss Mid Cap Fund;Regular Plan;IDCW;60.726;06-Nov-2026
148304;INF843K01ZZ9;-;Edelweiss Mid Cap Fund (Segregated Portfolio 1);Direct Plan;Growth;0.0000;06-Nov-2026
140175;INF843K01AB1;-;Edelweiss Large & Mid Cap Fund;Direct Plan;Growth;95.1;06-Nov-2026

Open Ended Schemes(Debt Scheme - Corporate Bond Fund)

PGIM India Mutual Fund

138330;INF223J01OO2;-;PGIM India Corporate Bond Fund;Direct Plan;Direct Growth;51.3083;06-Nov-2026
138317;INF223J01CY6;INF223J01CZ3;PGIM India Corporate Bond Fund;Regular Plan;Regular IDCW;10.3103;06-Nov-2026

Open Ended Schemes(Debt Scheme - Banking and PSU Fund)

Sundaram Mutual Fund

134370;INF903J012J4;INF903J013J2;Sundaram Banking and PSU Debt Fund;Direct Plan;IDCW (Income Distribution CUM Capital Withdrawal);11.4008;06-Nov-2026

Close Ended Schemes(Income)

ICICI Prudential Mutual Fund

146800;INF109KC1VR0;-;ICICI Prudential Fixed Maturity Plan - Series 85 - 10 Years Plan I;;;17.5415;06-Nov-2026
101010;INF109K01XX1;-;ICICI Prudential Midcap Fund - Direct Plan - Growth;Direct Plan;Growth;300.1;06-Nov-2026
"""
RECORDS = parse_amfi_text_data(NAVALL)
REGISTRY = registry_from_records(parse_amfi_text_data(NAVALL, include_unpriced=True))
LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'tests-search'}}


class AmfiPlanOptionColumnsTests(SimpleTestCase):
    def test_each_plan_and_option_keeps_its_own_code_name_and_isins(self):
        row = next(record for record in RECORDS if record['scheme_code'] == '140226')
        self.assertEqual((row['scheme_name'], row['plan'], row['option']), ('Edelweiss Mid Cap Fund', 'Regular Plan', 'IDCW'))
        self.assertEqual((row['isin'], row['isin_reinvest'], row['nav'], row['date']), ('INF843K01021', 'INF843K01039', 60.726, '06-Nov-2026'))
        self.assertEqual((row['amc'], row['category']), ('Edelweiss Mutual Fund', 'Equity Scheme - Mid Cap Fund'))

    def test_display_name_adds_plan_and_option_once(self):
        names = {record['scheme_code']: record['display_name'] for record in RECORDS}
        self.assertEqual(names['140225'], 'Edelweiss Mid Cap Fund - Regular Plan - Growth')
        self.assertEqual(names['146800'], 'ICICI Prudential Fixed Maturity Plan - Series 85 - 10 Years Plan I')  # blank plan and option
        self.assertEqual(names['101010'], 'ICICI Prudential Midcap Fund - Direct Plan - Growth')  # name already says both
        self.assertEqual(scheme_display_name({'scheme_name': 'X Fund', 'plan': None, 'option': 'Growth'}), 'X Fund - Growth')

    def test_analysis_parser_still_skips_unpriced_rows(self):
        self.assertNotIn('148304', [record['scheme_code'] for record in RECORDS])
        self.assertIn('148304', REGISTRY.by_code)


class FundSearchTests(SimpleTestCase):
    def search(self, query, **kwargs):
        with patch('fund_analyzer.services.get_registry', return_value=REGISTRY):
            return [row['scheme_code'] for row in search_funds(query, **kwargs)['results']]

    def test_regular_plan_search_that_used_to_return_nothing(self):
        for query in ('Edelweiss Mid Cap Fund - Regular Plan', 'Edelweiss Mid Cap Fund Regular Plan', 'regular plan mid cap edelweiss'):
            self.assertEqual(self.search(query)[:2], ['140225', '140226'], query)

    def test_direct_plan_search(self):
        self.assertEqual(self.search('Edelweiss Mid Cap Fund Direct Plan')[:2], ['140228', '140227'])

    def test_broad_search_lists_every_plan_and_option_with_direct_growth_first(self):
        self.assertEqual(self.search('Edelweiss Mid Cap Fund')[:4], ['140228', '140227', '140225', '140226'])

    def test_code_and_either_isin_return_exactly_that_scheme(self):
        self.assertEqual(self.search('140225'), ['140225'])
        self.assertEqual(self.search(' inf843k01013 '), ['140225'])
        self.assertEqual(self.search('INF843K01039'), ['140226'])

    def test_case_spacing_punctuation_and_idcw_spellings(self):
        self.assertEqual(self.search('EDELWEISS   mid-cap, fund  (regular) idcw'), ['140226'])
        self.assertEqual(self.search('edelweiss midcap regular dividend'), ['140226'])
        self.assertIn('134370', self.search('sundaram banking psu direct idcw'))
        self.assertIn('134370', self.search('withdrawal capital cum distribution income sundaram'))
        self.assertEqual(self.search('pgim corporate bond regular idcw'), ['138317'])
        self.assertIn('140175', self.search('edelweiss large and mid cap'))

    def test_filler_words_absent_from_amfi_text_do_not_hide_a_scheme(self):
        self.assertIn('146800', self.search('ICICI fixed maturity series 85 option'))

    def test_pages_cover_every_match_in_rank_order_and_total_counts_all(self):
        with patch('fund_analyzer.services.get_registry', return_value=REGISTRY):
            everything = search_funds('edelweiss', limit=100)
            pages = [search_funds('edelweiss', limit=2, offset=offset) for offset in (0, 2, 4)]
        self.assertEqual(everything['total'], 6)
        self.assertEqual([row['scheme_code'] for page in pages for row in page['results']], [row['scheme_code'] for row in everything['results']])
        self.assertEqual([row['scheme_code'] for row in pages[0]['results']], ['140228', '140227'])
        self.assertTrue(all(page['total'] == 6 for page in pages))

    def test_scheme_with_zero_nav_is_still_searchable(self):
        self.assertEqual(self.search('148304'), ['148304'])
        self.assertIn('148304', self.search('edelweiss segregated'))
        with patch('fund_analyzer.services.get_registry', return_value=REGISTRY):
            self.assertIsNone(search_funds('148304')['results'][0]['nav'])

    def test_inactive_scheme_is_searchable_and_ranked_after_live_ones(self):
        old = dict(REGISTRY.by_code['140225'], scheme_code='999001', date='01-Jan-2019')
        registry = registry_from_records([*REGISTRY.records, old])
        with patch('fund_analyzer.services.get_registry', return_value=registry):
            rows = search_funds('Edelweiss Mid Cap Fund Regular')['results']
        self.assertEqual([row['scheme_code'] for row in rows][:3], ['140225', '140226', '999001'])
        self.assertNotEqual(rows[2]['scheme_status'], 'ACTIVE')

    def test_api_returns_display_name_plan_option_and_total(self):
        with patch('fund_analyzer.services.get_registry', return_value=REGISTRY):
            data = APIClient().get('/api/search', {'q': 'Edelweiss Mid Cap Fund - Regular Plan'}).json()
        self.assertEqual(data['total'], 2)
        self.assertEqual([(row['scheme_code'], row['display_name'], row['plan'], row['option']) for row in data['results']], [
            ('140225', 'Edelweiss Mid Cap Fund - Regular Plan - Growth', 'Regular Plan', 'Growth'),
            ('140226', 'Edelweiss Mid Cap Fund - Regular Plan - IDCW', 'Regular Plan', 'IDCW'),
        ])

    def test_api_pages_with_offset_and_rejects_bad_numbers(self):
        with patch('fund_analyzer.services.get_registry', return_value=REGISTRY):
            client = APIClient()
            second = client.get('/api/search', {'q': 'edelweiss', 'limit': 2, 'offset': 2}).json()
            bad = client.get('/api/search', {'q': 'edelweiss', 'offset': 'x'})
        self.assertEqual((second['total'], second['offset'], second['limit'], len(second['results'])), (6, 2, 2, 2))
        self.assertEqual(bad.status_code, 400)

    def test_records_without_display_name_get_one(self):
        old = [{key: value for key, value in record.items() if key != 'display_name'} for record in RECORDS]
        self.assertEqual(registry_from_records(old).by_code['140226']['display_name'], 'Edelweiss Mid Cap Fund - Regular Plan - IDCW')


def _feed(extra_rows: int = 0, drop: int = 0) -> str:
    """NAVALL plus synthetic AMCs, so the registry's size checks have something to measure."""
    lines = NAVALL.splitlines()
    for amc in range(12):
        lines += ['', f'Test AMC {amc} Mutual Fund', '']
        lines += [f'{900000 + amc * 100 + n};INF000T{amc:02d}{n:03d};-;Test Fund {amc}-{n};Direct Plan;Growth;10.5;06-Nov-2026' for n in range(10 + extra_rows)]
    return '\n'.join(lines[:len(lines) - drop])


def _make_stale(hours: float) -> None:
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec='seconds')
    cache.set(scheme_registry.META_KEY, {**cache.get(scheme_registry.META_KEY), 'fetched_at': old}, None)
    scheme_registry._memory.meta['fetched_at'] = old


@override_settings(CACHES=LOCMEM, FUND_ANALYZER_AMFI_REGISTRY_MIN_SCHEMES=50, FUND_ANALYZER_AMFI_REGISTRY_MAX_DROP_PCT=10,
                   FUND_ANALYZER_HOLDINGS_CACHE_DIR=tempfile.mkdtemp(prefix='mns-test-registry-'))
class SchemeRegistryTests(TestCase):
    def setUp(self):
        cache.clear()
        scheme_registry.reset_memory()
        scheme_registry.registry_path().unlink(missing_ok=True)

    def tearDown(self):
        scheme_registry.reset_memory()

    def amfi(self, text=None, error=None):
        return patch('fund_analyzer.scheme_registry.fetch_url', side_effect=error, return_value=MagicMock(text=text))

    def test_every_scheme_code_in_the_feed_is_kept_with_its_fields(self):
        payload = build_registry(_feed())
        codes = {line.split(';')[0] for line in _feed().splitlines() if line.split(';')[0].isdigit()}
        self.assertEqual({record['scheme_code'] for record in payload['records']}, codes)
        self.assertEqual((payload['count'], payload['unpriced_count'], payload['duplicate_codes']), (len(codes), 1, 0))
        row = next(record for record in payload['records'] if record['scheme_code'] == '140227')
        self.assertEqual((row['isin_payout_growth'], row['isin_reinvest'], row['plan'], row['option']), ('INF843K01AP1', 'INF843K01AQ9', 'Direct Plan', 'IDCW'))

    def test_small_or_shrunken_feeds_are_rejected(self):
        with self.assertRaises(RegistryRejected):
            build_registry(NAVALL)  # too few schemes and AMCs
        full = build_registry(_feed())['count']
        with self.assertRaisesRegex(RegistryRejected, 'fell from'):
            build_registry(_feed(drop=40), previous_count=full)
        self.assertTrue(build_registry(_feed(drop=40), previous_count=full, force=True)['forced'])

    def test_first_search_loads_amfi_once_then_uses_the_registry(self):
        with self.amfi(_feed()) as fetch:
            for query in ('edelweiss', 'edelweiss mid', 'edelweiss mid cap'):
                search_funds(query)
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(scheme_registry.registry_path().exists())

    def test_a_failed_or_rejected_refresh_keeps_the_last_good_registry(self):
        with self.amfi(_feed()):
            refresh_registry()
        with self.amfi(error=ProviderError('AMFI down')):
            self.assertEqual(refresh_registry()['status'], 'failed')
        with self.amfi(NAVALL):
            self.assertEqual(refresh_registry()['status'], 'rejected')
        self.assertEqual(search_funds('140225')['results'][0]['scheme_code'], '140225')
        status = registry_status()
        self.assertEqual(status['schemeCount'], build_registry(_feed())['count'])
        self.assertIn('rejected', status['lastRefreshError']['error'])

    def test_stale_registry_refreshes_once_and_new_schemes_appear(self):
        with self.amfi(_feed()):
            refresh_registry()
        self.assertEqual(search_funds('900012')['total'], 0)
        _make_stale(13)
        with self.amfi(_feed(extra_rows=3)) as fetch:
            self.assertEqual(search_funds('900012')['total'], 1)
            search_funds('900012')
        self.assertEqual(fetch.call_count, 1)
        self.assertIsNone(registry_status()['lastRefreshError'])

    def test_stale_registry_with_amfi_down_still_searches_and_backs_off(self):
        with self.amfi(_feed()):
            refresh_registry()
        _make_stale(72)
        with self.amfi(error=ProviderError('AMFI down')) as fetch:
            for _ in range(3):
                self.assertEqual([row['scheme_code'] for row in search_funds('140226')['results']], ['140226'])
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(registry_status()['stale'])

    def test_restart_with_empty_cache_and_amfi_down_uses_the_file(self):
        with self.amfi(_feed()):
            refresh_registry()
        cache.clear()
        scheme_registry.reset_memory()
        with self.amfi(error=ProviderError('AMFI down')) as fetch:
            self.assertEqual(search_funds('140228')['total'], 1)
        fetch.assert_not_called()

    def test_corrupt_file_and_amfi_down_is_an_error_not_an_empty_list(self):
        path = scheme_registry.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": 1, "records": [', encoding='utf-8')
        with self.amfi(error=ProviderError('AMFI down')), self.assertRaises(ProviderError):
            get_registry()

    def test_status_endpoint_is_public_and_refresh_is_admin_only(self):
        with self.amfi(_feed()):
            refresh_registry()
        client = APIClient()
        self.assertEqual(client.get('/api/search/registry').json()['schemeCount'], build_registry(_feed())['count'])
        self.assertIn(client.post('/api/search/registry/refresh').status_code, (401, 403))
        admin = get_user_model().objects.create_user(email='admin@example.com', password='x-123456789', is_staff=True)
        client.force_authenticate(admin)
        with self.amfi(_feed(extra_rows=1)):
            self.assertEqual(client.post('/api/search/registry/refresh').status_code, 200)
        with self.amfi(NAVALL):
            self.assertEqual(client.post('/api/search/registry/refresh').status_code, 502)

    def test_management_command(self):
        out = StringIO()
        with self.amfi(_feed()):
            call_command('refresh_amfi_registry', stdout=out)
        self.assertIn('refreshed', out.getvalue())
        with self.amfi(NAVALL), self.assertRaises(CommandError):
            call_command('refresh_amfi_registry', stdout=StringIO())


class UnpricedSchemeAnalysisTests(SimpleTestCase):
    """A scheme found through search must not break Compare when its history holds zero NAVs."""

    def test_zero_nav_history_gets_a_reason_not_a_crash(self):
        history = [{'scheme_code': '148304', 'date': f'2026-0{month}-01', 'nav': 0.0} for month in range(1, 9)]
        with patch('fund_analyzer.services.fetch_amfi_history', return_value=history):
            from .services import fetch_fund_data
            result = fetch_fund_data('148304', include_metadata=False, latest_records=[{**REGISTRY.by_code['148304'], 'nav': 0.0}])
        self.assertIsNone(result['metrics'])
        self.assertIn('zero NAV', result['metrics_unavailable'])

    def test_valid_history_metrics_are_unchanged(self):
        from .analytics import metrics
        from .services import _metrics_or_reason
        history = [{'date': f'2026-{month:02d}-01', 'nav': 10 + month} for month in range(1, 13)]
        self.assertEqual(_metrics_or_reason(history, 0.065, '1'), (metrics(history, 0.065), None))
        self.assertEqual(_metrics_or_reason([], 0.065, '1'), (None, None))
