#!/usr/bin/env python3
"""Build the visual worker-flow page from docs/worker-flow.md.

The Markdown is the single source of truth; the HTML is generated from it so the
two cannot drift (test_worker_flow_docs.py rebuilds it and compares).

    python3 scripts/build_worker_flow_html.py                 # writes docs/worker-flow.html and docs/index.html
    python3 scripts/build_worker_flow_html.py --fragment OUT  # also writes the Artifact-ready fragment

The full page loads Mermaid from a pinned CDN so it renders when opened from
disk or served from anywhere. The fragment has no diagram library: the Artifact
viewer renders <pre class="mermaid"> natively.

Standard library only.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "worker-flow.md"
TARGET = ROOT / "docs" / "worker-flow.html"
INDEX = ROOT / "docs" / "index.html"
REPO_URL = "https://github.com/SWARM-Media-Steaming/swarm-automation"
MERMAID_SRC = "https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.0/mermaid.min.js"
TITLE = "Worker Flow"

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700'
    '&family=JetBrains+Mono:wght@400;500&display=swap">'
)

STYLE = """
/* Layout: a reference document. Sticky section index on wide screens, one reading column,
   diagrams in their own scrollable frames so a large flowchart never widens the page. */
:root {
  --bg: #f5f8f9; --surface: #ffffff; --surface-2: #eaf0f2; --fg: #14232c; --muted: #52646e;
  --line: #d3dde2; --accent: #0a6c7e; --accent-ink: #ffffff; --code-bg: #e6eef1; --warn: #8a5a00;
  --font-body: "Instrument Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e171c; --surface: #152229; --surface-2: #1c2d36; --fg: #e2ebf0; --muted: #96a8b2;
    --line: #2a3d47; --accent: #5cc3d6; --accent-ink: #062831; --code-bg: #1d2e37; --warn: #e0b04a;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0e171c; --surface: #152229; --surface-2: #1c2d36; --fg: #e2ebf0; --muted: #96a8b2;
  --line: #2a3d47; --accent: #5cc3d6; --accent-ink: #062831; --code-bg: #1d2e37; --warn: #e0b04a;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font-family: var(--font-body); font-size: 16px; line-height: 1.6;
  padding-block: 0 4rem; padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); margin: 0; }
a { color: var(--accent); }
code { font-family: var(--font-mono); font-size: .86em; background: var(--code-bg); padding: .1em .35em; border-radius: 4px;
  overflow-wrap: anywhere; }
.wrap { max-width: 78rem; margin: 0 auto; }
.masthead { padding-block: 2.5rem 1.5rem; border-bottom: 1px solid var(--line); margin-bottom: 1.5rem; }
.masthead h1 { font-size: clamp(1.9rem, 4vw, 2.7rem); line-height: 1.1; margin: 0 0 .6rem; letter-spacing: -.02em;
  text-wrap: balance; font-weight: 700; }
.masthead p { margin: 0; max-width: 46rem; color: var(--muted); font-size: 1.05rem; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr); gap: 2rem; }
@media (min-width: 62rem) { .layout { grid-template-columns: 15rem minmax(0, 1fr); } }
nav.index { display: none; }
@media (min-width: 62rem) {
  nav.index { display: block; position: sticky; top: 1rem; align-self: start; max-height: calc(100vh - 2rem); overflow: auto; }
}
nav.index h2 { font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); margin: 0 0 .6rem; font-weight: 600; }
nav.index ol { list-style: none; margin: 0; padding: 0; display: grid; gap: .1rem; }
nav.index a { display: block; padding: .35rem .6rem; border-radius: 6px; text-decoration: none; color: var(--fg); font-size: .92rem; line-height: 1.35; }
nav.index a:hover, nav.index a:focus-visible { background: var(--surface-2); outline: none; }
nav.index a.current { background: var(--surface-2); color: var(--accent); font-weight: 600; }
main { min-width: 0; }
section.part { margin-bottom: 3.2rem; scroll-margin-top: 1rem; }
section.part > h2 { font-size: 1.55rem; line-height: 1.2; margin: 0 0 1rem; letter-spacing: -.01em; text-wrap: balance; font-weight: 650; }
h3 { font-size: 1.1rem; margin: 1.8rem 0 .6rem; font-weight: 650; }
p { margin: 0 0 1rem; max-width: 46rem; }
ul, ol { margin: 0 0 1.1rem; padding-left: 1.3rem; max-width: 46rem; display: grid; gap: .45rem; }
.callout { background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--accent);
  padding: .8rem 1rem; border-radius: 6px; margin: 0 0 1.4rem; max-width: 46rem; }
