"""Tests for kg_ingest.docsite crawler and extractor.

Uses a local http.server fixture so CI never hits the live internet.
Run: PYTHONPATH=. ./.venv/bin/python tests/test_docsite.py
"""
import http.server
import socketserver
import threading
import urllib.parse

from kg_ingest.docsite import CrawlConfig, Page, Section, crawl_site, _parse_sitemap_xml

# ---------------------------------------------------------------------------
# Fixture HTML pages served by the local test server
# ---------------------------------------------------------------------------

_ROOT = b"""<html><head><title>Docs Home</title></head>
<body>
<h1>Welcome</h1>
<p>Main content on the root page.</p>
<ul>
  <li><a href="/install.html">Installation</a></li>
  <li><a href="/api.html">API Reference</a></li>
  <li><a href="/guide.html">User Guide</a></li>
  <li><a href="/faq.html">FAQ</a></li>
  <li><a href="/concepts.html">Concepts</a></li>
  <li><a href="/examples.html">Examples</a></li>
  <li><a href="/config.html">Configuration</a></li>
  <li><a href="/troubleshoot.html">Troubleshooting</a></li>
  <li><a href="/changelog/v1.html">Changelog v1</a></li>
  <li><a href="/private/secret.html">Secret</a></li>
</ul>
</body></html>"""

# Page with h1 / h2 / h3 heading structure
_INSTALL = b"""<html><head><title>Installation Guide</title></head>
<body>
<h1>Installation Guide</h1>
<p>Install using pip.</p>
<h2>Prerequisites</h2>
<p>Python 3.10 or later.</p>
<h3>Optional dependencies</h3>
<p>Install extras for additional features.</p>
</body></html>"""

# Page with nav / footer / aside that must be stripped
_API = b"""<html><head><title>API Reference</title></head>
<body>
<nav>Nav sidebar link Alpha, Nav sidebar link Beta</nav>
<article>
<h1>API Reference</h1>
<p>The main API content here.</p>
</article>
<footer>Footer copyright notice Delta</footer>
<aside>Aside sidebar content Gamma</aside>
</body></html>"""

_SIMPLE = lambda title: (
    f"<html><head><title>{title}</title></head>"
    f"<body><h1>{title}</h1><p>{title} content.</p></body></html>"
).encode()

_ROBOTS = b"User-agent: *\nDisallow: /private/\n"

_CHANGELOG = b"""<html><head><title>Changelog v1</title></head>
<body><h1>Changelog v1</h1><p>Version 1 release notes.</p></body></html>"""

_PRIVATE = b"""<html><head><title>Secret Page</title></head>
<body><h1>Secret</h1><p>This should not be crawled.</p></body></html>"""

_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>__BASE__/</loc></url>
  <url><loc>__BASE__/install.html</loc></url>
  <url><loc>__BASE__/api.html</loc></url>
</urlset>"""

# MkDocs-style fixture: content inside <div class="md-content"><article>, no nav in content
_MKDOCS = b"""<html><head><title>MkDocs Page</title></head>
<body>
<nav class="md-nav">Nav link One Nav link Two</nav>
<div class="md-content">
  <article>
    <h2>Configuration</h2>
    <p>Configure the tool using a YAML file.</p>
    <h2>Advanced Usage</h2>
    <p>Advanced patterns for power users.</p>
  </article>
</div>
<footer class="md-footer">Footer content Bravo</footer>
</body></html>"""

# Page with no h1 — title must fall back to <title>; sections from h2+
_NO_H1 = b"""<html><head><title>Page Without H1</title></head>
<body>
<main>
  <h2>First Section</h2>
  <p>Content of the first section without an h1.</p>
  <h2>Second Section</h2>
  <p>Content of the second section.</p>
</main>
</body></html>"""

# Prism line-numbers gutter: digits must NOT appear in section text after stripping
_CODE_GUTTER = b"""<html><head><title>Code Example</title></head>
<body>
<article>
  <h2>Example</h2>
  <pre class="language-python line-numbers"><span aria-hidden="true" class="line-numbers-rows"><span>1</span><span>2</span></span>def hello():
    return world
  </pre>
</article>
</body></html>"""


# Sphinx-style heading with a headerlink pilcrow that must be stripped
_SPHINX_HEADERLINK = b"""<html><head><title>Sphinx Page</title></head>
<body>
<main>
  <h2>Quickstart<a class="headerlink" href="#quickstart" title="Link to this heading">\xc2\xb6</a></h2>
  <p>Getting going quickly.</p>
