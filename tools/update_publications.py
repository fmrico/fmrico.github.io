#!/usr/bin/env python3
"""Update publications.html from Google Scholar and generate per-paper BibTeX files.

- Source list: Google Scholar profile (title/authors/venue/year + Scholar detail URL)
- External link: best-effort from Scholar citation detail page (publisher landing page)
- DOI/BibTeX/PDF: Crossref lookup by title (best-effort)

This script is intentionally dependency-free (requests only).
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Iterable

import requests

SCHOLAR_USER = "5e4caiwAAAAJ"
SCHOLAR_HL = "es"
PAGESIZE = 100

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT_HTML = os.path.join(ROOT_DIR, "publications.html")
BIB_DIR = os.path.join(ROOT_DIR, "bibtex")

UA = "fmrico.github.io/1.0 (mailto:francisco.rico@urjc.es)"


@dataclass(frozen=True)
class Pub:
    title: str
    authors: str
    venue: str
    year: str
    scholar_url: str


@dataclass
class EnrichedPub:
    pub: Pub
    link_url: str | None = None
    doi: str | None = None
    doi_url: str | None = None
    pdf_url: str | None = None
    bib_filename: str | None = None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def _get_text(s: requests.Session, url: str, *, sleep_s: float = 0.0) -> str:
    if sleep_s:
        time.sleep(sleep_s)
    r = s.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def _abs_scholar_url(href: str) -> str:
    href = href.replace("&amp;", "&")
    if href.startswith("http"):
        return href
    return "https://scholar.google.com" + href


def _extract_first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, flags=re.S)
    return m.group(1) if m else None


def _parse_profile_rows(profile_html: str) -> list[Pub]:
    pubs: list[Pub] = []

    for row in re.findall(r"<tr class=\"gsc_a_tr\".*?</tr>", profile_html, flags=re.S):
        a_tag = _extract_first(r"(<a[^>]*class=\"gsc_a_at\"[^>]*>.*?</a>)", row)
        if not a_tag:
            continue
        href = _extract_first(r"href=\"([^\"]+)\"", a_tag)
        title = _extract_first(r"class=\"gsc_a_at\"[^>]*>(.*?)</a>", a_tag)
        if not href or title is None:
            continue

        title = html.unescape(re.sub(r"<.*?>", "", title)).strip()
        scholar_url = _abs_scholar_url(href)

        grays = re.findall(r"<div class=\"gs_gray\">(.*?)</div>", row, flags=re.S)
        authors = html.unescape(re.sub(r"<.*?>", "", grays[0])).strip() if len(grays) >= 1 else ""
        venue = html.unescape(re.sub(r"<.*?>", "", grays[1])).strip() if len(grays) >= 2 else ""
        venue = re.sub(r"\s+", " ", venue)

        year = _extract_first(r"class=\"gsc_a_y\"[^>]*>\s*<span[^>]*>(\d{4})</span>", row) or ""

        pubs.append(Pub(title=title, authors=authors, venue=venue, year=year, scholar_url=scholar_url))

    return pubs


def fetch_scholar_publications(s: requests.Session) -> list[Pub]:
    all_pubs: list[Pub] = []
    for cstart in range(0, 5000, PAGESIZE):
        url = (
            f"https://scholar.google.com/citations?user={SCHOLAR_USER}"
            f"&hl={SCHOLAR_HL}&cstart={cstart}&pagesize={PAGESIZE}"
        )
        html_text = _get_text(s, url, sleep_s=1.0 if cstart else 0.0)
        pubs = _parse_profile_rows(html_text)
        if not pubs:
            break
        all_pubs.extend(pubs)

    # Deduplicate by (title, year)
    seen: set[tuple[str, str]] = set()
    deduped: list[Pub] = []
    for p in all_pubs:
        key = (p.title.casefold(), p.year)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)

    return deduped


def fetch_scholar_external_link(s: requests.Session, scholar_url: str) -> str | None:
    try:
        detail_html = _get_text(s, scholar_url, sleep_s=0.8)
    except Exception:
        return None

    link = _extract_first(r"class=\"gsc_oci_title_link\"[^>]*href=\"([^\"]+)\"", detail_html)
    if not link:
        return None
    return html.unescape(link)


def _norm_title(t: str) -> str:
    t = t.casefold()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def crossref_best_match(s: requests.Session, title: str, year: str) -> dict | None:
    query = urllib.parse.quote(title)
    url = f"https://api.crossref.org/works?rows=5&query.bibliographic={query}"
    try:
        data = s.get(url, timeout=30, headers={"User-Agent": UA}).json()
    except Exception:
        return None

    items = data.get("message", {}).get("items", [])
    if not items:
        return None

    want = _norm_title(title)

    def score(it: dict) -> float:
        cand_title = (it.get("title") or [""])[0]
        cand = _norm_title(cand_title)
        if not cand:
            return 0.0
        # simple overlap score
        want_set = set(want.split())
        cand_set = set(cand.split())
        jacc = len(want_set & cand_set) / max(1, len(want_set | cand_set))

        y = ""
        issued = it.get("issued", {}).get("date-parts", [])
        if issued and issued[0] and issued[0][0]:
            y = str(issued[0][0])
        year_bonus = 0.08 if (year and y == year) else 0.0

        return jacc + year_bonus

    best = max(items, key=score)
    if score(best) < 0.35:
        return None
    return best


def crossref_bibtex(s: requests.Session, doi: str) -> str | None:
    doi_enc = urllib.parse.quote(doi)
    url = f"https://api.crossref.org/works/{doi_enc}/transform/application/x-bibtex"
    try:
        r = s.get(url, timeout=30, headers={"User-Agent": UA})
        if r.status_code != 200:
            return None
        return r.text.strip() + "\n"
    except Exception:
        return None


def _sanitize_doi(doi: str) -> str:
    # Keep filenames stable and filesystem-safe.
    s = doi.strip().lower()
    s = s.replace("https://doi.org/", "")
    s = s.replace("http://doi.org/", "")
    s = s.replace("/", "_")
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s


def _slug(s: str) -> str:
    s = _norm_title(s)
    s = s.replace(" ", "-")
    s = s[:80].strip("-")
    return s or "pub"


def _make_id_fallback(p: Pub) -> str:
    base = f"{p.title}|{p.year}|{p.venue}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{_slug(p.title)}-{p.year or 'noyear'}-{h}"


def write_bibtex_file(filename: str, content: str) -> None:
    os.makedirs(BIB_DIR, exist_ok=True)
    path = os.path.join(BIB_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _escape_attr(u: str) -> str:
    return html.escape(u, quote=True)


def build_publications_html(pubs: Iterable[EnrichedPub]) -> str:
    groups: dict[str, list[EnrichedPub]] = {}
    for p in pubs:
        y = p.pub.year or "Unknown"
        groups.setdefault(y, []).append(p)

    def year_key(y: str) -> int:
        return int(y) if y.isdigit() else -1

    out: list[str] = []
    for y in sorted(groups.keys(), key=year_key, reverse=True):
        out.append(f'    <h3 class="pub-year">{html.escape(y)}</h3>')
        out.append('    <div class="pub-list">')
        for ep in groups[y]:
            p = ep.pub
            where = p.venue
            if p.year and p.year not in where:
                where = f"{where}, {p.year}" if where else p.year

            cite = f"{p.authors}, “{p.title},” {p.venue}, {p.year}.".strip()
            cite = re.sub(r"\s+", " ", cite)

            link_url = ep.link_url or ep.doi_url or p.scholar_url
            paper_url = ep.pdf_url or link_url

            bib_href = f"bibtex/{ep.bib_filename}" if ep.bib_filename else "#"

            out.append('      <article class="card pub-card">')
            out.append('        <div class="pub-top">')
            out.append(f'          <div class="pub-where">{html.escape(where)}</div>')
            out.append('          <div class="badges">')
            if p.year:
                out.append(f'            <span class="badge">{html.escape(p.year)}</span>')
            out.append('            <span class="badge badge-muted">Q?</span>')
            out.append('          </div>')
            out.append('        </div>')
            out.append(f'        <p class="pub-cite">{html.escape(cite)}</p>')

            if ep.doi and ep.doi_url:
                out.append(
                    '        <p class="pub-doi">DOI: '
                    f'<a href="{_escape_attr(ep.doi_url)}">{html.escape(ep.doi)}</a></p>'
                )

            out.append('        <div class="pub-links">')
            out.append(f'          <a href="{_escape_attr(link_url)}">Link</a>')
            out.append(f'          <a href="{_escape_attr(p.scholar_url)}">Scholar</a>')
            if ep.bib_filename:
                out.append(
                    f'          <a href="{_escape_attr(bib_href)}" download>BiBTeX</a>'
                )
            else:
                out.append('          <a href="#">BibTeX</a>')
            out.append(f'          <a href="{_escape_attr(paper_url)}">Paper</a>')
            out.append('          <a href="#">Video</a>')
            out.append('        </div>')
            out.append('      </article>')
        out.append('    </div>')

    return "\n".join(out) + "\n"


def write_publications_page(pubs_html: str) -> None:
    doc = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Publications – Francisco Martín Rico</title>
    <meta name=\"description\" content=\"Publications by Francisco Martín Rico (selected papers, journals, conferences, books) with DOI and BibTeX links.\" />
  <meta name=\"robots\" content=\"index, follow\" />
    <meta name=\"author\" content=\"Francisco Martín Rico\" />
    <link rel=\"canonical\" href=\"https://fmrico.github.io/publications.html\" />
    <link rel=\"icon\" href=\"img/fmrico.png\" type=\"image/png\" />

    <meta property=\"og:site_name\" content=\"Francisco Martín Rico\" />
    <meta property=\"og:type\" content=\"website\" />
    <meta property=\"og:title\" content=\"Publications – Francisco Martín Rico\" />
    <meta property=\"og:description\" content=\"Publications by Francisco Martín Rico with DOI and BibTeX links.\" />
    <meta property=\"og:url\" content=\"https://fmrico.github.io/publications.html\" />
    <meta property=\"og:image\" content=\"https://fmrico.github.io/img/fmrico.png\" />

    <meta name=\"twitter:card\" content=\"summary\" />
    <meta name=\"twitter:title\" content=\"Publications – Francisco Martín Rico\" />
    <meta name=\"twitter:description\" content=\"Publications by Francisco Martín Rico with DOI and BibTeX links.\" />
    <meta name=\"twitter:image\" content=\"https://fmrico.github.io/img/fmrico.png\" />
    <meta name=\"twitter:site\" content=\"@fmrico\" />

    <script type=\"application/ld+json\">
    {{
        \"@context\": \"https://schema.org\",
        \"@type\": \"CollectionPage\",
        \"name\": \"Publications – Francisco Martín Rico\",
        \"url\": \"https://fmrico.github.io/publications.html\",
        \"about\": {{
            \"@type\": \"Person\",
            \"name\": \"Francisco Martín Rico\",
            \"url\": \"https://fmrico.github.io/\"
        }}
    }}
    </script>
  <link rel=\"stylesheet\" href=\"styles.css\" />
</head>
<body>

<header>
  <div class=\"topbar\">
    <div class=\"brand\">
      <p class=\"brand-title\">Francisco Martín Rico</p>
      <p class=\"brand-subtitle\">Academic homepage</p>
    </div>
    <nav class=\"nav\" aria-label=\"Primary\">
      <a href=\"index.html\">Home</a>
      <a href=\"publications.html\" aria-current=\"page\">Publications</a>
      <a href=\"projects.html\">Projects</a>
    </nav>
  </div>
</header>

<main>
  <div class=\"hero\">
    <div>
      <h1>Publications</h1>
    </div>
  </div>

  <section>
    <h2>List</h2>
{pubs_html}  </section>
</main>

<footer>
  <div class=\"footer-inner\">
    <p>© {time.gmtime().tm_year} Francisco Martín Rico</p>
    <p class=\"muted\">Static HTML + CSS (GitHub Pages).</p>
  </div>
</footer>

</body>
</html>
"""

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(doc)


