"""How each AMC publishes its monthly portfolio files.

An adapter only finds candidate files. It never decides a file is correct: every
workbook is still checked for the scheme name, reporting month, ISINs and weights.
Add an AMC by adding an adapter, without changing the overlap formula or the frontend.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

from .providers import ProviderError, fetch_amfi_disclosure_directory

# Structured retrieval statuses, used in logs and the internal coverage report.
SOURCE_NOT_SUPPORTED = 'SOURCE_NOT_SUPPORTED'
DISCLOSURE_NOT_PUBLISHED = 'DISCLOSURE_NOT_PUBLISHED'
DOWNLOAD_BLOCKED = 'DOWNLOAD_BLOCKED'
DOWNLOAD_FAILED = 'DOWNLOAD_FAILED'
INVALID_WORKBOOK = 'INVALID_WORKBOOK'
SCHEME_NOT_FOUND = 'SCHEME_NOT_FOUND'
PARSING_FAILED = 'PARSING_FAILED'
NO_COMMON_MONTH = 'NO_COMMON_MONTH'
HOLDINGS_AVAILABLE = 'HOLDINGS_AVAILABLE'

HDFC_PUBLIC_FILE_ROOT = 'https://files.hdfcfund.com/s3fs-public/'
PPFAS_SCHEME_FILE_PREFIXES = {
    'parag parikh flexi cap fund': 'PPFCF_',
    'parag parikh large cap fund': 'PPLCF_',
    'parag parikh elss tax saver fund': 'PPTSF_',
    'parag parikh conservative hybrid fund': 'PPCHF_',
    'parag parikh dynamic asset allocation fund': 'PPDAAF_',
    'parag parikh arbitrage fund': 'PPAF_',
    'parag parikh liquid fund': 'PPLF_',
}
MAX_DIRECTORY_LINKS_PER_MONTH = 2
_SECOND_LEVEL_SUFFIXES = {'co.in', 'net.in', 'org.in', 'firm.in', 'gen.in', 'ind.in', 'co.uk'}
_NOT_A_MONTHLY_PORTFOLIO = re.compile(
    r'(?i)fortnight|half[\s_-]*year|\bhy\b|_hy_|dashboard|overlap|top[\s_-]*10|proxy|voting|\baum\b|associate'
    r'|risk[\s_-]*o|riskometer|financial|annual|factsheet|bank[\s_-]*list|debt[\s_-]*portfolio|performance',
)


class PortfolioError(ProviderError):
    """A retrieval failure with a structured status."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def classify_error(error: Exception | str) -> str:
    """Turn any retrieval or parsing failure into one structured status."""
    status = getattr(error, 'status', None)
    if status:
        return status
    text = str(error).lower()
    if re.search(r'\b(401|403|407|429|451)\b|forbidden|too many requests', text):
        return DOWNLOAD_BLOCKED
    if re.search(r'\b(404|410)\b|not found for url|no monthly portfolio|no monthly files', text):
        return DISCLOSURE_NOT_PUBLISHED
    if re.search(r'no sheet matched|no holdings section', text):
        return SCHEME_NOT_FOUND
    if re.search(r'could not be parsed|not a zip|badzip|workbook contains no sheets|unsupported format|xlrd|corrupt', text):
        return INVALID_WORKBOOK
    if re.search(r'timed out|timeout|connection|\b5\d\d\b|empty monthly|listing unavailable', text):
        return DOWNLOAD_FAILED
    return PARSING_FAILED


def normalise_name(value: str) -> str:
    # AMFI and AMC files write the same scheme with both "&" and "and".
    text = re.sub(r'\b(direct|regular|growth|option|plan|idcw|reinvestment|reinvest|dividend|and)\b', ' ', value, flags=re.I)
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def scheme_link_matches(text: str, fund_name: str) -> bool:
    wanted = normalise_name(fund_name)
    candidate = normalise_name(unquote(text))
    return bool(wanted and (wanted in candidate or candidate in wanted))


