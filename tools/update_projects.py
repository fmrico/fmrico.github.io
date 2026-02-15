#!/usr/bin/env python3
"""Generate projects.html.

Competitive projects are parsed from URJC PDI (Proyectos tab):
https://servicios.urjc.es/pdi/ver/francisco.rico

Open source projects are currently a curated static list.

Output sections:
- Competitive projects (ongoing)
- Open source projects
- Competitive projects (past)

Cards mimic publications style; badge shows year interval.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
from dataclasses import dataclass

import requests

UA = "fmrico.github.io/1.0 (mailto:francisco.rico@urjc.es)"
URJC_URL = "https://servicios.urjc.es/pdi/ver/francisco.rico"
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT_HTML = os.path.join(ROOT_DIR, "projects.html")
PROJECT_LINKS_JSON = os.path.join(os.path.dirname(__file__), "project_links.json")


EXCLUDE_COMPETITIVE_TITLES = {
    # User curation (2026-02-15)
    "grupo de investigación de alto rendimiento en robótica inteligente de la urjc",
    "rld -coresense",
    "rld aiplan4eu - upf4ros",
    "línea de actuación nº 3. departamento. teoría de la señal y comunicaciones",
    "rld -robmosys - center for advanced training on robotics and open source (act-ros)",
    "rld mocap4ros2",
    "robmosys-mros rld",
}


FORCE_PAST_COMPETITIVE_TITLES = {
    # Force into past section regardless of end date
    "ciberseguridad y seguridad en arquitecturas cognitivas para robots",
}


OPEN_SOURCE = [
    ("EasyNav", "https://easynavigation.github.io/"),
    ("NavMap", "https://github.com/fmrico/NavMap"),
    ("PlanSys2", "https://plansys2.github.io/"),
    ("MOCAP4ROS2", "https://mocap4ros2-project.github.io/"),
    ("YAETS", "https://github.com/fmrico/yaets"),
    ("cascade_lifecycle", "https://github.com/fmrico/cascade_lifecycle"),
    ("Book ROS2", "https://github.com/fmrico/book_ros2"),
]


@dataclass
class CompetitiveProject:
    title: str
    start: str
    end: str
    funder: str
    pis: list[str]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def _strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _norm_title(s: str) -> str:
    s = html.unescape(s)
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .,:;\t\n\r")
    return s


def load_project_links() -> dict[str, str]:
    if not os.path.exists(PROJECT_LINKS_JSON):
        return {}
    try:
        with open(PROJECT_LINKS_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    if not isinstance(raw, dict):
        return {}

    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        out[_norm_title(k)] = v
    return out


def _field_value(panel: str, label: str) -> str:
    m = re.search(rf"<b>\s*{re.escape(label)}\s*:</b>\s*([^<]*)", panel, flags=re.S | re.I)
    return _strip_tags(m.group(1)) if m else ""


def _ul_after_label(panel: str, label: str) -> list[str]:
    # Find first <ul>...</ul> after a <b>label:</b>
    m = re.search(rf"<b>\s*{re.escape(label)}\s*:</b>\s*(?:</p>)?\s*<ul>(.*?)</ul>", panel, flags=re.S | re.I)
    if not m:
        return []
    ul = m.group(1)
    items = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", ul, flags=re.S | re.I):
        t = _strip_tags(li)
        if t:
            items.append(t)
    return items


def _parse_date(s: str) -> dt.date | None:
    s = s.strip()
    if not s:
        return None
    parts = s.split("/")
    if len(parts) != 3:
        return None
    d, m, y = parts
    y_i = int(y)
    if y_i < 100:
        y_i += 2000
    try:
        return dt.date(y_i, int(m), int(d))
    except Exception:
        return None


def _year_interval(start: str, end: str) -> str:
    s = _parse_date(start)
    e = _parse_date(end)
    if s and e:
        return f"{s.year}–{e.year}"
    if s and not e:
        return f"{s.year}–"
    if not s and e:
        return f"–{e.year}"
    return ""


def fetch_competitive_projects() -> list[CompetitiveProject]:
    s = _session()
    page = s.get(URJC_URL, timeout=30).text

    m = re.search(r'<div[^>]+id="tab_proyectos"[^>]*>(.*?)<div[^>]+id="tab_publicaciones"', page, flags=re.S)
    if not m:
        return []
    sec = m.group(1)

    panels = sec.split('<div class="panel panel-default">')
    out: list[CompetitiveProject] = []

    for chunk in panels[1:]:
        title_m = re.search(r'<a class="accordion-toggle"[^>]*>\s*(.*?)\s*</a>', chunk, flags=re.S)
        if not title_m:
            continue
        title = _strip_tags(title_m.group(1))

        start = _field_value(chunk, "Fecha inicio")
        end = _field_value(chunk, "Fecha fin")
        funder = _field_value(chunk, "Entidad financiadora")
        pis = _ul_after_label(chunk, "Investigador/es principal/es")

        if title:
            out.append(CompetitiveProject(title=title, start=start, end=end, funder=funder, pis=pis))

    # Keep order as URJC provides (appears most recent first)
    return out


def split_ongoing_past(projects: list[CompetitiveProject]) -> tuple[list[CompetitiveProject], list[CompetitiveProject]]:
    today = dt.date.today()
    ongoing: list[CompetitiveProject] = []
    past: list[CompetitiveProject] = []

    for p in projects:
        norm_title = _norm_title(p.title)
        if norm_title in FORCE_PAST_COMPETITIVE_TITLES:
            past.append(p)
            continue
        end = _parse_date(p.end)
        if end is None or end >= today:
            ongoing.append(p)
        else:
            past.append(p)

    return ongoing, past


def _esc(s: str) -> str:
    return html.escape(s)


def render_competitive_cards(projects: list[CompetitiveProject], project_links: dict[str, str]) -> str:
    parts: list[str] = []
    parts.append('<div class="proj-list">')
    for p in projects:
        if _norm_title(p.title) in EXCLUDE_COMPETITIVE_TITLES:
            continue
        interval = _year_interval(p.start, p.end)
        parts.append('  <article class="card proj-card">')
        parts.append('    <div class="proj-top">')
        parts.append(f'      <div class="proj-title">{_esc(p.title)}</div>')
        parts.append('      <div class="badges">')
        if interval:
            parts.append(f'        <span class="badge">{_esc(interval)}</span>')
        parts.append('      </div>')
        parts.append('    </div>')

        if p.pis:
            parts.append(f'    <p class="proj-meta"><strong>Principal Investigators:</strong> {_esc(", ".join(p.pis))}</p>')
        if p.funder:
            parts.append(f'    <p class="proj-meta"><strong>Funding entity:</strong> {_esc(p.funder)}</p>')

        web = project_links.get(_norm_title(p.title))
        if web:
            parts.append('    <div class="proj-links">')
            parts.append(f'      <a href="{html.escape(web, quote=True)}">Web</a>')
            parts.append('    </div>')
        parts.append('  </article>')
    parts.append('</div>')
    return "\n".join(parts)


def render_open_source_cards() -> str:
    parts: list[str] = []
    parts.append('<div class="proj-list">')
    for name, url in OPEN_SOURCE:
        parts.append('  <article class="card proj-card">')
        parts.append('    <div class="proj-top">')
        parts.append(f'      <div class="proj-title">{_esc(name)}</div>')
        parts.append('      <div class="badges"></div>')
        parts.append('    </div>')
        parts.append('    <div class="proj-links">')
        parts.append(f'      <a href="{html.escape(url, quote=True)}">Repo</a>')
        parts.append('    </div>')
        parts.append('  </article>')
    parts.append('</div>')
    return "\n".join(parts)


def write_projects_page(ongoing_html: str, oss_html: str, past_html: str) -> None:
    year = dt.date.today().year
    doc = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Projects – Francisco Martín Rico</title>
    <meta name=\"description\" content=\"Projects by Francisco Martín Rico (competitive research projects and open source software).\" />
  <meta name=\"robots\" content=\"index, follow\" />
    <meta name=\"author\" content=\"Francisco Martín Rico\" />
    <link rel=\"canonical\" href=\"https://fmrico.github.io/projects.html\" />
    <link rel=\"icon\" href=\"img/fmrico.png\" type=\"image/png\" />

    <meta property=\"og:site_name\" content=\"Francisco Martín Rico\" />
    <meta property=\"og:type\" content=\"website\" />
    <meta property=\"og:title\" content=\"Projects – Francisco Martín Rico\" />
    <meta property=\"og:description\" content=\"Competitive research projects and open source projects by Francisco Martín Rico.\" />
    <meta property=\"og:url\" content=\"https://fmrico.github.io/projects.html\" />
    <meta property=\"og:image\" content=\"https://fmrico.github.io/img/fmrico.png\" />

    <meta name=\"twitter:card\" content=\"summary\" />
    <meta name=\"twitter:title\" content=\"Projects – Francisco Martín Rico\" />
    <meta name=\"twitter:description\" content=\"Competitive research projects and open source projects by Francisco Martín Rico.\" />
    <meta name=\"twitter:image\" content=\"https://fmrico.github.io/img/fmrico.png\" />
    <meta name=\"twitter:site\" content=\"@fmrico\" />

    <script type=\"application/ld+json\">
    {{
        \"@context\": \"https://schema.org\",
        \"@type\": \"CollectionPage\",
        \"name\": \"Projects – Francisco Martín Rico\",
        \"url\": \"https://fmrico.github.io/projects.html\",
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
      <a href=\"publications.html\">Publications</a>
      <a href=\"projects.html\" aria-current=\"page\">Projects</a>
    </nav>
  </div>
</header>

<main>
  <div class=\"hero\">
    <div>
      <h1>Projects</h1>
    </div>
  </div>

  <section>
    <h2>Competitive projects (ongoing)</h2>
    {ongoing_html}
  </section>

  <section>
    <h2>Open source projects</h2>
    {oss_html}
  </section>

  <section>
    <h2>Competitive projects (past)</h2>
    {past_html}
  </section>
</main>

<footer>
  <div class=\"footer-inner\">
    <p>© {year} Francisco Martín Rico</p>
  </div>
</footer>

</body>
</html>
"""

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(doc)


def main() -> None:
    projects = fetch_competitive_projects()
    ongoing, past = split_ongoing_past(projects)

    project_links = load_project_links()

    ongoing_html = render_competitive_cards(ongoing, project_links)
    past_html = render_competitive_cards(past, project_links)
    oss_html = render_open_source_cards()

    write_projects_page(ongoing_html, oss_html, past_html)
    print(f"Wrote {OUT_HTML}")
    print(f"Competitive: {len(projects)} (ongoing {len(ongoing)}, past {len(past)})")
    print(f"Open source: {len(OPEN_SOURCE)}")


if __name__ == "__main__":
    main()