def main() -> None:
    s = _session()

    pubs = fetch_scholar_publications(s)
    print(f"Fetched {len(pubs)} publications from Scholar")

    enriched: list[EnrichedPub] = []

    for i, p in enumerate(pubs, start=1):
        ep = EnrichedPub(pub=p)

        # Link (publisher landing page) from Scholar citation details
        ep.link_url = fetch_scholar_external_link(s, p.scholar_url)

        # Crossref DOI + BibTeX + PDF (best-effort)
        cr = crossref_best_match(s, p.title, p.year)
        if cr:
            doi = cr.get("DOI")
            if doi:
                ep.doi = doi
                ep.doi_url = f"https://doi.org/{doi}"

            if isinstance(cr.get("link"), list):
                for link in cr.get("link"):
                    u = link.get("URL")
                    if not u:
                        continue
                    if u.lower().endswith(".pdf") or "pdf" in u.lower():
                        ep.pdf_url = u
                        break
                if not ep.pdf_url and cr.get("link"):
                    # Some records provide a PDF URL without an obvious content-type.
                    ep.pdf_url = cr.get("link")[0].get("URL")

        # BibTeX file generation
        bib_content = None
        bib_filename = None
        if ep.doi:
            bib_content = crossref_bibtex(s, ep.doi)
            bib_filename = f"{_sanitize_doi(ep.doi)}.bib"

        if not bib_content:
            # Minimal fallback BibTeX (keeps per-paper files even when DOI lookup fails)
            key = _make_id_fallback(p)
            bib_filename = f"{key}.bib"
            bib_content = (
                f"@misc{{{key},\n"
                f"  title={{{p.title}}},\n"
                f"  author={{{p.authors}}},\n"
                + (f"  year={{{p.year}}},\n" if p.year else "")
                + (f"  howpublished={{{p.venue}}},\n" if p.venue else "")
                + f"  note={{Google Scholar entry}}\n"
                f"}}\n"
            )

        ep.bib_filename = bib_filename
        write_bibtex_file(bib_filename, bib_content)

        enriched.append(ep)

        if i % 20 == 0:
            print(f"Enriched {i}/{len(pubs)}")

    pubs_html = build_publications_html(enriched)
    write_publications_page(pubs_html)

    print(f"Wrote {OUT_HTML}")
    print(f"Wrote BibTeX files in {BIB_DIR}")


if __name__ == "__main__":
    main()
