"""Fetch a source's pages: plain request first, headless Chromium as a fallback, then follow links."""
from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import Source

log = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 cfp-bot")
TIMEOUT = 30
DELAY_SECONDS = 2.0
MIN_TEXT_CHARS = 400
MAX_PAGE_CHARS = 40_000

DEFAULT_LINK_KEYWORDS = ["call", "proposal", "abstract", "submission", "submit", "present",
                         "cfp", "symposium", "conference", "meeting", "congress"]
STRONG_KEYWORDS = {"call", "proposal", "abstract", "submission", "submit", "present", "cfp"}
EXCLUDE_LINK_WORDS = ["past", "archive", "previous", "roommate", "gallery", "photo", "login",
                      "log-in", "sign-in", "signin", "donate", "join", "cart", "privacy", "cookie",
                      "facebook", "twitter", "instagram", "linkedin", "youtube", "video",
                      "download_ical", "exhibit", "advertis", "sponsor", "minutes", "faq", "award",
                      "competition", "banquet", "hotel", "travel", "book", "article", "ethics",
                      "intern program", "interns", "committee", "talent"]
_YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


def mentions_only_past_years(hay: str, this_year: int) -> bool:
    years = [int(y) for y in _YEAR.findall(hay)]
    return bool(years) and max(years) < this_year
SKIP_EXTENSIONS = (".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp3", ".mp4")

DATE_LIKE = re.compile(
    r"\b(20[2-3]\d)\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.I)


@dataclass
class Page:
    url: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, absolute url)
    via: str = "plain"  # or "browser"


@dataclass
class FetchResult:
    pages: list[Page] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    fixture_text: str | None = None  # set when read from tests/fixtures instead of the web

    def combined_text(self) -> str:
        """Every page's text, each followed by its link list; this is what Gemini reads."""
        if self.fixture_text is not None:
            return self.fixture_text
        chunks = []
        for p in self.pages:
            links = "\n".join(f"LINK: {t} -> {u}" for t, u in useful_links(p.links))
            chunks.append(f"=== PAGE: {p.url} ===\n{p.text}\n{links}".rstrip())
        return "\n\n".join(chunks)


