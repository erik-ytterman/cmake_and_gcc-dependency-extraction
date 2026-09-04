#!/usr/bin/env python3
"""Render the repo's Markdown docs (README, ALGORITHM, TUTORIAL) to
standalone, self-contained HTML for reading or printing.

    python3 util/export_docs.py [--out DIR] [--repo DIR]

Output (default DIR = /tmp/extract_closure):
    index.html      landing page linking the three, with the source commit
    README.html
    ALGORITHM.html
    TUTORIAL.html

Each file embeds its own CSS (no external assets), keeps the cross-document
links working (`TUTORIAL.md` -> `TUTORIAL.html`, `#anchors` resolved), and
carries an `@media print` stylesheet tuned for the browser's "Save as PDF".

Dependency: markdown-it-py. PDF is best-effort: if weasyprint, wkhtmltopdf,
pandoc or headless Chrome is on PATH, a `.pdf` is written next to each `.html`;
otherwise that step is skipped with a note.
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from markdown_it import MarkdownIt

DOCS = ["README.md", "ALGORITHM.md", "TUTORIAL.md", "GLOSSARY.md"]

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #f6f7f9;
  color: #1b1f24;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
}
main {
  max-width: 52rem;
  margin: 0 auto;
  padding: 3.5rem 1.5rem 6rem;
}
.docnav {
  max-width: 52rem;
  margin: 0 auto;
  padding: 1rem 1.5rem 0;
  font-size: .85rem;
  color: #57606a;
}
.docnav a { color: #57606a; margin-right: 1rem; text-decoration: none; }
.docnav a.active { color: #1b1f24; font-weight: 600; }
h1, h2, h3, h4 { line-height: 1.25; font-weight: 650; margin: 2.2em 0 .7em; }
h1 { font-size: 2rem; margin-top: 0; }
h2 { font-size: 1.5rem; padding-bottom: .3em; border-bottom: 1px solid #d8dee4; }
h3 { font-size: 1.2rem; }
h4 { font-size: 1rem; color: #3b434b; }
p, ul, ol { margin: 0 0 1em; }
li { margin: .25em 0; }
a { color: #0a63c9; }
hr { border: 0; border-top: 1px solid #d8dee4; margin: 2.5em 0; }
blockquote {
  margin: 1.4em 0;
  padding: .1em 1.1em;
  border-left: 4px solid #b9c1c9;
  background: #eef1f4;
  color: #3b434b;
  border-radius: 0 4px 4px 0;
}
blockquote > :first-child { margin-top: .6em; }
blockquote > :last-child { margin-bottom: .6em; }
code {
  font: .875em/1.5 ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
        "Liberation Mono", monospace;
  background: #eceff2;
  padding: .15em .4em;
  border-radius: 4px;
}
pre {
  background: #1f2428;
  color: #e6edf3;
  padding: 1rem 1.1rem;
  border-radius: 8px;
  overflow-x: auto;
  line-height: 1.5;
}
pre code { background: none; padding: 0; font-size: .84rem; color: inherit; }
table {
  border-collapse: collapse;
  width: 100%;
  margin: 1.4em 0;
  font-size: .92rem;
  display: block;
  overflow-x: auto;
}
th, td { border: 1px solid #d0d7de; padding: .5em .75em; text-align: left;
         vertical-align: top; }
thead th { background: #eef1f4; }
tbody tr:nth-child(even) { background: #f6f7f9; }
img { max-width: 100%; }
details { margin: 1.2em 0; padding: .6em 1em; background: #eef1f4;
          border-radius: 6px; }
summary { cursor: pointer; font-weight: 600; }

@media print {
  body { background: #fff; font-size: 11pt; }
  .docnav { display: none; }
  main { max-width: none; padding: 0; }
  h1, h2, h3, h4 { page-break-after: avoid; }
  pre, table, blockquote, details { page-break-inside: avoid; }
  pre { background: #f4f4f4; color: #111; border: 1px solid #ddd; }
  a { color: inherit; text-decoration: none; }
  h2 { border-bottom-color: #999; }
}
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<nav class="docnav">{nav}</nav>
<main>
{body}
</main>
</body>
</html>
"""


def slugify(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s-]+", "-", s)
    return s.strip("-")


def make_md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True})
    md.enable(["table", "strikethrough"])

    def link_open(tokens, idx, options, env):
        href = tokens[idx].attrGet("href")
        if href:
            href = re.sub(r"([\w.-]+)\.md(#|$)",
                          lambda m: f"{m.group(1)}.html{m.group(2)}", href)
            tokens[idx].attrSet("href", href)
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["link_open"] = link_open
    return md