</main>
</body></html>"""

_FIXTURE_PAGES: dict[str, tuple[str, bytes]] = {
    "/robots.txt":        ("text/plain",  _ROBOTS),
    "/":                  ("text/html",   _ROOT),
    "/install.html":      ("text/html",   _INSTALL),
    "/api.html":          ("text/html",   _API),
    "/guide.html":        ("text/html",   _SIMPLE("User Guide")),
    "/faq.html":          ("text/html",   _SIMPLE("FAQ")),
    "/concepts.html":     ("text/html",   _SIMPLE("Concepts")),
    "/examples.html":     ("text/html",   _SIMPLE("Examples")),
    "/config.html":       ("text/html",   _SIMPLE("Configuration")),
    "/troubleshoot.html": ("text/html",   _SIMPLE("Troubleshooting")),
    "/changelog/v1.html": ("text/html",   _CHANGELOG),
    "/private/secret.html": ("text/html", _PRIVATE),
    "/mkdocs.html":       ("text/html",   _MKDOCS),
    "/no-h1.html":        ("text/html",   _NO_H1),
    "/code.html":         ("text/html",   _CODE_GUTTER),
    "/sphinx.html":       ("text/html",   _SPHINX_HEADERLINK),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        entry = _FIXTURE_PAGES.get(path)
        if entry:
            ct, body = entry
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class _FixtureServer(socketserver.TCPServer):
    allow_reuse_address = True


def _start_server() -> tuple[_FixtureServer, str]:
    server = _FixtureServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_basic_crawl(base_url: str) -> None:
    """Every returned Page has non-empty required fields and sections."""
    cfg = CrawlConfig(root_url=base_url, delay=0.0, max_pages=50)
    pages = crawl_site(cfg)

    assert len(pages) >= 5, f"Expected >=5 pages, got {len(pages)}"
    for p in pages:
        assert isinstance(p, Page), f"Not a Page: {p!r}"
        assert p.url, "url must be non-empty"
        assert p.title, f"title must be non-empty for {p.url}"
        assert p.fetched_at, f"fetched_at must be non-empty for {p.url}"
        assert p.content_hash and len(p.content_hash) == 64, \
            f"content_hash must be a 64-char hex string for {p.url}"
        assert isinstance(p.sections, list), f"sections must be a list for {p.url}"

    urls = {p.url for p in pages}
    assert any("install" in u for u in urls), "install.html should be crawled"
    print(f"PASS: test_basic_crawl ({len(pages)} pages)")


def test_heading_structure(base_url: str) -> None:
    """h1/h2/h3 tags produce sections with correct heading, level, and text."""
    cfg = CrawlConfig(root_url=f"{base_url}/install.html", delay=0.0, max_pages=1)
    pages = crawl_site(cfg)

    assert len(pages) == 1, f"Expected 1 page, got {len(pages)}"
    page = pages[0]
    assert page.title == "Installation Guide", f"Unexpected title: {page.title!r}"

    by_heading = {s.heading: s for s in page.sections}
    assert "Installation Guide" in by_heading, \
        f"Expected h1 section; got headings: {list(by_heading)}"
    assert "Prerequisites" in by_heading, \
        f"Expected h2 section; got headings: {list(by_heading)}"
    assert "Optional dependencies" in by_heading, \
        f"Expected h3 section; got headings: {list(by_heading)}"

    h1 = by_heading["Installation Guide"]
    assert h1.level == 1, f"h1 section level must be 1, got {h1.level}"
    assert "pip" in h1.text, f"h1 text should contain 'pip': {h1.text!r}"

    h2 = by_heading["Prerequisites"]
    assert h2.level == 2, f"h2 section level must be 2, got {h2.level}"
    assert "Python 3.10" in h2.text, f"h2 text should mention Python: {h2.text!r}"

    h3 = by_heading["Optional dependencies"]
    assert h3.level == 3, f"h3 section level must be 3, got {h3.level}"
    assert "extras" in h3.text, f"h3 text should mention extras: {h3.text!r}"

    print("PASS: test_heading_structure")


def test_content_isolation(base_url: str) -> None:
    """nav/footer/aside text is excluded; article content is preserved."""
    cfg = CrawlConfig(root_url=f"{base_url}/api.html", delay=0.0, max_pages=1)
    pages = crawl_site(cfg)

    assert len(pages) == 1
    page = pages[0]
    all_text = " ".join(s.text for s in page.sections)

    # Chrome text must NOT appear
    assert "Alpha" not in all_text, f"nav text 'Alpha' leaked into sections: {all_text!r}"
    assert "Beta" not in all_text, f"nav text 'Beta' leaked into sections: {all_text!r}"
    assert "Delta" not in all_text, f"footer text 'Delta' leaked into sections: {all_text!r}"
    assert "Gamma" not in all_text, f"aside text 'Gamma' leaked into sections: {all_text!r}"

    # Main content MUST appear
    assert "main API content" in all_text, \
        f"Article text missing from sections: {all_text!r}"

    print("PASS: test_content_isolation")


def test_robots_disallow(base_url: str) -> None:
    """robots.txt Disallow: /private/ prevents fetching those pages."""
    cfg = CrawlConfig(root_url=base_url, delay=0.0, max_pages=50)
    pages = crawl_site(cfg)

    private_pages = [p for p in pages if "/private/" in p.url]
    assert not private_pages, \
        f"Expected no /private/ pages, got: {[p.url for p in private_pages]}"

    print("PASS: test_robots_disallow")


def test_max_pages(base_url: str) -> None:
    """max_pages caps the result to at most that many pages."""
    cfg = CrawlConfig(root_url=base_url, delay=0.0, max_pages=3)
    pages = crawl_site(cfg)

    assert len(pages) <= 3, f"Expected <=3 pages with max_pages=3, got {len(pages)}"
    assert len(pages) >= 1, "Expected at least 1 page"
    print(f"PASS: test_max_pages (got {len(pages)} pages with max_pages=3)")


def test_exclude_pattern(base_url: str) -> None:
    """exclude glob prevents matching pages from being crawled."""
    cfg = CrawlConfig(
        root_url=base_url,
        delay=0.0,
        max_pages=50,
        exclude=["**/changelog/**"],
    )
    pages = crawl_site(cfg)

    changelog_pages = [p for p in pages if "/changelog/" in p.url]
    assert not changelog_pages, \
        f"Expected no /changelog/ pages with exclude, got: {[p.url for p in changelog_pages]}"
    print("PASS: test_exclude_pattern")


def test_robots_missing_allows_crawl(base_url: str) -> None:
    """When robots.txt returns 404, crawling is not blocked (allow all)."""
    # Temporarily remove robots.txt from the fixture so the server returns 404
    robots_entry = _FIXTURE_PAGES.pop("/robots.txt")
    try:
        cfg = CrawlConfig(root_url=base_url, delay=0.0, max_pages=50)
        pages = crawl_site(cfg)
    finally:
        _FIXTURE_PAGES["/robots.txt"] = robots_entry

    assert len(pages) >= 5, (
        f"Expected >=5 pages when robots.txt is missing (404), got {len(pages)}. "
        "Sites without robots.txt must not be silently blocked."
    )
    print(f"PASS: test_robots_missing_allows_crawl ({len(pages)} pages)")


def test_include_pattern(base_url: str) -> None:
    """include glob restricts fetching to only matching paths.

    Uses sitemap mode so all candidate URLs are known upfront and the include
    filter is the only gate between discovery and fetching.
    """
    sitemap_content = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>" + base_url.encode() + b"/install.html</loc></url>"
        b"<url><loc>" + base_url.encode() + b"/api.html</loc></url>"
        b"<url><loc>" + base_url.encode() + b"/guide.html</loc></url>"
        b"<url><loc>" + base_url.encode() + b"/faq.html</loc></url>"
        b"</urlset>"
    )
    _FIXTURE_PAGES["/sitemap.xml"] = ("application/xml", sitemap_content)
    try:
        cfg = CrawlConfig(
            root_url=base_url,
            delay=0.0,
            max_pages=50,
            include=["/install.html", "/api.html"],
        )
        pages = crawl_site(cfg)
    finally:
        del _FIXTURE_PAGES["/sitemap.xml"]

    urls = {urllib.parse.urlparse(p.url).path for p in pages}
    # Both included pages must appear
    assert "/install.html" in urls, f"/install.html missing from include crawl: {urls}"
    assert "/api.html" in urls, f"/api.html missing from include crawl: {urls}"
    # Pages in the sitemap but outside the include list must NOT appear
    excluded = urls - {"/install.html", "/api.html"}
    assert not excluded, f"Pages outside include list were crawled: {excluded}"
    print(f"PASS: test_include_pattern ({len(pages)} pages)")


def test_content_hash_deterministic(base_url: str) -> None:
    """Same HTML content produces identical content_hash across two crawls."""
    cfg = CrawlConfig(root_url=base_url, delay=0.0, max_pages=1)

    pages1 = crawl_site(cfg)
    pages2 = crawl_site(cfg)

    assert pages1 and pages2, "Expected at least one page in each crawl"
    assert pages1[0].url == pages2[0].url, "First page URL must be the same"
    assert pages1[0].content_hash == pages2[0].content_hash, (
        f"content_hash not deterministic: "
        f"{pages1[0].content_hash!r} vs {pages2[0].content_hash!r}"
    )
    print("PASS: test_content_hash_deterministic")


def test_sitemap_parsing() -> None:
    """_parse_sitemap_xml handles both urlset and sitemapindex formats."""
    urlset = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/page1</loc></url>
      <url><loc>https://example.com/page2</loc></url>
    </urlset>"""
    sub, pages = _parse_sitemap_xml(urlset)
    assert sub == [], f"urlset should have no sub-sitemaps: {sub}"
    assert pages == ["https://example.com/page1", "https://example.com/page2"], pages

    index = b"""<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sitemap-posts.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sitemap-docs.xml</loc></sitemap>
    </sitemapindex>"""
    sub, pages = _parse_sitemap_xml(index)
    assert sub == [
        "https://example.com/sitemap-posts.xml",
        "https://example.com/sitemap-docs.xml",
    ], sub
    assert pages == [], f"sitemapindex should have no direct pages: {pages}"

    # Malformed XML returns empty lists without raising
    sub, pages = _parse_sitemap_xml(b"<not valid xml &&&")
    assert sub == [] and pages == []

    print("PASS: test_sitemap_parsing")


