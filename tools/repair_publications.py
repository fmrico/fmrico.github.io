#!/usr/bin/env python3
"""Repair and normalize publications.html.

- Fix broken markup inside .pub-links by rebuilding each pub-card block.
- Remove redundant "Link" when it is just the DOI URL.
- Fill the badge (Q1–Q4 / Class 1–2/3) using data from https://gsyc.urjc.es/~fmartin/
  (applied only up to year <= 2022; for 2023+ left for manual curation).

This script is dependency-free (requests only).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Optional
import os
import urllib.parse

import requests

FMARTIN_URL = "https://gsyc.urjc.es/~fmartin/"
UA = "fmrico.github.io/1.0 (mailto:francisco.rico@urjc.es)"


MANUAL_RANKS: dict[str, str] = {
    # Provided by user (2026-02-15)
    # Keyed by normalized title.
    "dynamic delegation of behavior trees to enhance cooperation in robot teams": "Q2",
    "towards a robotic intrusion prevention system combining security and safety in cognitive social robots": "Q1",
    "open source robot localization for nonplanar environments": "Q2",
    "a visual questioning answering approach to enhance robot localization in indoor environments": "Q3",
    "regulated pure pursuit for robot path tracking": "Q2",
    "an autonomous ground robot to support firefighters interventions in indoor emergencies": "Q2",
}


EXCLUDE_YEARS = {"", "Unknown", "1978", "1991"}


EXCLUDE_DOIS = {
    "10.1201/9781420031393-49",
    "10.4995/thesis/10251/38902",
    "10.5565/rev/tradumatica.7",
    "10.1109/crv.2009.18",
    "10.1109/icip.2009.5413736",
    "10.1007/3-540-45603-1_54",
    "10.1093/med/9780198779117.001.0001",
    "10.1109/conielecomp.2011.5749380",
    "10.5772/7351",
    "10.2307/j.ctv2s0jcdb.240",
    "10.1109/sice.2008.4655047",
    "10.25100/peu.680.cap8",
    "10.1201/9781003289623",
    "10.5821/dissertation-2117-363911",
    "10.15332/dt.inv.2020.01681",
    "10.1109/case56687.2023.10260363",
    "10.1109/isoirs65690.2025.11168047",
}


EXCLUDE_TEXT_SNIPPETS = [
    "una perspectiva de la inteligencia artificial en su 50 aniversario: campus",
    "robotica. unileon. es, 2007",
    "constraints 2, 3, 2013",
    "planning. wiki-the ai planning & pddl wiki",
    "leading with depth: the impact of emotions and relationships on leadership, 2023",
]


DOI_RANK_OVERRIDES: dict[str, str] = {
    "10.1017/s0263574708004414": "Q2",
}


@dataclass
class Card:
    year: str
    where: str
    cite: str
    doi: Optional[str]
    doi_url: Optional[str]
    scholar_url: Optional[str]
    bibtex_url: Optional[str]
    link_url: Optional[str]
    paper_url: Optional[str]
    badge: str


def _norm(s: str) -> str:
    s = html.unescape(s)
    s = s.casefold()
    s = re.sub(r"[“”\"']", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_text_for_filter(s: str) -> str:
    s = html.unescape(s or "")
    s = s.casefold()
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("–", "-")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _norm_doi_for_match(doi: str | None) -> str:
    if not doi:
        return ""
    d = html.unescape(doi).strip().lower()
    d = d.replace("https://doi.org/", "").replace("http://doi.org/", "")
    if d.startswith("0."):
        d = "10." + d[2:]
    return d


_MOJIBAKE_REPL = {
    # Common UTF-8 -> CP1252/Latin-1 mojibake
    "Ã¡": "á",
    "Ã©": "é",
    "Ã­": "í",
    "Ã³": "ó",
    "Ãº": "ú",
    "Ã±": "ñ",
    "Ã": "Á",
    "Ã": "É",
    "Ã": "Í",
    "Ã": "Ó",
    "Ã": "Ú",
    "Ã": "Ñ",
    "â": "–",
    "â": "—",
    "â": "“",
    "â": "”",
    "â": "’",
    # Specific observed cases in this site
    "MartÃn": "Martín",
    "MartÃ­n": "Martín",
    "Mart√≠n": "Martín",
    "MÃºzquiz": "Múzquiz",
    "Hernàndez": "Hernández",
}


def _fix_mojibake(s: str) -> str:
    if not s:
        return s
    out = s
    for bad, good in _MOJIBAKE_REPL.items():
        out = out.replace(bad, good)
    return out


def _should_exclude(card: "Card") -> bool:
    if (card.year or "") in EXCLUDE_YEARS:
        return True

    doi = _norm_doi_for_match(card.doi)
    if doi and doi in EXCLUDE_DOIS:
        return True

    text = _norm_text_for_filter(" ".join([card.where or "", card.cite or "", card.doi or ""]))
    for snip in EXCLUDE_TEXT_SNIPPETS:
        if snip in text:
            return True

    return False


def _clean_url(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip()
    # Remove accidental newlines/spaces inside URLs.
    u = re.sub(r"\s+", "", u)
    # Undo over-escaped entities (e.g., &amp;amp;).
    for _ in range(4):
        new_u = html.unescape(u)
        if new_u == u:
            break
        u = new_u
    return u


def _bibtex_path_from_href(href: str | None) -> str | None:
    if not href:
        return None
    # publications.html uses relative hrefs like bibtex/<file>.bib
    href = href.lstrip("/")
    if not href.startswith("bibtex/"):
        return None
    return os.path.join(os.path.dirname(__file__), "..", href)


def _parse_bibtex_authors(bibtex_text: str) -> list[str]:
    # Very small parser: extract the author field without swallowing following fields.
    # Crossref BibTeX can be single-line, so we stop at the first closing brace
    # that is followed by a comma (end of the author field): `author={...},`
    m = re.search(r"\bauthor\s*=\s*\{(.*?)\}\s*,", bibtex_text, flags=re.S | re.I)
    if not m:
        return []
    raw = m.group(1).strip()
    # Split by ' and ' respecting BibTeX semantics.
    parts = [p.strip() for p in raw.split(" and ") if p.strip()]
    out: list[str] = []
    for p in parts:
        # Convert "Family, Given" -> "Given Family" when possible.
        if "," in p:
            family, given = [x.strip() for x in p.split(",", 1)]
            if given:
                out.append(f"{given} {family}".strip())
            else:
                out.append(family)
        else:
            out.append(p)
    return out


def _authors_from_bibtex_href(bibtex_href: str | None) -> list[str]:
    path = _bibtex_path_from_href(bibtex_href)
    if not path or not os.path.exists(path):
        return []
    try:
        txt = open(path, "r", encoding="utf-8").read()
    except Exception:
        return []
    return _parse_bibtex_authors(txt)


def _crossref_best_match(title: str, year: str | None) -> dict | None:
    query = urllib.parse.quote(title)
    url = f"https://api.crossref.org/works?rows=5&query.bibliographic={query}"
    try:
        data = requests.get(url, timeout=30, headers={"User-Agent": UA}).json()
    except Exception:
        return None

    items = data.get("message", {}).get("items", [])
    if not items:
        return None

    want = _norm(title)

    def score(it: dict) -> float:
        cand_title = (it.get("title") or [""])[0]
        cand = _norm(cand_title)
        if not cand:
            return 0.0
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


def _authors_from_crossref(title: str, year: str | None) -> list[str]:
    item = _crossref_best_match(title, year)
    if not item:
        return []
    authors = item.get("author")
    if not isinstance(authors, list):
        return []
    out: list[str] = []
    for a in authors:
        if not isinstance(a, dict):
            continue
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = (given + " " + family).strip()
        if name:
            out.append(name)
    return out


def _format_author_list(names: list[str]) -> str:
    names = [re.sub(r"\s+", " ", n).strip() for n in names if n.strip()]
    return ", ".join(names)


def fetch_rank_map() -> dict[str, str]:
    r = requests.get(FMARTIN_URL, timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    t = r.text

    mapping: dict[str, str] = {}

    # The page renders entries like:
    # <p>[Journal Q1]<b>"Title"</b> Authors. Venue. 2022.</p>
    # or sometimes a linked title inside the bold section.
    entry_re = re.compile(
        r"\[(Journal\s+Q[1-4]|Journal|Class\s+[1-3])\]"  # label
        r".*?<b>\s*(?:&quot;|\")\s*(.*?)\s*(?:&quot;|\")\s*</b>",
        flags=re.S,
    )

    for m in entry_re.finditer(t):
        label = m.group(1).strip()
        raw_title = m.group(2).strip()
        raw_title = re.sub(r"<.*?>", "", raw_title)
        # If the title is like [Title](url), keep the visible title.
        raw_title = re.sub(r"\[(.*?)\]\([^)]*\)", r"\1", raw_title)
        title = raw_title.strip()

        badge = None
        if label.startswith("Journal Q"):
            badge = label.replace("Journal ", "")  # Q1..Q4
        elif label.startswith("Class"):
            badge = label
        else:
            badge = "Q?"

        key = _norm(title)
        if key and badge and badge != "Q?":
            mapping[key] = badge

    return mapping


def extract_title_from_cite(cite: str) -> str:
    # Prefer text between curly quotes: “…,”
    m = re.search(r"[“\"]([^”\"]+)[”\"]", cite)
    if m:
        title = m.group(1).strip()
        return title.strip(" ,.;")
    return cite.strip().strip(" ,.;")


def extract_authors_from_cite(cite: str) -> str:
    # Usually: "AUTHORS, “TITLE,” ..."
    m = re.search(r"^(.*?),\s*[“\"]", cite)
    if not m:
        return ""
    return m.group(1).strip()


def parse_cards(doc: str) -> list[Card]:
    cards: list[Card] = []

    for block in re.findall(r"<article class=\"card pub-card\">.*?</article>", doc, flags=re.S):
        where = re.search(r"<div class=\"pub-where\">(.*?)</div>", block, flags=re.S)
        cite = re.search(r"<p class=\"pub-cite\">(.*?)</p>", block, flags=re.S)
        year = re.search(r"<span class=\"badge\">(\d{4})</span>", block)
        badge = re.search(r"<span class=\"badge badge-muted\">(.*?)</span>", block, flags=re.S)

        doi = re.search(r"<p class=\"pub-doi\">\s*DOI:\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, flags=re.S)
        doi_url = _clean_url(doi.group(1)) if doi else None
        doi_txt = html.unescape(doi.group(2)).strip() if doi else None

        links = {
            m.group(2): _clean_url(m.group(1))
            for m in re.finditer(r"<a\s+href=\"([^\"]+)\"[^>]*>([^<]+)</a>", block)
        }

        cards.append(
            Card(
                year=year.group(1) if year else "",
                where=html.unescape(where.group(1)).strip() if where else "",
                cite=html.unescape(re.sub(r"\s+", " ", cite.group(1))).strip() if cite else "",
                doi=doi_txt,
                doi_url=doi_url,
                scholar_url=_clean_url(links.get("Scholar")),
                bibtex_url=_clean_url(links.get("BibTeX")),
                link_url=_clean_url(links.get("Link")),
                paper_url=_clean_url(links.get("Paper")),
                badge=html.unescape(badge.group(1)).strip() if badge else "Q?",
            )
        )

    return cards


def rebuild_card(card: Card, rank_map: dict[str, str]) -> str:
    card = Card(
        year=card.year,
        where=_fix_mojibake(card.where),
        cite=_fix_mojibake(card.cite),
        doi=_fix_mojibake(card.doi) if card.doi else None,
        doi_url=card.doi_url,
        scholar_url=card.scholar_url,
        bibtex_url=card.bibtex_url,
        link_url=card.link_url,
        paper_url=card.paper_url,
        badge=_fix_mojibake(card.badge),
    )

    badge_value = card.badge or "Q?"

    doi_norm = _norm_doi_for_match(card.doi)
    doi_override = DOI_RANK_OVERRIDES.get(doi_norm)
    if doi_override:
        badge_value = doi_override

    # Apply manual overrides first.
    title_for_rank = extract_title_from_cite(card.cite)
    manual = MANUAL_RANKS.get(_norm(title_for_rank))
    if manual:
        badge_value = manual

    # Apply rank map only for year <= 2022 (2023+ left for manual updates).
    if card.year.isdigit() and int(card.year) <= 2022:
        key = _norm(title_for_rank)
        if key in rank_map:
            badge_value = rank_map[key]

    if badge_value == "Q?":
        badge_value = ""

    link_url = card.link_url
    doi_url = card.doi_url

    # If we have a DOI and a non-DOI landing page, make the DOI hyperlink point
    # to that landing page and drop the separate "Link".
    if card.doi and doi_url and link_url and not link_url.startswith("https://doi.org/"):
        doi_url = link_url
        link_url = None

    # Remove redundant Link if it is just the DOI URL.
    if doi_url and link_url and link_url.strip() == doi_url.strip():
        link_url = None
    if doi_url and link_url and link_url.startswith("https://doi.org/"):
        # Still redundant if DOI is present (keep DOI line instead)
        link_url = None

    # Prefer PDF for paper when it looks like a PDF; otherwise fall back.
    paper_url = card.paper_url
    if not paper_url:
        paper_url = link_url or card.doi_url or card.scholar_url or "#"

    # Expand authors list (no ellipsis) using BibTeX first, then Crossref.
    title = title_for_rank
    authors_list = _authors_from_bibtex_href(card.bibtex_url)
    if not authors_list:
        authors_list = _authors_from_crossref(title, card.year if card.year else None)
    authors_list = [_fix_mojibake(a) for a in authors_list]

    cite_authors = _format_author_list(authors_list) if authors_list else extract_authors_from_cite(card.cite)
    if cite_authors:
        cite_authors = cite_authors.replace("...", "").strip(" ,")
    cite_line = card.cite
    if cite_authors and title:
        # Rebuild a consistent IEEE-like line with full authors.
        cite_line = f"{cite_authors}, “{title},” {card.where}.".strip()

    parts: list[str] = []
    parts.append('      <article class="card pub-card">')
    parts.append('        <div class="pub-top">')
    parts.append(f'          <div class="pub-where">{html.escape(card.where)}</div>')
    parts.append('          <div class="badges">')
    if card.year:
        parts.append(f'            <span class="badge">{html.escape(card.year)}</span>')
    if badge_value:
        parts.append(f'            <span class="badge badge-muted">{html.escape(badge_value)}</span>')
    parts.append('          </div>')
    parts.append('        </div>')
    parts.append(f'        <p class="pub-cite">{html.escape(cite_line)}</p>')
    if card.doi and doi_url:
        parts.append(
            '        <p class="pub-doi">DOI: '
            f'<a href="{html.escape(doi_url, quote=True)}">{html.escape(card.doi)}</a></p>'
        )

    parts.append('        <div class="pub-links">')
    if link_url:
        parts.append(f'          <a href="{html.escape(link_url, quote=True)}">Link</a>')
    if card.scholar_url:
        parts.append(f'          <a href="{html.escape(card.scholar_url, quote=True)}">Scholar</a>')
    if card.bibtex_url:
        parts.append(f'          <a href="{html.escape(card.bibtex_url, quote=True)}" download>BibTeX</a>')
    else:
        parts.append('          <a href="#">BibTeX</a>')
    parts.append(f'          <a href="{html.escape(paper_url, quote=True)}">Paper</a>')
    parts.append('          <a href="#">Video</a>')
    parts.append('        </div>')
    parts.append('      </article>')

    return "\n".join(parts)


def _render_list(cards: list[Card], rank_map: dict[str, str]) -> str:
    kept: list[Card] = [c for c in cards if not _should_exclude(c)]

    by_year: dict[str, list[str]] = {}
    for c in kept:
        if not c.year or not c.year.isdigit():
            continue
        if c.year in EXCLUDE_YEARS:
            continue
        by_year.setdefault(c.year, []).append(rebuild_card(c, rank_map))

    years_sorted = sorted(by_year.keys(), key=lambda y: int(y), reverse=True)
    parts: list[str] = []
    for y in years_sorted:
        items = by_year.get(y) or []
        if not items:
            continue
        parts.append(f'    <h3 class="pub-year">{html.escape(y)}</h3>')
        parts.append('    <div class="pub-list">')
        parts.append("\n".join(items))
        parts.append('    </div>')
    return "\n".join(parts) + "\n"


def main() -> None:
    path = "publications.html"
    doc = open(path, "r", encoding="utf-8").read()

    rank_map = fetch_rank_map()
    cards = parse_cards(doc)

    new_list = _render_list(cards, rank_map)
    m = re.search(r"(<section>\s*<h2>List</h2>\s*)(.*?)(\s*</section>)", doc, flags=re.S)
    if not m:
        raise SystemExit("Could not find publications List section")
    new_doc = doc[: m.start(2)] + new_list + doc[m.end(2) :]

    open(path, "w", encoding="utf-8").write(new_doc)

    removed = sum(1 for c in cards if _should_exclude(c))
    print(f"Parsed {len(cards)} cards")
    print(f"Removed {removed} cards (filters)")
    print(f"Kept {len(cards) - removed} cards")
    print(f"Rank map entries: {len(rank_map)}")


if __name__ == "__main__":
    main()