class Fetcher:
    """Holds the HTTP session, the robots.txt cache, the per-host delay and the lazy browser."""

    def __init__(self, use_browser: bool = True):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Accept-Language": "en-US,en;q=0.9"})
        self.use_browser = use_browser
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}
        self._pw = None
        self._browser = None

    # ---- politeness -------------------------------------------------------------------------
    def _wait_for_host(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < DELAY_SECONDS:
                time.sleep(DELAY_SECONDS - gap)
        self._last_request[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait_for_host(url)
                r = self.session.get(base + "/robots.txt", timeout=TIMEOUT)
                if r.status_code == 200 and "text/html" not in r.headers.get("content-type", ""):
                    rp.parse(r.text.splitlines())
                    self._robots[base] = rp
                else:
                    self._robots[base] = None  # no usable robots.txt: everything allowed
            except requests.RequestException:
                self._robots[base] = None
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # ---- fetching ---------------------------------------------------------------------------
    def _plain(self, url: str) -> tuple[int, str, str]:
        self._wait_for_host(url)
        r = self.session.get(url, timeout=TIMEOUT)
        r.encoding = r.encoding or r.apparent_encoding
        return r.status_code, r.url, r.text

    def _browser_get(self, url: str) -> tuple[int, str, str]:
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        self._wait_for_host(url)
        page = self._browser.new_page(user_agent=USER_AGENT)
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # some sites never go idle; what has loaded is usually enough
            return (resp.status if resp else 0), page.url, page.content()
        finally:
            page.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
            self._browser = self._pw = None

    def get_page(self, url: str) -> Page:
        """Fetch one URL; fall back to the browser when the plain result is thin or blocked."""
        status, final_url, html, problem = 0, url, "", None
        try:
            status, final_url, html = self._plain(url)
        except requests.RequestException as e:
            problem = f"{type(e).__name__}: {e}"
        page = html_to_page(final_url, html) if html and status < 400 else None
        thin = page is None or len(page.text) < MIN_TEXT_CHARS or not DATE_LIKE.search(page.text)
        if thin and self.use_browser:
            try:
                b_status, b_url, b_html = self._browser_get(url)
                b_page = html_to_page(b_url, b_html, via="browser")
                if b_status < 400 and (page is None or len(b_page.text) > len(page.text)):
                    page, status = b_page, b_status
                elif page is None:
                    problem = f"HTTP {b_status} (browser)"
            except Exception as e:  # browser failures shouldn't hide the plain result
                problem = problem or f"browser: {type(e).__name__}: {e}"
        if page is None:
            raise FetchError(problem or f"HTTP {status}")
        return page

    def fetch_source(self, source: Source) -> FetchResult:
        result = FetchResult()
        start_pages: list[Page] = []
        errors = []
        for url in source.urls:
            if not self.allowed(url):
                errors.append(f"{url}: blocked by robots.txt")
                continue
            try:
                start_pages.append(self.get_page(url))
            except FetchError as e:
                errors.append(f"{url}: {e}")
        if not start_pages:
            result.error = "; ".join(errors) or "no pages fetched"
            return result
        result.pages.extend(start_pages)
        result.notes.extend(errors)

        seen = {canonical(p.url) for p in start_pages} | {canonical(u) for u in source.urls}
        for url in pick_links(start_pages, source, seen):
            if not self.allowed(url):
                result.notes.append(f"{url}: blocked by robots.txt")
                continue
            try:
                page = self.get_page(url)
            except FetchError as e:
                result.notes.append(f"{url}: {e}")
                continue
            if canonical(page.url) not in seen:
                seen.add(canonical(page.url))
                result.pages.append(page)
        return result


class FetchError(Exception):
    pass


# ---- helpers (pure; tested offline) ---------------------------------------------------------
def canonical(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def same_site(a: str, b: str) -> bool:
    """Same site = same last two host labels, so conferences.example.org counts as example.org."""
    def base(u: str) -> str:
        host = urlparse(u).hostname or ""
        return ".".join(host.lower().split(".")[-2:])
    return base(a) == base(b) != ""


BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "tr", "td", "th", "thead", "tbody", "section", "article", "header", "main",
              "aside", "blockquote", "pre", "figure", "figcaption", "address", "hr", "body",
              "button", "option", "label", "details", "summary"]


def html_to_page(url: str, html: str, via: str = "plain") -> Page:
    soup = BeautifulSoup(html, "html.parser")
    # Links come from the whole page (menus included), so menu items like "Symposium" are found.
    links: list[tuple[str, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urldefrag(urljoin(url, href))[0]
        if not absolute.startswith("http"):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())[:120]
        key = (text.lower(), absolute)
        if key in seen:
            continue
        seen.add(key)
        links.append((text, absolute))
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe",
                     "template"]):
        tag.decompose()
    for tag in soup.select('[role="navigation"], [aria-hidden="true"]'):
        tag.decompose()
    # Line breaks only at block elements, so "June 2<sup>nd</sup> – June 6<sup>th</sup>"
    # stays on one line instead of being split at every inline tag.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = []
    for line in soup.get_text().splitlines():
        line = " ".join(line.split())
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    text = "\n".join(lines)[:MAX_PAGE_CHARS]
    return Page(url=url, text=text, links=links, via=via)


LISTED_LINK_WORDS = DEFAULT_LINK_KEYWORDS + ["register", "registration", "detail", "info",
                                             "overview", "guideline", "deadline", "portal"]
MAX_LISTED_LINKS = 40


def useful_links(links: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The links worth showing Gemini (for cfp_url / info_url): ones that look event-related."""
    out = []
    for text, url in links:
        hay = f"{text} {urlparse(url).path}".lower()
        if any(w in hay for w in LISTED_LINK_WORDS) and not any(w in hay for w in EXCLUDE_LINK_WORDS):
            out.append((text, url))
            if len(out) >= MAX_LISTED_LINKS:
                break
    return out


def pick_links(pages: list[Page], source: Source, seen: set[str],
               this_year: int | None = None) -> list[str]:
    """Same-site links whose text or URL contains a keyword, best first, at most max_links."""
    if source.max_links <= 0:
        return []
    this_year = this_year or date.today().year
    keywords = [k.lower() for k in (source.link_keywords or DEFAULT_LINK_KEYWORDS)]
    scored = []
    order = 0
    for page in pages:
        for text, url in page.links:
            order += 1
            if not same_site(url, page.url) or canonical(url) in seen:
                continue
            if urlparse(url).path.lower().endswith(SKIP_EXTENSIONS):
                continue
            hay = f"{text} {urlparse(url).path}".lower()
            if any(w in hay for w in EXCLUDE_LINK_WORDS) or mentions_only_past_years(hay, this_year):
                continue
            hits = [k for k in keywords if k in hay]
            if not hits:
                continue
            score = sum(2 if k in STRONG_KEYWORDS else 1 for k in hits)
            scored.append((-score, order, url))
    picked, picked_keys = [], set()
    for _, _, url in sorted(scored):
        key = canonical(url)
        if key in picked_keys:
            continue
        picked_keys.add(key)
        picked.append(url)
        if len(picked) >= source.max_links:
            break
    return picked