def test_sitemap_crawl(base_url: str) -> None:
    """Sitemap-mode: pages listed in sitemap.xml are fetched."""
    sitemap_content = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>" + base_url.encode() + b"/install.html</loc></url>"
        b"<url><loc>" + base_url.encode() + b"/api.html</loc></url>"
        b"</urlset>"
    )
    # Temporarily inject a sitemap into the fixture table
    _FIXTURE_PAGES["/sitemap.xml"] = ("application/xml", sitemap_content)
    try:
        cfg = CrawlConfig(root_url=base_url, delay=0.0, max_pages=10)
        pages = crawl_site(cfg)
    finally:
        del _FIXTURE_PAGES["/sitemap.xml"]

    urls = {p.url for p in pages}
    assert any("install" in u for u in urls), f"install.html missing from sitemap crawl: {urls}"
    assert any("api" in u for u in urls), f"api.html missing from sitemap crawl: {urls}"
    print(f"PASS: test_sitemap_crawl ({len(pages)} pages via sitemap)")


def test_sparse_sitemap_falls_back_to_links(base_url: str) -> None:
    """A sitemap listing ONLY the root (Read-the-Docs style) must not suppress
    link-walking — linked pages are still discovered (KGB-5 smoke finding)."""
    sitemap_content = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>" + base_url.encode() + b"/</loc></url>"
        b"</urlset>"
    )
    _FIXTURE_PAGES["/sitemap.xml"] = ("application/xml", sitemap_content)
    try:
        cfg = CrawlConfig(root_url=base_url, delay=0.0, max_pages=10)
        pages = crawl_site(cfg)
    finally:
        del _FIXTURE_PAGES["/sitemap.xml"]

    urls = {p.url for p in pages}
    assert len(pages) > 1, f"sparse sitemap suppressed BFS: only {urls}"
    assert any("install" in u for u in urls), f"linked install.html not discovered: {urls}"
    print(f"PASS: test_sparse_sitemap_falls_back_to_links ({len(pages)} pages)")