def month_in_text(value: str) -> str | None:
    """Find the reporting month in a link's label or file name.

    A full date is read first, so 'October 31, 2026' is never read as October 2031.
    """
    text = re.sub(r'[-_.,/]+', ' ', value)
    text = re.sub(r'(?i)(\d)(st|nd|rd|th)\b', r'\1', text)
    text = re.sub(r'(?i)\bsept\b', 'Sep', text)
    latest_year = date.today().year + 1
    numeric = re.search(r'\b(\d{1,2})\s+(\d{1,2})\s+(20\d{2})\b', text)
    if numeric and 1 <= int(numeric.group(2)) <= 12 and int(numeric.group(1)) <= 31:
        return f'{numeric.group(3)}-{int(numeric.group(2)):02d}'
    searches = (
        (r'\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b', ('%d %B %Y', '%d %b %Y')),
        (r'\b[A-Za-z]{3,9}\s+\d{1,2}\s+\d{4}\b', ('%B %d %Y', '%b %d %Y')),
        (r'\b[A-Za-z]{3,9}\s+\d{4}\b', ('%B %Y', '%b %Y')),
        (r'\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2}\b', ('%d %B %y', '%d %b %y')),
        (r'\b[A-Za-z]{3,9}\s+\d{1,2}\s+\d{2}\b', ('%B %d %y', '%b %d %y')),
        (r'\b[A-Za-z]{3,9}\s+\d{2}\b', ('%B %y', '%b %y')),
    )
    for pattern, formats in searches:
        for candidate in re.findall(pattern, text):
            for date_format in formats:
                try:
                    parsed = datetime.strptime(candidate, date_format)
                except ValueError:
                    continue
                if 2000 <= parsed.year <= latest_year:
                    return parsed.strftime('%Y-%m')
    return None


def portfolio_date(month: str) -> str:
    first = date.fromisoformat(f'{month}-01')
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (next_month - timedelta(days=1)).isoformat()


def registered_domain(host: str) -> str:
    parts = host.lower().split(':')[0].strip('.').split('.')
    if len(parts) >= 3 and '.'.join(parts[-2:]) in _SECOND_LEVEL_SUFFIXES:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def is_official_url(url: str, domains: list[str] | tuple[str, ...]) -> bool:
    """Only follow web links on the AMC's own domain. Users can never supply these."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if parts.scheme not in ('http', 'https') or not host:
        return False
    return any(host == domain or host.endswith(f'.{domain}') for domain in domains)


class AmcAdapter:
    """Discovery rules for one AMC's published portfolio files."""

    parser = ''

    def __init__(self, source: dict[str, Any]):
        self.source = source

    def matches_link(self, combined: str) -> bool:
        return 'monthly portfolio' in combined or 'monthly-portfolio' in combined

    def is_candidate(self, text: str, href: str) -> bool:
        combined = f'{text} {href}'.lower()
        if not urlsplit(href).path.lower().endswith(('.xls', '.xlsx')):
            return False
        if 'fortnight' in combined or 'half-year' in combined or 'half yearly' in combined:
            return False
        return self.matches_link(combined)

    def select_links(self, links: list[dict[str, str]], fund_name: str) -> list[dict[str, str]]:
        """Choose the files that could hold this scheme, from everything on the page."""
        return links

    def fallback_links(self, fund_name: str, months: list[str], error: ProviderError) -> list[dict[str, str]]:
        """Candidates to try when the listing page cannot be read. Most AMCs have none."""
        raise error


class HdfcAdapter(AmcAdapter):
    """HDFC rejects server requests for its listing, but its public file host allows them."""

    parser = 'hdfc_xlsx_by_scheme'

    def matches_link(self, combined: str) -> bool:
        return 'monthly' in combined

    def select_links(self, links, fund_name):
        return [link for link in links if scheme_link_matches(f"{link['text']} {urlsplit(link['url']).path}", fund_name)]

    def fallback_links(self, fund_name, months, error):
        """HDFC's published pattern. One or two candidates a month, each checked by content."""
        name = re.split(r'(?i)\s+-\s+(?:direct|regular|growth|idcw)', fund_name)[0].strip()
        if not name:
            raise error
        names = [name]
        if name.isupper():
            names.append(re.sub(r'(?i)^hdfc\b', 'HDFC', name.title()))
        elif '&' in name:
            names.append(name.replace('&', 'and'))
        links = []
        for month in sorted(months, reverse=True):
            reporting_date = date.fromisoformat(portfolio_date(month))
            folder = (reporting_date + timedelta(days=1)).strftime('%Y-%m')
            for display_name in names:
                filename = f"Monthly {display_name} - {reporting_date.day} {reporting_date.strftime('%B %Y')}.xlsx"
                links.append({
                    'url': urljoin(HDFC_PUBLIC_FILE_ROOT, f'{folder}/{quote(filename)}'),
                    'text': filename, 'month': month, 'listing_error': str(error), 'guessed': True,
                })
        return links


class PpfasAdapter(AmcAdapter):
    parser = 'ppfas_consolidated_excel'

    def matches_link(self, combined: str) -> bool:
        return 'consolidated' in combined or 'ppfas_monthly_portfolio_report' in combined

    def select_links(self, links, fund_name):
        prefix = PPFAS_SCHEME_FILE_PREFIXES.get(normalise_name(fund_name))
        if prefix:
            scheme_links = [link for link in links if urlsplit(link['url']).path.rsplit('/', 1)[-1].upper().startswith(prefix)]
            if scheme_links:
                return scheme_links
        return [link for link in links if link['text'].lower() == 'consolidated']