def render_body(md: MarkdownIt, text: str) -> str:
    tokens = md.parse(text)
    seen: dict[str, int] = {}
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            slug = slugify(tokens[i + 1].content)  # the inline token that follows
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            tok.attrSet("id", f"{slug}-{n}" if n else slug)  # GitHub-style dedup
    return md.renderer.render(tokens, md.options, {})


def nav_html(current: str) -> str:
    parts = ['<a href="index.html">index</a>']
    for d in DOCS:
        name = d[:-3]
        cls = ' class="active"' if d == current else ""
        parts.append(f'<a href="{name}.html"{cls}>{name}</a>')
    return "".join(parts)


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def try_pdf(html_files: list[Path]) -> str:
    """Best-effort HTML->PDF. Returns a status string."""
    try:
        import weasyprint  # noqa: F401
        for f in html_files:
            weasyprint.HTML(str(f)).write_pdf(str(f.with_suffix(".pdf")))
        return f"PDF: weasyprint -> {len(html_files)} file(s)"
    except Exception:
        pass

    for tool, cmd in (
        ("wkhtmltopdf", lambda f: ["wkhtmltopdf", "-q", str(f),
                                   str(f.with_suffix(".pdf"))]),
        ("pandoc", lambda f: ["pandoc", str(f), "-o", str(f.with_suffix(".pdf"))]),
    ):
        if shutil.which(tool):
            ok = sum(subprocess.run(cmd(f), capture_output=True).returncode == 0
                     for f in html_files)
            if ok:
                return f"PDF: {tool} -> {ok}/{len(html_files)} file(s)"

    for chrome in ("chromium", "chromium-browser", "google-chrome",
                   "google-chrome-stable"):
        if shutil.which(chrome):
            ok = 0
            for f in html_files:
                r = subprocess.run(
                    [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                     f"--print-to-pdf={f.with_suffix('.pdf')}",
                     "--print-to-pdf-no-header", f.as_uri()],
                    capture_output=True)
                ok += r.returncode == 0
            if ok:
                return f"PDF: {chrome} headless -> {ok}/{len(html_files)} file(s)"

    return ("PDF: skipped (no weasyprint / wkhtmltopdf / pandoc / headless "
            "Chrome). Open the HTML and use the browser's \"Save as PDF\" -- "
            "the print stylesheet is tuned for it.")


def main() -> None:
    default_repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=default_repo,
                    help="repo root (default: the repo containing this script)")
    ap.add_argument("--out", type=Path, default=Path("/tmp/extract_closure"),
                    help="output directory (default: /tmp/extract_closure)")
    args = ap.parse_args()

    repo, out = args.repo.resolve(), args.out.resolve()
    missing = [d for d in DOCS if not (repo / d).is_file()]
    if missing:
        sys.exit(f"error: not found under {repo}: {', '.join(missing)}")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    md = make_md()
    html_files = []
    for d in DOCS:
        src = repo / d
        page = PAGE.format(
            title=html.escape(f"{d[:-3]} — extract_closure docs"),
            css=CSS, nav=nav_html(d),
            body=render_body(md, src.read_text(encoding="utf-8")))
        dest = out / f"{d[:-3]}.html"
        dest.write_text(page, encoding="utf-8")
        html_files.append(dest)
        print(f"  {d:14} -> {dest}")

    commit = git(repo, "rev-parse", "--short", "HEAD") or "(unknown)"
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    index_md = (
        "# extract_closure — documentation\n\n"
        f"Rendered from the repo Markdown on {stamp}.\n\n"
        f"Source commit: `{commit}`\n\n"
        "- [README](README.html) — quickstart\n"
        "- [TUTORIAL](TUTORIAL.html) — concepts and APIs, hands-on, "
        "with the traps\n"
        "- [ALGORITHM](ALGORITHM.html) — stage-by-stage reference\n")
    (out / "index.html").write_text(
        PAGE.format(title="extract_closure docs", css=CSS, nav=nav_html(""),
                    body=render_body(md, index_md)), encoding="utf-8")
    print(f"  {'index.html':14} -> {out / 'index.html'}")

    print()
    print(try_pdf(html_files))
    print(f"\nDone. Open: file://{out / 'index.html'}")


if __name__ == "__main__":
    main()