def test_sphinx_headerlink_stripped(base_url: str) -> None:
    """Sphinx headerlink pilcrows are removed from heading text."""
    cfg = CrawlConfig(root_url=f"{base_url}/sphinx.html", delay=0.0, max_pages=1)
    pages = crawl_site(cfg)
    assert len(pages) == 1
    headings = {s.heading for s in pages[0].sections if s.level > 0}
    assert "Quickstart" in headings, f"clean heading missing: {headings}"
    assert not any("\u00b6" in h for h in headings), f"pilcrow leaked: {headings}"
    print("PASS: test_sphinx_headerlink_stripped")


def test_lazy_import() -> None:
    """Importing kg_ingest.docsite must not import bs4 at module level."""
    import importlib
    import sys

    # Remove any cached import so we test the module's own top-level behaviour
    for key in list(sys.modules):
        if key.startswith("kg_ingest.docsite"):
            del sys.modules[key]

    # Temporarily hide bs4 from the import machinery
    bs4_mod = sys.modules.pop("bs4", None)
    try:
        import kg_ingest.docsite  # must not raise ImportError
        assert hasattr(kg_ingest.docsite, "crawl_site")
    finally:
        if bs4_mod is not None:
            sys.modules["bs4"] = bs4_mod

    print("PASS: test_lazy_import")


