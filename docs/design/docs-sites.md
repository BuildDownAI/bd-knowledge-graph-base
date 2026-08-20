# How to add a docs site to the knowledge graph

The `docs_sites:` list in `sources.yml` tells the ingest pipeline which
published documentation sites to crawl. Each entry produces `DocSite`,
`DocPage`, and `DocSection` spine nodes in the graph; sections become
individual search cards so users can find specific passages by meaning.

---

## Before you add any site: the version-selection question

A docs site often serves multiple version areas (e.g. `/stable/` and
`/latest/`). Each crawl root creates **one DocSite node**, so you must decide
upfront:

> **Which area / version of this docs site is closest to the code changes on
> the branch this KG ingests?**

Point the `url:` at that area, and record the answer as `documents_branch:`.

**For a Mintlify site with stable/latest areas:**

| KG branch  | Crawl root URL                          | `documents_branch` |
|------------|-----------------------------------------|--------------------|
| `testing`  | `https://docs.example.com/latest/`      | `testing`          |
| `main`     | `https://docs.example.com/stable/`      | `main`             |

One crawl root = one DocSite node = one branch. If you need both version
areas in the same graph, add two separate entries with different `url:` values.

---

## Minimal entry (own site, linked to repo)

```yaml
docs_sites:
  - url: https://docs.your-project.example.com/latest/
    repo: your-org/your-project     # links DocSite to the code repo node
    documents_branch: testing       # which code branch this docs version documents
```

## Minimal entry (third-party site, no repo link)

```yaml
docs_sites:
  - url: https://click.palletsprojects.com/en/stable/
    max_pages: 50
    delay: 1.0
    user_agent: "KGB-DocBot/1.0 (+https://github.com/your-org/your-kg)"
```

---

## Full field reference

| Field              | Type       | Default          | Description |
|--------------------|------------|------------------|-------------|
| `url`              | string     | *(required)*     | Crawl root URL. The crawl stays within this URL prefix and the same hostname. |
| `repo`             | string     | *(none)*         | `owner/name` slug of a code repo already in the graph. Emits `kg:docsUrl` on the repo node. Omit for third-party sites. |
| `documents_branch` | string     | *(none)*         | Code branch this docs version documents (e.g. `testing`, `main`). Emitted as `kg:documentsBranch` on the DocSite node. |
| `max_depth`        | integer    | `4`              | BFS link-following depth from the crawl root. Ignored when a sitemap is found. |
| `max_pages`        | integer    | `300`            | Hard cap on pages fetched. Apply a lower cap (≤ 50) for third-party sites. |
| `include`          | list[glob] | `[]` (all)       | URL path globs; only paths matching at least one glob are fetched. |
| `exclude`          | list[glob] | `[]` (none)      | URL path globs; matching paths are skipped even if they match `include`. |
| `delay`            | float      | `0.5`            | Seconds to wait between requests. Increase for foreign sites (≥ 1.0 recommended). |
| `user_agent`       | string     | `KGB-DocBot/1.0` | HTTP `User-Agent` header. Include a contact URL when crawling third-party sites. |

---

## Politeness rules (non-negotiable for foreign sites)

We are a guest on third-party sites. Violating these rules risks being
blocked and harms the project's reputation.

1. **Honor `robots.txt`.** The crawler checks `robots.txt` on every run and
   skips disallowed paths. Do not set `exclude:` to work around a disallow.
2. **Keep delays.** Use `delay: 1.0` or higher for any site you do not
   control. The default 0.5 s is acceptable only for your own site.
3. **Cap pages.** Set `max_pages: 50` (or lower) for third-party sites.
   A full crawl of a foreign site is rarely useful and always rude.
4. **Identify yourself.** Set `user_agent:` to include a contact URL so
   operators can reach you if the crawler causes problems.
5. **Check the license.** Some docs sites prohibit automated scraping in
   their terms of service. Read the ToS before adding a third-party site.

---

## Own site vs foreign site

| Question                    | Own site                     | Foreign site                    |
|-----------------------------|------------------------------|---------------------------------|
| `delay:`                    | 0.5 (default) is fine        | ≥ 1.0                           |
| `max_pages:`                | Up to 300                    | ≤ 50                            |
| `user_agent:`               | Default is fine              | Include your contact URL        |
| `repo:` link                | Yes, link to your code repo  | Omit (no repo node to link)     |
| `documents_branch:`         | Set it — it documents branch | Omit unless versioned           |

---

## Extraction notes

The crawler isolates main content via `<article>`, `role="main"`, or `<main>`
tags before extracting sections. Nav, header, footer, and aside elements are
stripped. This works well for Mintlify, MkDocs, Docusaurus, and plain HTML5
sites. Sites that use table-based layouts or render content entirely via
JavaScript may produce poor results.

Line-number gutters from common syntax highlighters (Prism, highlight.js,
Rouge, Sphinx/Pygments) are stripped automatically. Unknown highlighters may
still inject gutter text into section content; this is a known limitation.

When a page has no `<h1>`, the title falls back to the `<title>` tag, then
to the last URL path segment. Sections are still extracted from `<h2>` and
deeper headings — a missing `<h1>` does not prevent section cards from being
built.

---

## Misconfiguration signals

- **DocSite node with no DocPage children:** the crawl returned zero pages.
  Likely causes: the `url:` is unreachable, `include:` is too restrictive, or
  `robots.txt` disallows the root. The DocSite node is still written (SHACL
  conforms), but no search cards are produced.
- **All section text looks like navigation:** the site uses a layout where
  content is outside `<article>`/`<main>`. Try a more specific `include:`
  glob pointing at the content sub-path.