class NipponAdapter(AmcAdapter):
    parser = 'nippon_monthly_excel'


class SbiAdapter(AmcAdapter):
    parser = 'sbi_monthly_excel'

    def select_links(self, links, fund_name):
        selected = [link for link in links if scheme_link_matches(f"{link['text']} {urlsplit(link['url']).path}", fund_name)]
        return selected or [link for link in links if 'all schemes' in link['text'].lower()]


class DirectoryAdapter(AmcAdapter):
    """Any AMC whose page, as listed in AMFI's directory, publishes plain Excel links."""

    parser = 'directory_monthly_excel'

    def matches_link(self, combined: str) -> bool:
        return 'portfolio' in combined or 'all funds' in combined or 'all schemes' in combined

    def is_candidate(self, text: str, href: str) -> bool:
        # Look at the label and file name only. A folder can be called anything.
        file_name = unquote(urlsplit(href).path.rsplit('/', 1)[-1])
        return not _NOT_A_MONTHLY_PORTFOLIO.search(f'{text} {file_name}') and super().is_candidate(text, href)

    def select_links(self, links, fund_name):
        by_month: dict[str, list[dict[str, str]]] = {}
        for link in links:
            by_month.setdefault(link['month'], []).append(link)
        selected = []
        for month_links in by_month.values():
            own = [link for link in month_links if scheme_link_matches(f"{link['text']} {urlsplit(link['url']).path}", fund_name)]
            # A file that names no fund, or says it covers them all, may hold every scheme.
            shared = [
                link for link in month_links
                if link not in own and re.search(r'(?i)\ball\b|consolidated|combined|^(?!.*fund).*$', unquote(f"{link['text']} {urlsplit(link['url']).path}"))
            ]
            selected += (own or shared)[:MAX_DIRECTORY_LINKS_PER_MONTH]
        return selected


_ADAPTERS = {adapter.parser: adapter for adapter in (HdfcAdapter, PpfasAdapter, NipponAdapter, SbiAdapter, DirectoryAdapter)}
AMC_SOURCES = {
    'HDFC Mutual Fund': {
        'key': 'hdfc',
        'url': 'https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio',
        'parser': 'hdfc_xlsx_by_scheme',
        'domains': ['hdfcfund.com'],
    },
    'PPFAS Mutual Fund': {
        'key': 'ppfas',
        'url': 'https://amc.ppfas.com/downloads/portfolio-disclosure/',
        'parser': 'ppfas_consolidated_excel',
        'domains': ['ppfas.com'],
    },
    'Nippon India Mutual Fund': {
        'key': 'nippon',
        'url': 'https://mf.nipponindiaim.com/investor-service/downloads/factsheet-portfolio-and-other-disclosures/',
        'parser': 'nippon_monthly_excel',
        'domains': ['nipponindiaim.com'],
    },
    'SBI Mutual Fund': {
        'key': 'sbi',
        'url': 'https://www.sbimf.com/portfolios',
        'parser': 'sbi_monthly_excel',
        'domains': ['sbimf.com'],
    },
}
_official_domains: dict[str, list[str]] = {source['key']: source['domains'] for source in AMC_SOURCES.values()}


def adapter_for(source: dict[str, Any]) -> AmcAdapter:
    return _ADAPTERS[source['parser']](source)


def candidate_link(parser_key: str, text: str, href: str) -> bool:
    adapter = _ADAPTERS.get(parser_key)
    return bool(adapter and adapter({}).is_candidate(text, href))


def official_domains(amc_key: str) -> list[str]:
    return _official_domains.get(amc_key, [])


def source_for_amc(amc_name: str, *, use_directory: bool = True) -> dict[str, Any] | None:
    """The AMC's source: a dedicated adapter, or the page AMFI's directory lists for it."""
    source = AMC_SOURCES.get(amc_name)
    if source or not use_directory:
        return source
    try:
        url = fetch_amfi_disclosure_directory().get(amc_name)
    except ProviderError:
        return None
    if not url or not urlsplit(url).hostname:
        return None
    key = re.sub(r'[^a-z0-9]+', '-', amc_name.lower()).strip('-')
    domains = [registered_domain(urlsplit(url).hostname)]
    _official_domains[key] = domains
    return {'key': key, 'url': url, 'parser': DirectoryAdapter.parser, 'domains': domains}