.callout p { margin: 0; }
.tablewrap { overflow-x: auto; margin: 0 0 1.4rem; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: .93rem; min-width: 34rem; }
th, td { text-align: left; padding: .55rem .8rem; vertical-align: top; border-bottom: 1px solid var(--line); }
th { font-size: .74rem; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); background: var(--surface-2); font-weight: 600; }
tr:last-child td { border-bottom: 0; }
td:first-child code { white-space: nowrap; }
figure.diagram { margin: 0 0 1.6rem; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
figure.diagram .bar { display: flex; justify-content: space-between; align-items: center; gap: .6rem; padding: .45rem .8rem;
  background: var(--surface-2); border-bottom: 1px solid var(--line); font-size: .78rem; color: var(--muted); }
figure.diagram .bar b { color: var(--fg); font-weight: 600; letter-spacing: .02em; }
figure.diagram button { font: inherit; font-size: .78rem; color: var(--fg); background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; padding: .2rem .6rem; cursor: pointer; }
figure.diagram button:hover { border-color: var(--accent); }
figure.diagram button:focus-visible, nav.index a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
figure.diagram .scroll { overflow: auto; padding: 1rem; max-height: 80vh; }
figure.diagram .zoomer { width: calc(100% * var(--zoom, 1)); }
figure.diagram pre.mermaid { margin: 0; display: block; width: 100%; font-family: var(--font-body); background: transparent; }
figure.diagram pre.mermaid svg { display: block; width: 100%; max-width: none !important; height: auto; }
figure.diagram .zoom { display: flex; align-items: center; gap: .35rem; }
figure.diagram .zoom .level { min-width: 3.2em; text-align: center; font-variant-numeric: tabular-nums; }
.contents { margin: 0 0 1.4rem; }
.contents h2 { font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); margin: 0 0 .5rem; font-weight: 600; }
@media (min-width: 62rem) { .contents { display: none; } }
pre.code { background: var(--code-bg); padding: .8rem 1rem; border-radius: 8px; overflow-x: auto; font-family: var(--font-mono); font-size: .86rem; }
@media (prefers-reduced-motion: no-preference) { nav.index a { transition: background .12s; } }
"""

SCRIPT = """
(function () {
  var levels = [1, 1.5, 2, 3];
  Array.prototype.forEach.call(document.querySelectorAll('figure.diagram'), function (figure) {
    var index = 0;
    var label = figure.querySelector('.level');
    function apply() {
      figure.style.setProperty('--zoom', String(levels[index]));
      label.textContent = Math.round(levels[index] * 100) + '%';
    }
    figure.addEventListener('click', function (event) {
      var button = event.target.closest ? event.target.closest('button[data-z]') : null;
      if (!button) { return; }
      var action = button.getAttribute('data-z');
      if (action === 'in') { index = Math.min(levels.length - 1, index + 1); }
      else if (action === 'out') { index = Math.max(0, index - 1); }
      else { index = 0; }
      apply();
    });
  });
  var links = document.querySelectorAll('nav.index a');
  var parts = document.querySelectorAll('section.part');
  if ('IntersectionObserver' in window && parts.length) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        Array.prototype.forEach.call(links, function (link) {
          link.classList.toggle('current', link.getAttribute('href') === '#' + entry.target.id);
        });
      });
    }, { rootMargin: '-10% 0px -80% 0px' });
    Array.prototype.forEach.call(parts, function (part) { observer.observe(part); });
  }
})();
"""

MERMAID_INIT = """
(function () {
  if (!window.mermaid) { return; }
  var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  window.mermaid.initialize({ startOnLoad: true, securityLevel: 'strict', theme: dark ? 'dark' : 'default',
    flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: false } });
})();
"""


def slug(text: str) -> str:
    text = re.sub(r"[`*]", "", text).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def inline(text: str) -> str:
    """Escape, then apply code spans, bold and links."""
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        escaped = html.escape(part, quote=False)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', escaped)
        out.append(escaped)
    return "".join(out)


def split_row(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip())
    return [c.strip().replace("\\|", "|") for c in cells[1:-1]]


class Doc:
    """Markdown subset used by docs/worker-flow.md, rendered to sectioned HTML."""

    def __init__(self, source: str):
        self.title = TITLE
        self.intro = ""
        self.nav: list[tuple[str, str]] = []
        self.diagrams = 0
        self.body = self.render(source.splitlines())

    def diagram(self, code: str, heading: str) -> str:
        self.diagrams += 1
        label = heading or f"Diagram {self.diagrams}"
        return (
            f'<figure class="diagram"><div class="bar"><b>{html.escape(label)}</b>'
            '<div class="zoom"><button type="button" data-z="out" aria-label="Zoom out">\u2212</button>'
            '<span class="level" aria-live="polite">100%</span>'
            '<button type="button" data-z="in" aria-label="Zoom in">+</button>'
            '<button type="button" data-z="fit">Fit</button></div></div>'
            f'<div class="scroll"><div class="zoomer"><pre class="mermaid">{html.escape(code)}</pre></div></div></figure>'
        )

    def render(self, lines: list[str]) -> str:
        out: list[str] = []
        i, heading, in_section = 0, "", False
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                out.append(f"<p>{inline(' '.join(paragraph))}</p>")
                paragraph.clear()

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("```"):
                flush()
                language = stripped[3:].strip()
                block = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    block.append(lines[i])
                    i += 1
                code = "\n".join(block)
                out.append(self.diagram(code, heading) if language == "mermaid"
                           else f'<pre class="code">{html.escape(code)}</pre>')
            elif re.match(r"^#{1,3} ", line):
                flush()
                level = len(line) - len(line.lstrip("#"))
                text = line[level:].strip()
                if level == 1:
                    self.title = re.sub(r"`", "", text)
                elif level == 2:
                    if in_section:
                        out.append("</section>")
                    ident = slug(text)
                    self.nav.append((ident, re.sub(r"`", "", text)))
                    out.append(f'<section class="part" id="{ident}"><h2>{inline(text)}</h2>')
                    in_section, heading = True, re.sub(r"`", "", text)
                else:
                    heading = re.sub(r"`", "", text)
                    out.append(f'<h3 id="{slug(text)}">{inline(text)}</h3>')
            elif stripped == "---" or not stripped:
                flush()
            elif stripped.startswith(">"):
                flush()
                quote = []
                while i < len(lines) and lines[i].strip().startswith(">"):
                    quote.append(lines[i].strip().lstrip(">").strip())
                    i += 1
                i -= 1
                out.append(f'<div class="callout"><p>{inline(" ".join(quote))}</p></div>')
            elif stripped.startswith("|"):
                flush()
                rows = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    rows.append(lines[i])
                    i += 1
                i -= 1
                head, body = split_row(rows[0]), [split_row(r) for r in rows[2:]]
                cells = "".join(f"<th>{inline(c)}</th>" for c in head)
                trs = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in body)
                out.append(f'<div class="tablewrap"><table><thead><tr>{cells}</tr></thead><tbody>{trs}</tbody></table></div>')
            elif re.match(r"^(-|\d+\.) ", stripped):
                flush()
                ordered = bool(re.match(r"^\d+\.", stripped))
                items: list[str] = []
                while i < len(lines):
                    current = lines[i]
                    if re.match(r"^(-|\d+\.) ", current.strip()) and not current.startswith("  "):
                        items.append(re.sub(r"^(-|\d+\.) ", "", current.strip()))
                    elif current.startswith("  ") and current.strip() and items:
                        items[-1] += " " + current.strip()
                    else:
                        break
                    i += 1
                i -= 1
                tag = "ol" if ordered else "ul"
                out.append(f"<{tag}>" + "".join(f"<li>{inline(item)}</li>" for item in items) + f"</{tag}>")
            else:
                if not self.intro and not in_section and not stripped.startswith("Contents"):
                    pass
                paragraph.append(stripped)
            i += 1
        flush()
        if in_section:
            out.append("</section>")
        return "\n".join(out)


def split_masthead(body: str) -> tuple[str, str]:
    """The introduction before the first section, and the sections."""
    marker = '<section class="part"'
    index = body.find(marker)
    return (body[:index], body[index:]) if index != -1 else (body, "")


def page_parts(source: str) -> tuple[str, str, str]:
    """(title, head fragment, body fragment) shared by the full page and the Artifact fragment."""
    doc = Doc(source)
    intro, sections = split_masthead(doc.body)
    intro_paragraphs = re.findall(r"<p>(.*?)</p>", intro, re.S)
    lead = intro_paragraphs[0] if intro_paragraphs else ""
    callouts = "".join(re.findall(r'<div class="callout">.*?</div>', intro, re.S))
    contents = "".join(re.findall(r"<ol>.*?</ol>", intro, re.S))
    nav = "".join(f'<li><a href="#{ident}">{html.escape(label)}</a></li>' for ident, label in doc.nav)
    head = (
        f"<title>{html.escape(TITLE)}</title>"
        '<meta name="claude:viewport-fit-sides" content="cover">'
        f"{FONTS}<style>{STYLE}</style>"
    )
    body = (
        '<div class="wrap">'
        f'<header class="masthead"><h1>{html.escape(doc.title)}</h1><p>{lead}</p></header>'
        f'<div class="layout"><nav class="index" aria-label="Sections"><h2>Sections</h2><ol>{nav}</ol></nav>'
        f'<main>{callouts}<div class="contents"><h2>Sections</h2>{contents}</div>{sections}</main></div></div>'
    )
    return TITLE, head, body + f"<script>{SCRIPT}</script>"


def build_full(source: str) -> str:
    _, head, body = page_parts(source)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f"{head}</head><body>{body}"
        f'<script src="{MERMAID_SRC}"></script><script>{MERMAID_INIT}</script></body></html>\n'
    )


def build_fragment(source: str) -> str:
    """Artifact-ready: no document skeleton and no diagram library (rendered natively)."""
    _, head, body = page_parts(source)
    return head + body + "\n"


INDEX_STYLE = """
.cards { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fill, minmax(min(100%, 19rem), 1fr)); margin: 0 0 2rem; padding: 0; list-style: none; max-width: none; }
.card { display: grid; gap: .35rem; align-content: start; height: 100%; padding: 1rem 1.1rem; background: var(--surface); border: 1px solid var(--line);
  border-radius: 10px; text-decoration: none; color: var(--fg); }