def test_mkdocs_content_isolation(base_url: str) -> None:
    """MkDocs-style <article> inside a wrapper div: nav/footer stripped, article content kept."""
    cfg = CrawlConfig(root_url=f"{base_url}/mkdocs.html", delay=0.0, max_pages=1)
    pages = crawl_site(cfg)

    assert len(pages) == 1
    page = pages[0]
    all_text = " ".join(s.text for s in page.sections)

    assert "One" not in all_text, f"nav text leaked: {all_text!r}"
    assert "Two" not in all_text, f"nav text leaked: {all_text!r}"
    assert "Bravo" not in all_text, f"footer text leaked: {all_text!r}"

    headings = {s.heading for s in page.sections}
    assert "Configuration" in headings, f"Expected 'Configuration' section; got {headings}"
    assert "Advanced Usage" in headings, f"Expected 'Advanced Usage' section; got {headings}"
    assert "YAML" in all_text, f"Article content missing: {all_text!r}"
    print("PASS: test_mkdocs_content_isolation")


def test_missing_h1_fallback(base_url: str) -> None:
    """Page with no h1: title from <title> tag; sections extracted from h2+ headings."""
    cfg = CrawlConfig(root_url=f"{base_url}/no-h1.html", delay=0.0, max_pages=1)
    pages = crawl_site(cfg)

    assert len(pages) == 1
    page = pages[0]

    assert page.title == "Page Without H1", f"Expected title from <title>, got {page.title!r}"

    headings = {s.heading for s in page.sections if s.level > 0}
    assert "First Section" in headings, f"h2 sections missing; got {headings}"
    assert "Second Section" in headings, f"h2 sections missing; got {headings}"
    print("PASS: test_missing_h1_fallback")


def test_line_number_gutter_stripped(base_url: str) -> None:
    """Prism line-numbers gutter digits are not included in section text."""
    cfg = CrawlConfig(root_url=f"{base_url}/code.html", delay=0.0, max_pages=1)
    pages = crawl_site(cfg)

    assert len(pages) == 1
    page = pages[0]
    all_text = " ".join(s.text for s in page.sections)

    # The fixture code has no digits; any digit means gutter leaked through
    import re
    bare_digits = re.findall(r"(?<!\w)\d+(?!\w)", all_text)
    assert not bare_digits, (
        f"Line-number gutter digits leaked into section text: {bare_digits!r}\n"
        f"Full section text: {all_text!r}"
    )
    assert "hello" in all_text or "world" in all_text, \
        f"Actual code content missing after gutter strip: {all_text!r}"
    print("PASS: test_line_number_gutter_stripped")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server, base_url = _start_server()
    try:
        test_sitemap_parsing()
        test_lazy_import()
        test_basic_crawl(base_url)
        test_heading_structure(base_url)
        test_content_isolation(base_url)
        test_robots_disallow(base_url)
        test_max_pages(base_url)
        test_exclude_pattern(base_url)
        test_robots_missing_allows_crawl(base_url)
        test_include_pattern(base_url)
        test_content_hash_deterministic(base_url)
        test_sitemap_crawl(base_url)
        test_sparse_sitemap_falls_back_to_links(base_url)
        test_sphinx_headerlink_stripped(base_url)
        test_mkdocs_content_isolation(base_url)
        test_missing_h1_fallback(base_url)
        test_line_number_gutter_stripped(base_url)
        print("\nALL TESTS PASSED")
    finally:
        server.shutdown()


def test_zero_width_stripped():
    from kg_ingest.docsite import _clean
    assert _clean("​Required") == "Required"
    assert _clean("﻿Title​") == "Title"
    print("PASS: test_zero_width_stripped")


if __name__ == "__main__" and "test_zero_width_stripped" not in globals().get("_ran", []):
    test_zero_width_stripped()
