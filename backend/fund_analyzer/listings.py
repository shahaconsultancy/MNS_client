"""File listings for AMC sites whose portfolio page fills its list with a public request.

Each reader asks the same public endpoint the AMC's own page asks, with the parameters the page
itself sends, and returns candidate links. It never signs in, solves a challenge or forges a
session: if the endpoint refuses, that is recorded as ACCESS_BLOCKED and the source is left alone.
Every link must still be on the AMC's official domain, and every file is still checked by content.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

import requests

from .adapters import is_official_url, month_in_text, official_domains, portfolio_date
from .providers import HEADERS, SourceFailure
from .source_health import (
    ACCESS_BLOCKED,
    DISCOVERY_FAILED,
    RATE_LIMITED,
    SOURCE_TEMPORARILY_UNAVAILABLE,
    STAGE_LISTING,
    backoff_with_jitter,
    category_for_exception,
    category_for_http,
    evidence_from_response,
    looks_blocked,
    retry_after_seconds,
    throttle,
)

logger = logging.getLogger(__name__)

LISTING_TIMEOUT = (5, 20)
LISTING_ATTEMPTS = 2
# A Retry-After longer than this is not waited out inside a request; the circuit opens instead.
MAX_INLINE_RETRY_AFTER = 5.0


def source_request(method: str, url: str, *, amc_key: str, stage: str = STAGE_LISTING, guessed: bool = False, **kwargs) -> requests.Response:
    """One bounded request to an AMC's official host, with its failure categorised and evidenced."""
    domains = official_domains(amc_key)
    if domains and not is_official_url(url, domains):
        raise SourceFailure(f'{url} is not on an official domain of {amc_key}', category=DISCOVERY_FAILED, stage=stage)
    headers = {**HEADERS, **(kwargs.pop('headers', None) or {})}
    last: SourceFailure | None = None
    for attempt in range(LISTING_ATTEMPTS):
        throttle(urlsplit(url).hostname or '')
        started = time.perf_counter()
        try:
            response = requests.request(method, url, headers=headers, timeout=kwargs.pop('timeout', LISTING_TIMEOUT), **kwargs)
        except requests.RequestException as exc:
            category = category_for_exception(exc, stage=stage)
            last = SourceFailure(f'{exc.__class__.__name__}: {exc}', category=category, stage=stage, evidence={'url': url.split('?')[0], 'error': exc.__class__.__name__, 'seconds': round(time.perf_counter() - started, 2)})
        else:
            evidence = evidence_from_response(response, url, started)
            if domains and response.url and not is_official_url(response.url, domains):
                raise SourceFailure(f'{url} redirected off the official domain', category=DISCOVERY_FAILED, stage=stage, evidence=evidence)
            body = response.text[:20000] if 'html' in evidence['content_type'] or 'text' in evidence['content_type'] else ''
            if response.ok and not looks_blocked(body):
                return response
            category = category_for_http(response.status_code, stage=stage, body=body, guessed=guessed)
            last = SourceFailure(f'{response.status_code} {response.reason or ""} for url: {url}'.strip(), category=category, stage=stage, evidence={**evidence, 'blocked_page': looks_blocked(body)})
            if category == RATE_LIMITED:
                wait = retry_after_seconds(evidence.get('retry_after'))
                if wait is None or wait > MAX_INLINE_RETRY_AFTER or attempt + 1 >= LISTING_ATTEMPTS:
                    raise last
                time.sleep(wait)
                continue
        # Only a temporary failure is worth a second try. A block or a missing page is not.
        if last.category != SOURCE_TEMPORARILY_UNAVAILABLE or attempt + 1 >= LISTING_ATTEMPTS:
            raise last
        time.sleep(backoff_with_jitter(attempt))
    raise last  # pragma: no cover - the loop always returns or raises


def recent_months(count: int) -> list[date]:
    """Month ends, newest first, for the completed months in the lookback."""
    months, cursor = [], date.today().replace(day=1) - timedelta(days=1)
    for _ in range(count):
        months.append(cursor)
        cursor = cursor.replace(day=1) - timedelta(days=1)
    return months


def _link(url: str, text: str, month: str) -> dict[str, str]:
    return {'url': url, 'text': text, 'month': month, 'portfolio_date': portfolio_date(month)}