.card:hover, .card:focus-visible { border-color: var(--accent); outline: none; }
.card strong { font-size: 1.05rem; }
.card span { color: var(--muted); font-size: .92rem; }
.card.featured { border-left: 4px solid var(--accent); }
.kicker { font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); font-weight: 600; margin: 0 0 .6rem; }
"""


def doc_summary(path: Path) -> tuple[str, str]:
    """(title, one-line description) of a Markdown doc."""
    title, summary = path.stem.replace("-", " ").capitalize(), ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and title == path.stem.replace("-", " ").capitalize():
            title = re.sub(r"[`*]", "", stripped[2:]).strip()
        elif stripped and not stripped.startswith(("#", ">", "|", "-", "```", "<", "---")) and not summary:
            summary = re.sub(r"[`*]|\[([^\]]+)\]\([^)]*\)", lambda m: m.group(1) or "", stripped)
            summary = (summary[:157] + "...") if len(summary) > 160 else summary
        if summary and title != path.stem.replace("-", " ").capitalize():
            break
    return title, summary


def build_index() -> str:
    """The GitHub Pages landing page for docs/: the visual flow, then every other doc."""
    others = sorted(p for p in (ROOT / "docs").glob("*.md") if p.name != SOURCE.name)
    cards = "".join(
        f'<li><a class="card" href="{REPO_URL}/blob/main/docs/{html.escape(p.name)}">'
        f"<strong>{html.escape(doc_summary(p)[0])}</strong><span>{html.escape(doc_summary(p)[1])}</span></a></li>"
        for p in others
    )
    head = (
        "<title>Swarm Automation Docs</title>"
        '<meta name="claude:viewport-fit-sides" content="cover">'
        f"{FONTS}<style>{STYLE}{INDEX_STYLE}</style>"
    )
    body = (
        '<div class="wrap"><header class="masthead"><h1>Swarm Automation docs</h1>'
        "<p>How the issue worker picks up, works, attacks and delivers an issue, and the design notes behind it.</p></header>"
        '<p class="kicker">Start here</p><ul class="cards">'
        '<li><a class="card featured" href="worker-flow.html"><strong>Issue worker flow</strong>'
        "<span>Interactive diagrams: pickup, implementation, adversarial rounds and epochs, findings, delivery, holds.</span></a></li></ul>"
        f'<p class="kicker">Design notes</p><ul class="cards">{cards}</ul></div>'
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f"{head}</head><body>{body}</body></html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fragment", metavar="PATH", help="also write the Artifact-ready fragment here")
    parser.add_argument("--check", action="store_true", help="exit 1 when docs/worker-flow.html is out of date")
    args = parser.parse_args(argv)
    source = SOURCE.read_text(encoding="utf-8")
    full = build_full(source)
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        index = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
        if current != full or index != build_index():
            print("docs/worker-flow.html or docs/index.html is out of date; run scripts/build_worker_flow_html.py",
                  file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(full, encoding="utf-8")
    INDEX.write_text(build_index(), encoding="utf-8")
    (ROOT / "docs" / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}, {INDEX.relative_to(ROOT)}")
    if args.fragment:
        Path(args.fragment).write_text(build_fragment(source), encoding="utf-8")
        print(f"wrote {args.fragment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
