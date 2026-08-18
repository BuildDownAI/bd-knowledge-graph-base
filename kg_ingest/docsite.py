"""Docsite crawler and HTML extractor.

Entry point: crawl_site(config) -> list[Page]

beautifulsoup4 and requests are imported lazily so that importing this module
does not fail when those packages are absent.

Page shape (contract with KGB-4):
  Page(url, title, sections=[Section(heading, level, text)], fetched_at, content_hash)

content_hash is sha256 of raw HTML bytes — stable across identical fetches so
KGA-2 incremental refresh can detect unchanged pages.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import time
import urllib.parse
import urllib.robotparser
from datetime import datetime, timezone
from xml.etree import ElementTree as ET


# ── Data shapes ───────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Section:
    heading: str   # heading text; "" for content before the first heading
    level: int     # 0 = no heading (preamble), 1–4 = h1–h4
    text: str


@dataclasses.dataclass
class Page:
    url: str
    title: str
    sections: list[Section]
    fetched_at: str    # ISO-8601 UTC
    content_hash: str  # sha256 hex of raw HTML bytes


@dataclasses.dataclass
class CrawlConfig:
    root_url: str
    max_depth: int = 4
    max_pages: int = 300
    include: list[str] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)
    delay: float = 0.5
    user_agent: str = "KGB-DocBot/1.0"
    timeout: int = 30
    retries: int = 3


# ── URL utilities ─────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(p._replace(fragment=""))


def _same_site(url: str, root: str) -> bool:
    u = urllib.parse.urlparse(url)
    r = urllib.parse.urlparse(root)
    if u.scheme not in ("http", "https"):
        return False
    if u.netloc.lower() != r.netloc.lower():
        return False
    root_path = r.path if r.path.endswith("/") else r.path + "/"
    url_path = u.path or "/"
    return url_path == r.path or url_path.startswith(root_path)


def _path_matches_any(url: str, patterns: list[str]) -> bool:
    path = urllib.parse.urlparse(url).path
    return any(fnmatch.fnmatch(path, p) for p in patterns)


# ── Sitemap ───────────────────────────────────────────────────────────────────

def _parse_sitemap_xml(content: bytes) -> tuple[list[str], list[str]]:
    """Return (sub_sitemap_urls, page_urls) from sitemap XML."""
    sub_sitemaps: list[str] = []
    pages: list[str] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return sub_sitemaps, pages

    def bare(el: ET.Element) -> str:
        return el.tag.split("}")[-1] if "}" in el.tag else el.tag

    root_tag = bare(root)
    for child in root:
        child_tag = bare(child)
        if child_tag == "sitemap":
            for loc in child:
                if bare(loc) == "loc" and loc.text:
                    sub_sitemaps.append(loc.text.strip())
        elif child_tag == "url":
            for loc in child:
                if bare(loc) == "loc" and loc.text:
                    pages.append(loc.text.strip())
    return sub_sitemaps, pages


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get_response(url: str, config: CrawlConfig, session):
    last_exc: Exception | None = None
    for attempt in range(config.retries):
        try:
            return session.get(url, timeout=config.timeout, allow_redirects=True)
        except Exception as exc:
            last_exc = exc
            if attempt < config.retries - 1:
                time.sleep(2 ** attempt)
    raise last_exc  # type: ignore[misc]


def _get_robots(origin: str, config: CrawlConfig, session) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = _get_response(f"{origin}/robots.txt", config, session)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
    except Exception:
        pass
    return rp


def _discover_via_sitemap(origin: str, root: str, config: CrawlConfig, session) -> list[str]:
    try:
        resp = _get_response(f"{origin}/sitemap.xml", config, session)
        if not resp or resp.status_code != 200:
            return []
        sub_sitemaps, pages = _parse_sitemap_xml(resp.content)
        if sub_sitemaps:
            expanded: list[str] = []
            for ss_url in sub_sitemaps:
                try:
                    r2 = _get_response(ss_url, config, session)
                    if r2 and r2.status_code == 200:
                        _, pp = _parse_sitemap_xml(r2.content)
                        expanded.extend(pp)
                    time.sleep(config.delay)
                except Exception:
                    pass
            pages = expanded
        return [_normalize_url(p) for p in pages if _same_site(p, root)]
    except Exception:
        return []


# ── HTML extraction ───────────────────────────────────────────────────────────

def _extract_links(html_bytes: bytes, base_url: str) -> list[str]:
    from bs4 import BeautifulSoup  # lazy
    soup = BeautifulSoup(html_bytes, "lxml")
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        links.append(_normalize_url(urllib.parse.urljoin(base_url, href)))
    return links


def _isolate_main_content(soup):
    """Return the main content element; strips nav/footer/aside when falling back."""
    main = (
        soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.find("main")
    )
    if main:
        return main
    body = soup.find("body") or soup
    for tag in body.find_all(["nav", "header", "footer", "aside"]):
        tag.decompose()
    return body


def _extract_sections(main_el) -> list[Section]:
    from bs4 import NavigableString, Tag  # lazy

    HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
    BLOCK_TAGS = {"p", "li", "pre", "blockquote", "td", "dd", "dt"}
    SKIP_TAGS = {"nav", "header", "footer", "aside", "script", "style", "noscript"}

    sections: list[Section] = []
    state: dict = {"heading": "", "level": 0, "texts": []}

    def flush() -> None:
        text = " ".join(state["texts"]).strip()
        if state["heading"] or text:
            sections.append(Section(
                heading=state["heading"],
                level=state["level"],
                text=text,
            ))
        state["heading"] = ""
        state["level"] = 0
        state["texts"] = []

    def visit(el) -> None:
        if isinstance(el, NavigableString):
            text = str(el).strip()
            if text:
                state["texts"].append(text)
            return
        if not isinstance(el, Tag):
            return
        if el.name in SKIP_TAGS:
            return
        if el.name in HEADING_TAGS:
            flush()
            state["heading"] = el.get_text(strip=True)
            state["level"] = HEADING_TAGS[el.name]
        elif el.name in BLOCK_TAGS:
            text = el.get_text(separator=" ", strip=True)
            if text:
                state["texts"].append(text)
        else:
            for child in el.children:
                visit(child)

    for child in main_el.children:
        visit(child)
    flush()
    return sections


def _extract_page(html_bytes: bytes, url: str) -> Page:
    from bs4 import BeautifulSoup  # lazy

    soup = BeautifulSoup(html_bytes, "lxml")

    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        title = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1] or url

    main = _isolate_main_content(soup)
    sections = _extract_sections(main)

    return Page(
        url=url,
        title=title,
        sections=sections,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        content_hash=hashlib.sha256(html_bytes).hexdigest(),
    )


# ── Public entry point ────────────────────────────────────────────────────────

def crawl_site(config: CrawlConfig) -> list[Page]:
    """Crawl a documentation site and return one Page per fetched HTML page.

    Tries <origin>/sitemap.xml first; falls back to BFS link-walking.
    Respects robots.txt, max_depth, max_pages, and include/exclude path globs.
    No graph writes — callers (KGB-4) consume the returned Page objects.
    """
    import requests as _requests  # lazy
    from collections import deque

    session = _requests.Session()
    session.headers["User-Agent"] = config.user_agent

    root = _normalize_url(config.root_url)
    parsed = urllib.parse.urlparse(root)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    robots = _get_robots(origin, config, session)
    sitemap_pages = _discover_via_sitemap(origin, root, config, session)

    pages: list[Page] = []
    seen: set[str] = set()

    def _ok(url: str) -> bool:
        if config.include and not _path_matches_any(url, config.include):
            return False
        if config.exclude and _path_matches_any(url, config.exclude):
            return False
        return robots.can_fetch(config.user_agent, url)

    if sitemap_pages:
        for url in sitemap_pages:
            if len(pages) >= config.max_pages:
                break
            if url in seen or not _ok(url):
                continue
            seen.add(url)
            try:
                resp = _get_response(url, config, session)
                if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
                    pages.append(_extract_page(resp.content, url))
            except Exception:
                pass
            time.sleep(config.delay)
    else:
        queue: deque[tuple[str, int]] = deque([(root, 0)])
        seen.add(root)
        while queue and len(pages) < config.max_pages:
            url, depth = queue.popleft()
            if not _ok(url):
                continue
            try:
                resp = _get_response(url, config, session)
                if resp.status_code != 200:
                    continue
                if "html" not in resp.headers.get("content-type", ""):
                    continue
                page = _extract_page(resp.content, url)
                pages.append(page)
                time.sleep(config.delay)
                if depth < config.max_depth:
                    for link in _extract_links(resp.content, url):
                        if link not in seen and _same_site(link, root):
                            seen.add(link)
                            queue.append((link, depth + 1))
            except Exception:
                pass

    return pages