def wordpress_ajax_links(source: dict[str, Any], lookback: int) -> list[dict[str, str]]:
    """A WordPress page whose download sections load through admin-ajax with the nonce the page publishes."""
    config, key = source.get('listing') or {}, source['key']
    page = source_request('GET', source['url'], amc_key=key)
    match = re.search(r'\b' + re.escape(config['config_object']) + r'\s*=\s*(\{[^;]*?\})\s*;', page.text)
    if not match:
        raise SourceFailure(f"The page no longer publishes {config['config_object']}", category=DISCOVERY_FAILED, stage=STAGE_LISTING, status='SOURCE_NOT_SUPPORTED')
    try:
        settings = json.loads(match.group(1))
        ajax_url, nonce = settings['ajaxUrl'], settings['nonce']
    except (ValueError, KeyError) as exc:
        raise SourceFailure('The page listing settings could not be read', category=DISCOVERY_FAILED, stage=STAGE_LISTING, status='SOURCE_NOT_SUPPORTED') from exc
    section_id = config.get('section_id')
    title = config.get('section_title')
    if title:
        # Prefer the section by its visible title, so a renumbered section is still found.
        found = re.search(r'data-section-id="(\d+)"[^>]*>(?:(?!data-section-id).){0,1500}?bd-accordion-title">\s*' + re.escape(title) + r'\s*<', page.text, re.S)
        section_id = found.group(1) if found else section_id
    if not section_id:
        raise SourceFailure('The monthly portfolio section was not found on the page', category=DISCOVERY_FAILED, stage=STAGE_LISTING, status='SOURCE_NOT_SUPPORTED')

    def post(**data) -> Any:
        response = source_request('POST', ajax_url, amc_key=key, data={'nonce': nonce, **data})
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceFailure('The listing did not answer with JSON', category=DISCOVERY_FAILED, stage=STAGE_LISTING) from exc
        if not payload.get('success'):
            raise SourceFailure('The listing refused the request', category=ACCESS_BLOCKED if response.status_code == 403 else DISCOVERY_FAILED, stage=STAGE_LISTING)
        return payload.get('data') or {}

    links: list[dict[str, str]] = []
    april_start = config.get('financial_year') == 'april'
    for month_end in recent_months(lookback):
        start_year = month_end.year if not april_start or month_end.month >= 4 else month_end.year - 1
        year_label = f'{start_year}-{str(start_year + 1)[-2:]}' if april_start else str(month_end.year)
        data = post(action=config['list_action'], section_id=section_id, year=year_label, month=month_end.strftime('%B'))
        rows = re.findall(r'bd-download-title">\s*([^<]*?)\s*</span>.*?href="([^"]+)"', data.get('html') or '', re.S)
        for text, href in rows:
            month = month_in_text(f'{text} {href}')
            if month == month_end.strftime('%Y-%m'):
                links.append(_link(urljoin(ajax_url, href), text, month))
    return links


def json_downloads_links(source: dict[str, Any], lookback: int) -> list[dict[str, str]]:
    """A JSON downloads API: resolve the monthly-portfolio category by code, then page through its files."""
    config, key = source.get('listing') or {}, source['key']
    headers = {'Accept': 'application/json', **(config.get('headers') or {})}
    categories = source_request('GET', config['categories_url'], amc_key=key, headers=headers).json()
    category_id = _find_category(categories, config['category_code'])
    if not category_id:
        raise SourceFailure(f"Category {config['category_code']} is no longer listed", category=DISCOVERY_FAILED, stage=STAGE_LISTING, status='SOURCE_NOT_SUPPORTED')
    wanted = {month_end.strftime('%Y-%m') for month_end in recent_months(lookback)}
    links: list[dict[str, str]] = []
    for page_number in range(1, 4):
        body = {'categoryId': category_id, 'schemeCategory': '', 'userType': 'Investor', 'fileType': 'All', 'page': str(page_number), 'size': str(config.get('page_size') or 20)}
        payload = source_request('POST', config['files_url'], amc_key=key, headers={**headers, 'Content-Type': 'application/json'}, json=body).json()
        files = ((payload.get('success') or {}).get('data') or {}).get('files') or []
        for item in files:
            text = ((item.get('title') or {}).get('text') or '').strip()
            path = str(item.get('url') or '')
            month = month_in_text(text) or month_in_text(path)
            if not path or month not in wanted or not item.get('isEnabled', True):
                continue
            url = path if path.startswith('http') else config['file_base'].rstrip('/') + quote(path if path.startswith('/') else f'/{path}')
            links.append(_link(url, text, month))
        oldest = min((month_in_text(((item.get('title') or {}).get('text') or '')) or '9999' for item in files), default='')
        # Newest first: a short page is the last one, and a page reaching past the lookback is enough.
        if len(files) < int(config.get('page_size') or 20) or oldest < min(wanted):
            break
    return links


def _find_category(node: Any, code: str) -> str | None:
    if isinstance(node, dict):
        if (node.get('title') or {}).get('code') == code and node.get('id'):
            return str(node['id'])
        for value in node.values():
            found = _find_category(value, code)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_category(value, code)
            if found:
                return found
    return None


LISTING_READERS: dict[str, Callable[[dict[str, Any], int], list[dict[str, str]]]] = {
    'wordpress_ajax_listing': wordpress_ajax_links,
    'json_downloads_api': json_downloads_links,
}


# --- Recovery: find a moved disclosure page from the AMC's own published sitemap ------------------
SITEMAP_KEYWORDS = re.compile(r'(?i)monthly[-_ ]?(?:portfolio|holding)|portfolio[-_ ]disclosure|statutory[-_ ]disclosure')
MAX_SITEMAP_CANDIDATES = 5


def sitemap_candidates(source: dict[str, Any]) -> list[str]:
    """Pages on the AMC's own site whose address says they hold monthly portfolios.

    Only the robots.txt sitemaps the AMC publishes are read. Candidates are suggestions: the health
    check validates each one by listing real monthly files before it is reported as a replacement.
    """
    key, base = source['key'], source['url']
    root = f'{urlsplit(base).scheme}://{urlsplit(base).netloc}'
    try:
        robots = source_request('GET', f'{root}/robots.txt', amc_key=key).text
    except SourceFailure:
        robots = ''
    sitemaps = re.findall(r'(?im)^\s*sitemap:\s*(\S+)', robots) or [f'{root}/sitemap.xml']
    found: list[str] = []
    for sitemap in sitemaps[:3]:
        try:
            text = source_request('GET', sitemap, amc_key=key).text
        except SourceFailure:
            continue
        for loc in re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', text):
            loc = loc.replace('&amp;', '&')
            if SITEMAP_KEYWORDS.search(loc) and loc not in found and loc.rstrip('/') != base.rstrip('/'):
                found.append(loc)
    return found[:MAX_SITEMAP_CANDIDATES]
