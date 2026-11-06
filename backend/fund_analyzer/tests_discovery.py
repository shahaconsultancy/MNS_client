"""Tests for finding official portfolio files on AMC pages. These use no database."""
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase

from . import adapters, holdings
from .adapters import SCHEME_NOT_FOUND
from .holdings import _embedded_excel_paths, _fund_reason_code, _load_page_links

PAGE = '''<html><body>
<a href="/uploads/Plain_Link_Portfolio_March_31_2026_aa.xlsx">Download</a>
<script>window.__DATA__ = {"files":[
 {"url":"\/uploads\/ABK_MF_PORTFOLIO_March_31_2026_e8df14e50e.xlsx"},
 {"url":"/uploads/Abakkus_MF_Portfolio_31_01_2026_9cd68f430c.xls"},
 {"url":"/uploads/Fortnightly_Portfolio_15_01_2026_bb.xlsx"},
 {"url":"/uploads/Scheme_Dashboard_Apr_2026_cc.xlsx"},
 {"url":"/uploads/Monthly_Portfolio_April_2026.pdf"},
 {"url":"https://elsewhere.example/uploads/ABK_MF_PORTFOLIO_April_30_2026.xlsx"}
]};</script></body></html>'''


class EmbeddedFileTests(SimpleTestCase):
    def test_excel_paths_are_read_from_page_data_including_escaped_slashes(self):
        paths = _embedded_excel_paths(PAGE)
        self.assertIn('/uploads/ABK_MF_PORTFOLIO_March_31_2026_e8df14e50e.xlsx', paths)
        self.assertIn('/uploads/Abakkus_MF_Portfolio_31_01_2026_9cd68f430c.xls', paths)
        self.assertNotIn('/uploads/Monthly_Portfolio_April_2026.pdf', paths)
        self.assertEqual(len(paths), len(set(paths)))


class PageDiscoveryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _links(self):
        response = MagicMock(text=PAGE)
        with (
            patch.object(holdings, 'fetch_url', return_value=response),
            patch.dict(adapters._official_domains, {'x-mutual-fund': ['x.example']}),
        ):
            return _load_page_links('x-mutual-fund', 'https://www.x.example/statutory-disclosures', 'directory_monthly_excel')

    def test_monthly_files_in_page_data_are_found_with_their_months(self):
        found = {link['url'].rsplit('/', 1)[-1]: link['month'] for link in self._links()}
        self.assertEqual(found['ABK_MF_PORTFOLIO_March_31_2026_e8df14e50e.xlsx'], '2026-03')
        self.assertEqual(found['Abakkus_MF_Portfolio_31_01_2026_9cd68f430c.xls'], '2026-01')
        self.assertEqual(found['Plain_Link_Portfolio_March_31_2026_aa.xlsx'], '2026-03')

    def test_the_usual_filters_still_apply_to_files_in_page_data(self):
        urls = [link['url'] for link in self._links()]
        self.assertFalse([url for url in urls if 'Fortnightly' in url or 'Dashboard' in url])
        self.assertFalse([url for url in urls if not url.startswith('https://www.x.example/')])


class SchemeNotInFilesTests(SimpleTestCase):
    def test_a_scheme_missing_from_the_published_files_has_its_own_reason(self):
        self.assertEqual(_fund_reason_code({'available': False, 'status': SCHEME_NOT_FOUND}), 'SCHEME_NOT_IN_OFFICIAL_FILES')
