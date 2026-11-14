"""In-app offline Markdown manual viewer for EPOC-X Studio."""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Callable, Optional

from PyQt6 import QtCore, QtGui, QtWebEngineCore

from epoc_direct.paths import get_repo_root
from .qt_common import apply_dark_titlebar, asset_path, ensure_qt_app, install_sigint_handler
from .web_common import BaseWebWindow, StudioWebPage

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*```([A-Za-z0-9_+-]*)\s*$")
_ALERT_RE = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$", re.IGNORECASE)
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_HR_RE = re.compile(r"^\s*(?:---+|===+|\*\*\*+)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)*\s*\|?\s*$")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_NON_SLUG_RE = re.compile(r"[^a-z0-9\s-]")
_WS_RE = re.compile(r"[\s-]+")


def _resolve_doc_path(rel_or_abs: str | Path = "README.md") -> Optional[Path]:
    """Locate a repository documentation file for editable and source runs."""
    p = Path(rel_or_abs)
    if p.is_absolute() and p.is_file():
        return p.resolve()

    candidates: list[Path] = []
    repo_root = get_repo_root()
    if repo_root is not None:
        candidates.append(repo_root / p)
    candidates.append(Path(__file__).resolve().parents[2] / p)
    candidates.append(asset_path(p.name))

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def _slugify(text: str, seen: dict[str, int]) -> str:
    plain = _CODE_SPAN_RE.sub(r"\1", text)
    plain = _LINK_RE.sub(r"\1", plain)
    plain = plain.replace("**", "").replace("*", "").strip().lower()
    slug = _NON_SLUG_RE.sub("", plain)
    slug = _WS_RE.sub("-", slug).strip("-") or "section"
    count = seen.get(slug, 0)
    seen[slug] = count + 1
    return slug if count == 0 else f"{slug}-{count}"


def _format_inline(text: str) -> str:
    """Convert inline Markdown spans (code, links, bold, italic) to safe HTML."""
    code_tokens: list[str] = []
    link_tokens: list[str] = []

    def _stash_code(m: re.Match[str]) -> str:
        idx = len(code_tokens)
        code_tokens.append(f"<code>{html.escape(m.group(1), quote=True)}</code>")
        return f"\x00C{idx}\x00"

    def _stash_link(m: re.Match[str]) -> str:
        idx = len(link_tokens)
        label_escaped = html.escape(m.group(1), quote=True)
        label_escaped = _BOLD_RE.sub(r"<strong>\1</strong>", label_escaped)
        label_escaped = _ITALIC_RE.sub(r"<em>\1</em>", label_escaped)
        href = html.escape(m.group(2).strip(), quote=True)
        link_tokens.append(f'<a href="{href}">{label_escaped}</a>')
        return f"\x00L{idx}\x00"

    stashed = _CODE_SPAN_RE.sub(_stash_code, text)
    stashed = _LINK_RE.sub(_stash_link, stashed)
    escaped = html.escape(stashed, quote=True)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)

    for idx, val in enumerate(link_tokens):
        escaped = escaped.replace(f"\x00L{idx}\x00", val)
    for idx, val in enumerate(code_tokens):
        escaped = escaped.replace(f"\x00C{idx}\x00", val)
    return escaped


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_table_start(lines: list[str], idx: int) -> bool:
    """Return True when ``lines[idx]`` starts a Markdown pipe table."""
    return (
        idx + 1 < len(lines)
        and "|" in lines[idx]
        and bool(_TABLE_SEP_RE.match(lines[idx + 1]))
    )


def _parse_table(lines: list[str], start_idx: int, min_indent: int = 0) -> tuple[str, int]:
    """Parse a Markdown pipe table starting at ``start_idx`` into HTML."""
    n = len(lines)
    headers = _split_table_row(lines[start_idx])
    i = start_idx + 2
    rows: list[list[str]] = []
    while i < n:
        cur = lines[i]
        if not cur.strip() or "|" not in cur or _FENCE_RE.match(cur):
            break
        if min_indent and (len(cur) - len(cur.lstrip(" "))) < min_indent:
            break
        rows.append(_split_table_row(cur))
        i += 1
    thead = "".join(f"<th>{_format_inline(h)}</th>" for h in headers)
    tbody_rows = []
    for r in rows:
        padded = r + [""] * max(0, len(headers) - len(r))
        cells = "".join(f"<td>{_format_inline(c)}</td>" for c in padded[: len(headers)])
        tbody_rows.append(f"<tr>{cells}</tr>")
    table_html = (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(tbody_rows)}</tbody>"
        "</table></div>"
    )
    return table_html, i


def _markdown_to_html(md_text: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Render Markdown to HTML body and extract ``(level, slug, plain_title)`` TOC entries."""
    lines = md_text.splitlines()
    n = len(lines)
    i = 0
    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    seen_slugs: dict[str, int] = {}

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            lang = fence_m.group(1).strip().lower()
            i += 1
            code_lines: list[str] = []
            while i < n and not _FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1
            code_body = html.escape("\n".join(code_lines), quote=False)
            lang_badge = f'<span class="code-lang">{html.escape(lang)}</span>' if lang else "<span></span>"
            out.append(
                '<div class="code-block">'
                f'<div class="code-head">{lang_badge}'
                '<button type="button" class="copy-btn" onclick="copyCode(this)">Copy</button></div>'
                f"<pre><code>{code_body}</code></pre></div>"
            )
            continue

        # Headings
        head_m = _HEADING_RE.match(line)
        if head_m:
            level = len(head_m.group(1))
            raw_title = head_m.group(2).strip()
            slug = _slugify(raw_title, seen_slugs)
            if level in (2, 3):
                plain_title = _CODE_SPAN_RE.sub(r"\1", raw_title)
                plain_title = _LINK_RE.sub(r"\1", plain_title).replace("**", "").replace("*", "")
                toc.append((level, slug, plain_title))
            out.append(f'<h{level} id="{slug}">{_format_inline(raw_title)}</h{level}>')
            i += 1
            continue

        # Horizontal rule
        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        # Blockquotes and GitHub-style alerts
        if stripped.startswith(">"):
            bq_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                content = lines[i].strip()[1:]
                if content.startswith(" "):
                    content = content[1:]
                bq_lines.append(content)
                i += 1
            alert_m = _ALERT_RE.match(bq_lines[0].strip()) if bq_lines else None
            if alert_m:
                kind = alert_m.group(1).upper()
                body_html = "<br>".join(_format_inline(x) for x in bq_lines[1:] if x.strip())
                out.append(
                    f'<div class="alert alert-{kind.lower()}">'
                    f'<div class="alert-title">{kind}</div>'
                    f'<div class="alert-body">{body_html}</div></div>'
                )
            else:
                body_html = "<br>".join(_format_inline(x) for x in bq_lines if x.strip())
                out.append(f"<blockquote>{body_html}</blockquote>")
            continue

        # Markdown table
        if _is_table_start(lines, i):
            table_html, i = _parse_table(lines, i)
            out.append(table_html)
            continue

        # Unordered or ordered lists (including 1-level nested items)
        ul_m = _UL_RE.match(line)
        ol_m = _OL_RE.match(line)
        if ul_m or ol_m:
            list_html, i = _parse_list(lines, i)
            out.append(list_html)
            continue

        # Paragraph
        para_lines: list[str] = []
        while i < n:
            cur = lines[i]
            cur_s = cur.strip()
            if (
                not cur_s
                or _HEADING_RE.match(cur)
                or _FENCE_RE.match(cur)
                or _HR_RE.match(cur)
                or cur_s.startswith(">")
                or _UL_RE.match(cur)
                or _OL_RE.match(cur)
                or _is_table_start(lines, i)
            ):
                break
            para_lines.append(cur_s)
            i += 1
        out.append(f"<p>{_format_inline(' '.join(para_lines))}</p>")

    return "\n".join(out), toc


def _parse_list(lines: list[str], start_idx: int, min_indent: int = 0) -> tuple[str, int]:
    """Parse an ordered or unordered Markdown list with nested sublists and code blocks."""
    n = len(lines)
    first_line = lines[start_idx]
    first_ul = _UL_RE.match(first_line)
    first_ol = _OL_RE.match(first_line)
    is_ordered = first_ol is not None and first_ul is None
    base_indent = len((first_ol or first_ul).group(1))  # type: ignore[union-attr]
    tag = "ol" if is_ordered else "ul"
    items: list[str] = []
    i = start_idx

    while i < n:
        line = lines[i]
        if not line.strip():
            # Blank line ends list unless next non-blank line is another item at >= base_indent
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                nm_ul = _UL_RE.match(lines[j])
                nm_ol = _OL_RE.match(lines[j])
                nm = nm_ul or nm_ol
                if nm and len(nm.group(1)) >= base_indent:
                    i = j
                    continue
            break

        ul_m = _UL_RE.match(line)
        ol_m = _OL_RE.match(line)
        cur_m = ul_m or ol_m
        if not cur_m:
            break
        cur_indent = len(cur_m.group(1))
        if cur_indent < base_indent or (cur_indent == base_indent and bool(ol_m) != is_ordered):
            break
        if cur_indent > base_indent:
            # Nested list attached to previous item
            sub_html, i = _parse_list(lines, i, min_indent=cur_indent)
            if items:
                items[-1] = items[-1][:-5] + sub_html + "</li>"
            continue

        item_parts = [_format_inline(cur_m.group(2).strip())]
        i += 1
        # Collect indented continuation lines, nested lists, or indented code blocks
        while i < n:
            nxt = lines[i]
            if not nxt.strip():
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and (len(lines[j]) - len(lines[j].lstrip(" "))) > base_indent:
                    i = j
                    continue
                break
            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
            if nxt_indent <= base_indent:
                break
            nxt_fence = _FENCE_RE.match(nxt)
            if nxt_fence:
                lang = nxt_fence.group(1).strip().lower()
                i += 1
                c_lines: list[str] = []
                while i < n and not _FENCE_RE.match(lines[i]):
                    c_lines.append(lines[i].strip() if len(lines[i]) > nxt_indent else lines[i])
                    i += 1
                if i < n:
                    i += 1
                lang_badge = f'<span class="code-lang">{html.escape(lang)}</span>' if lang else "<span></span>"
                item_parts.append(
                    '<div class="code-block">'
                    f'<div class="code-head">{lang_badge}'
                    '<button type="button" class="copy-btn" onclick="copyCode(this)">Copy</button></div>'
                    f"<pre><code>{html.escape(chr(10).join(c_lines), quote=False)}</code></pre></div>"
                )
                continue
            if _is_table_start(lines, i):
                table_html, i = _parse_table(lines, i, min_indent=base_indent + 1)
                item_parts.append(table_html)
                continue
            if _UL_RE.match(nxt) or _OL_RE.match(nxt):
                sub_html, i = _parse_list(lines, i, min_indent=nxt_indent)
                item_parts.append(sub_html)
                continue
            item_parts.append(" " + _format_inline(nxt.strip()))
            i += 1

        items.append(f"<li>{''.join(item_parts)}</li>")

    return f"<{tag}>{''.join(items)}</{tag}>", i


def _build_toc_html(toc: list[tuple[int, str, str]]) -> str:
    """Render sidebar TOC with level-3 subheadings grouped and collapsed under level-2 parents."""
    groups: list[tuple[Optional[tuple[str, str]], list[tuple[str, str]]]] = []
    for lvl, slug, title in toc:
        if lvl == 2:
            groups.append(((slug, title), []))
        else:
            if groups:
                groups[-1][1].append((slug, title))
            else:
                groups.append((None, [(slug, title)]))

    parts: list[str] = []
    for parent, children in groups:
        if parent is None:
            for slug, title in children:
                parts.append(f'<a class="toc-link toc-l3" href="#{slug}">{html.escape(title)}</a>')
            continue

        slug, title = parent
        if not children:
            parts.append(f'<a class="toc-link toc-l2" href="#{slug}">{html.escape(title)}</a>')
            continue

        child_links = "\n".join(
            f'<a class="toc-link toc-l3" href="#{c_slug}">{html.escape(c_title)}</a>'
            for c_slug, c_title in children
        )
        parts.append(
            '<div class="toc-group">'
            f'<a class="toc-link toc-l2" href="#{slug}" aria-expanded="false">{html.escape(title)}</a>'
            f'<div class="toc-children">{child_links}</div>'
            "</div>"
        )
    return "\n".join(parts)


def build_manual_html(
    doc_path: Path,
    *,
    is_subdoc: bool = False,
) -> str:
    """Wrap a Markdown file in Studio's dark HTML shell with TOC and on-demand find."""
    md_text = doc_path.read_text(encoding="utf-8", errors="replace")
    body_html, toc = _markdown_to_html(md_text)

    toc_links = _build_toc_html(toc)
    back_btn = (
        '<button type="button" class="back-btn" id="backHomeBtn">&larr; Back to User Manual</button>'
        if is_subdoc
        else ""
    )
    doc_label = html.escape(doc_path.name)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>EPOC-X Studio — {doc_label}</title>
<style>
  :root {{
    --bg: #080d18;
    --panel: #0f172a;
    --row: #131d31;
    --surface: #162033;
    --surface-hover: #1e2d47;
    --border: #1e293b;
    --border-bright: #2b3d5c;
    --text: #e2e8f0;
    --text-bright: #f8fafc;
    --muted: #94a3b8;
    --caption: #64748b;
    --accent: #38bdf8;
    --good: #22c55e;
    --warn: #eab308;
    --bad: #ef4444;
    --font-sans: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif;
    --font-mono: Consolas, "Courier New", monospace;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.65 var(--font-sans);
    scroll-behavior: smooth;
  }}
  ::-webkit-scrollbar {{ width: 9px; height: 9px; }}
  ::-webkit-scrollbar-track {{ background: var(--bg); }}
  ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: #334155; }}

  .layout {{
    display: flex;
    min-height: 100vh;
  }}
  .sidebar {{
    width: 260px;
    flex-shrink: 0;
    background: var(--panel);
    border-right: 1px solid var(--border);
    padding: 16px 14px;
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .sidebar-head {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .brand {{
    color: var(--accent);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
  }}
  .sidebar-close-btn {{
    background: transparent;
    color: var(--muted);
    border: 1px solid transparent;
    border-radius: 5px;
    width: 24px;
    height: 24px;
    font: 600 15px/1 var(--font-sans);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: background .12s, color .12s, border-color .12s;
  }}
  .sidebar-close-btn:hover {{
    background: var(--surface);
    color: var(--text-bright);
    border-color: var(--border-bright);
  }}
  .sidebar-open-btn {{
    position: fixed;
    top: 14px;
    left: 14px;
    z-index: 900;
    display: none;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    background: var(--panel);
    color: var(--muted);
    border: 1px solid var(--border-bright);
    border-radius: 7px;
    font: 600 14px/1 var(--font-sans);
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
    transition: background .12s, color .12s, border-color .12s;
  }}
  .sidebar-open-btn:hover {{
    background: var(--surface-hover);
    color: var(--accent);
    border-color: var(--accent);
  }}
  .layout.sidebar-collapsed .sidebar {{
    display: none;
  }}
  .layout.sidebar-collapsed .sidebar-open-btn {{
    display: inline-flex;
  }}
  .layout.sidebar-collapsed .content-wrap {{
    padding-left: 54px;
  }}
  .find-bar {{
    position: fixed;
    top: 14px;
    right: 22px;
    z-index: 999;
    display: none;
    align-items: center;
    gap: 6px;
    padding: 6px 8px;
    background: var(--panel);
    border: 1px solid var(--border-bright);
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }}
  .find-bar.open {{
    display: flex;
  }}
  .find-input {{
    width: 210px;
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border-bright);
    border-radius: 5px;
    padding: 4px 8px;
    font: 12.5px var(--font-sans);
    outline: none;
  }}
  .find-input:focus {{
    border-color: var(--accent);
  }}
  .find-count {{
    min-width: 44px;
    text-align: center;
    font: 600 11.5px var(--font-mono);
    color: var(--muted);
    user-select: none;
  }}
  .find-count.empty {{
    color: var(--bad);
  }}
  .find-btn {{
    background: var(--surface);
    color: var(--muted);
    border: 1px solid var(--border-bright);
    border-radius: 5px;
    padding: 3px 7px;
    font: 600 11px var(--font-sans);
    cursor: pointer;
    line-height: 1.2;
  }}
  .find-btn:hover {{
    background: var(--surface-hover);
    color: var(--text-bright);
    border-color: var(--accent);
  }}
  .toc-nav {{
    display: flex;
    flex-direction: column;
    gap: 3px;
  }}
  .toc-group {{
    display: flex;
    flex-direction: column;
    gap: 2px;
  }}
  .toc-children {{
    display: none;
    flex-direction: column;
    gap: 2px;
    padding-left: 14px;
  }}
  .toc-group.open > .toc-children {{
    display: flex;
  }}
  .toc-link {{
    display: block;
    color: var(--muted);
    text-decoration: none;
    font-size: 12.5px;
    line-height: 1.35;
    padding: 5px 8px;
    border-radius: 6px;
    border-left: 2px solid transparent;
    transition: background .12s, color .12s, border-color .12s;
  }}
  .toc-link:hover,
  .toc-link:focus,
  .toc-link:active {{
    background: var(--surface);
    color: var(--text-bright);
    text-decoration: none;
  }}
  .toc-link.active {{
    background: rgba(56, 189, 248, 0.14);
    color: var(--accent);
    border-left-color: var(--accent);
  }}
  .toc-l3 {{
    padding-left: 10px;
    font-size: 12px;
  }}
  .back-btn {{
    width: 100%;
    background: var(--surface);
    color: var(--accent);
    border: 1px solid var(--border-bright);
    border-radius: 6px;
    padding: 6px 10px;
    font: 600 12.5px var(--font-sans);
    cursor: pointer;
    text-align: left;
  }}
  .back-btn:hover {{
    background: var(--surface-hover);
    border-color: var(--accent);
  }}

  .content-wrap {{
    flex: 1;
    min-width: 0;
    padding: 24px 36px 56px;
  }}
  .doc {{
    max-width: 860px;
    margin: 0 auto;
  }}
  h1, h2, h3, h4, h5, h6 {{
    color: var(--text-bright);
    line-height: 1.3;
    scroll-margin-top: 18px;
    text-decoration: none;
  }}
  h1:hover, h2:hover, h3:hover, h4:hover, h5:hover, h6:hover,
  h1 a:hover, h2 a:hover, h3 a:hover, h4 a:hover, h5 a:hover, h6 a:hover {{
    text-decoration: none;
  }}
  h1 {{
    font-size: 26px;
    margin: 0 0 14px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }}
  h2 {{
    font-size: 19px;
    margin: 28px 0 10px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
    color: var(--accent);
  }}
  h3 {{
    font-size: 15.5px;
    margin: 20px 0 8px;
  }}
  p {{
    margin: 0 0 12px;
  }}
  hr {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 22px 0;
  }}
  a {{
    color: var(--accent);
    text-decoration: none;
  }}
  a:hover {{
    text-decoration: underline;
  }}
  ul, ol {{
    margin: 6px 0 14px;
    padding-left: 22px;
  }}
  li {{
    margin-bottom: 5px;
  }}
  li > ul, li > ol {{
    margin-top: 5px;
    margin-bottom: 5px;
  }}
  code {{
    font-family: var(--font-mono);
    font-size: 12.5px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
    color: #bae6fd;
  }}
  .code-block {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 10px 0 16px;
    overflow: hidden;
  }}
  .code-head {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 5px 10px;
    background: var(--row);
    border-bottom: 1px solid var(--border);
  }}
  .code-lang {{
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--caption);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .copy-btn {{
    background: var(--surface);
    color: var(--muted);
    border: 1px solid var(--border-bright);
    border-radius: 5px;
    padding: 2px 8px;
    font: 600 11px var(--font-sans);
    cursor: pointer;
  }}
  .copy-btn:hover {{
    color: var(--text-bright);
    border-color: var(--accent);
    background: var(--surface-hover);
  }}
  pre {{
    margin: 0;
    padding: 12px 14px;
    overflow-x: auto;
    font-family: var(--font-mono);
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--text);
  }}
  pre code {{
    background: transparent;
    border: none;
    padding: 0;
    color: inherit;
    font-size: inherit;
  }}
  .table-wrap {{
    overflow-x: auto;
    margin: 10px 0 16px;
    border: 1px solid var(--border);
    border-radius: 8px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    background: var(--panel);
  }}
  th, td {{
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    text-align: left;
    vertical-align: top;
  }}
  th {{
    background: var(--row);
    color: var(--text-bright);
    font-weight: 700;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }}
  tr:last-child td {{
    border-bottom: none;
  }}
  blockquote {{
    margin: 10px 0 14px;
    padding: 8px 14px;
    background: var(--panel);
    border-left: 3px solid var(--accent);
    border-radius: 0 6px 6px 0;
    color: var(--muted);
  }}
  .alert {{
    margin: 12px 0 16px;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--border-bright);
    background: var(--panel);
  }}
  .alert-title {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
    margin-bottom: 4px;
  }}
  .alert-important {{
    border-color: rgba(56, 189, 248, 0.55);
    background: rgba(56, 189, 248, 0.08);
  }}
  .alert-important .alert-title {{ color: var(--accent); }}
  .alert-note .alert-title {{ color: var(--accent); }}
  .alert-tip {{
    border-color: rgba(34, 197, 94, 0.45);
    background: rgba(34, 197, 94, 0.08);
  }}
  .alert-tip .alert-title {{ color: var(--good); }}
  .alert-warning {{
    border-color: rgba(234, 179, 8, 0.55);
    background: rgba(234, 179, 8, 0.08);
  }}
  .alert-warning .alert-title {{ color: var(--warn); }}
  .alert-caution {{
    border-color: rgba(239, 68, 68, 0.55);
    background: rgba(239, 68, 68, 0.08);
  }}
  .alert-caution .alert-title {{ color: var(--bad); }}

  mark.find-hit {{
    background: rgba(234, 179, 8, 0.28);
    color: var(--text-bright);
    border-radius: 2px;
    padding: 0 1px;
  }}
  mark.find-hit.active {{
    background: rgba(56, 189, 248, 0.45);
    outline: 1px solid var(--accent);
  }}
  @media (max-width: 780px) {{
    .sidebar {{ display: none; }}
    .content-wrap {{ padding: 18px 18px 42px; }}
  }}
</style>
</head>
<body>
<div class="find-bar" id="findBar" aria-hidden="true">
  <input type="search" id="findInput" class="find-input" placeholder="Find in manual...">
  <span id="findCount" class="find-count"></span>
  <button type="button" class="find-btn" id="findPrevBtn" title="Previous match (Shift+Enter)">&#9650;</button>
  <button type="button" class="find-btn" id="findNextBtn" title="Next match (Enter)">&#9660;</button>
  <button type="button" class="find-btn" id="findCloseBtn" title="Close (Esc)">&times;</button>
</div>
<div class="layout" id="layoutRoot">
  <button type="button" class="sidebar-open-btn" id="sidebarOpenBtn" title="Show contents" aria-label="Show contents">&#9776;</button>
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head">
      <div class="brand">User Manual &middot; {doc_label}</div>
      <button type="button" class="sidebar-close-btn" id="sidebarCloseBtn" title="Hide contents" aria-label="Hide contents">&times;</button>
    </div>
    {back_btn}
    <nav class="toc-nav" id="tocNav">
      {toc_links}
    </nav>
  </aside>
  <main class="content-wrap">
    <article class="doc" id="docBody">
      {body_html}
    </article>
  </main>
</div>
<script>
const layoutRoot = document.getElementById('layoutRoot');
document.getElementById('sidebarCloseBtn').addEventListener('click', () => {{
  layoutRoot.classList.add('sidebar-collapsed');
}});
document.getElementById('sidebarOpenBtn').addEventListener('click', () => {{
  layoutRoot.classList.remove('sidebar-collapsed');
}});
function copyCode(btn) {{
  const pre = btn.closest('.code-block').querySelector('pre');
  if (!pre) return;
  const text = pre.innerText;
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(() => {{
      btn.textContent = 'Copied';
      setTimeout(() => btn.textContent = 'Copy', 1200);
    }}).catch(() => {{}});
  }}
}}

const backBtn = document.getElementById('backHomeBtn');
if (backBtn) {{
  backBtn.addEventListener('click', () => {{
    window.location.href = '__EPOC_MANUAL_HOME__';
  }});
}}

// Collapsible TOC subheadings (collapsed by default; click main heading to toggle)
const tocLinks = Array.from(document.querySelectorAll('.toc-link'));
for (const a of tocLinks) {{
  a.addEventListener('click', () => {{
    if (a.classList.contains('toc-l2')) {{
      const grp = a.closest('.toc-group');
      if (grp) {{
        const isOpen = grp.classList.toggle('open');
        a.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        updateActiveToc();
      }}
    }}
  }});
}}

// Highlight active section in TOC while scrolling (uses parent L2 if its group is collapsed)
const headings = tocLinks.map(a => document.getElementById(a.getAttribute('href').slice(1))).filter(Boolean);
function updateActiveToc() {{
  let current = headings[0];
  for (const h of headings) {{
    if (h.getBoundingClientRect().top <= 90) current = h;
  }}
  let activeLink = current
    ? tocLinks.find(a => a.getAttribute('href') === '#' + current.id)
    : null;
  if (activeLink && activeLink.classList.contains('toc-l3')) {{
    const grp = activeLink.closest('.toc-group');
    if (grp && !grp.classList.contains('open')) {{
      activeLink = grp.querySelector('.toc-link.toc-l2') || activeLink;
    }}
  }}
  for (const a of tocLinks) {{
    a.classList.toggle('active', a === activeLink);
  }}
}}
window.addEventListener('scroll', updateActiveToc, {{ passive: true }});
updateActiveToc();

// On-demand Ctrl+F floating find bar scoped strictly to #docBody (excludes side navigation)
const docBody = document.getElementById('docBody');
const findBar = document.getElementById('findBar');
const findInput = document.getElementById('findInput');
const findCount = document.getElementById('findCount');
let findHits = [];
let findIdx = -1;
let lastQuery = '';

function clearFindHighlights() {{
  for (const mark of Array.from(docBody.querySelectorAll('mark.find-hit'))) {{
    const parent = mark.parentNode;
    if (!parent) continue;
    parent.replaceChild(document.createTextNode(mark.textContent || ''), mark);
  }}
  docBody.normalize();
  findHits = [];
  findIdx = -1;
}}

function activateHit(idx) {{
  if (!findHits.length) return;
  for (const m of findHits) m.classList.remove('active');
  findIdx = ((idx % findHits.length) + findHits.length) % findHits.length;
  const hit = findHits[findIdx];
  hit.classList.add('active');
  hit.scrollIntoView({{ block: 'center', behavior: 'smooth' }});
  findCount.textContent = (findIdx + 1) + '/' + findHits.length;
  findCount.classList.remove('empty');
}}

function applyFind(rawQuery) {{
  const query = rawQuery.trim();
  lastQuery = query;
  clearFindHighlights();
  if (!query) {{
    findCount.textContent = '';
    findCount.classList.remove('empty');
    return;
  }}

  const qLower = query.toLowerCase();
  const walker = document.createTreeWalker(
    docBody,
    NodeFilter.SHOW_TEXT,
    {{
      acceptNode(node) {{
        const el = node.parentElement;
        if (!el) return NodeFilter.FILTER_REJECT;
        if (el.closest('button, script, style')) return NodeFilter.FILTER_REJECT;
        return node.nodeValue ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }}
    }}
  );

  const textNodes = [];
  let cur;
  while ((cur = walker.nextNode())) textNodes.push(cur);

  for (const node of textNodes) {{
    const text = node.nodeValue;
    const lower = text.toLowerCase();
    let pos = 0;
    let matchIdx = lower.indexOf(qLower, pos);
    if (matchIdx === -1) continue;

    const frag = document.createDocumentFragment();
    while (matchIdx !== -1) {{
      if (matchIdx > pos) {{
        frag.appendChild(document.createTextNode(text.slice(pos, matchIdx)));
      }}
      const mark = document.createElement('mark');
      mark.className = 'find-hit';
      mark.textContent = text.slice(matchIdx, matchIdx + query.length);
      frag.appendChild(mark);
      findHits.push(mark);
      pos = matchIdx + query.length;
      matchIdx = lower.indexOf(qLower, pos);
    }}
    if (pos < text.length) {{
      frag.appendChild(document.createTextNode(text.slice(pos)));
    }}
    node.parentNode.replaceChild(frag, node);
  }}

  if (findHits.length > 0) {{
    activateHit(0);
  }} else {{
    findCount.textContent = '0/0';
    findCount.classList.add('empty');
  }}
}}

function openFindBar() {{
  findBar.classList.add('open');
  findBar.setAttribute('aria-hidden', 'false');
  findInput.focus();
  findInput.select();
  if (findInput.value.trim()) {{
    applyFind(findInput.value);
  }}
}}

function closeFindBar() {{
  findBar.classList.remove('open');
  findBar.setAttribute('aria-hidden', 'true');
  findInput.blur();
  clearFindHighlights();
  lastQuery = '';
  findCount.textContent = '';
  findCount.classList.remove('empty');
}}

function runFind(backwards) {{
  const q = findInput.value.trim();
  if (!q) {{
    applyFind('');
    return;
  }}
  if (q !== lastQuery || !findHits.length) {{
    applyFind(q);
    return;
  }}
  activateHit(backwards ? findIdx - 1 : findIdx + 1);
}}

document.addEventListener('keydown', (e) => {{
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {{
    e.preventDefault();
    openFindBar();
  }} else if (e.key === 'Escape' && findBar.classList.contains('open')) {{
    e.preventDefault();
    closeFindBar();
  }}
}});

findInput.addEventListener('input', () => {{
  applyFind(findInput.value);
}});

findInput.addEventListener('keydown', (e) => {{
  if (e.key === 'Enter') {{
    e.preventDefault();
    runFind(e.shiftKey);
  }} else if (e.key === 'Escape') {{
    e.preventDefault();
    closeFindBar();
  }}
}});

document.getElementById('findNextBtn').addEventListener('click', () => runFind(false));
document.getElementById('findPrevBtn').addEventListener('click', () => runFind(true));
document.getElementById('findCloseBtn').addEventListener('click', closeFindBar);
</script>
</body>
</html>"""


class _ManualWebPage(StudioWebPage):
    """WebEngine page that opens external URLs in the OS browser and local docs in-app."""

    def __init__(self, home_path: Optional[Path], on_open_doc: Callable[[Path], None], parent=None):
        super().__init__(parent=parent)
        self._home_path = home_path
        self._on_open_doc = on_open_doc
        self._current_path: Optional[Path] = home_path

    def set_current_doc(self, path: Optional[Path]) -> None:
        self._current_path = path

    def acceptNavigationRequest(self, url: QtCore.QUrl, nav_type, isMainFrame: bool) -> bool:
        if not isMainFrame:
            return super().acceptNavigationRequest(url, nav_type, isMainFrame)

        raw = url.toString()
        if raw.endswith("__EPOC_MANUAL_HOME__") and self._home_path is not None:
            home = self._home_path
            QtCore.QTimer.singleShot(0, lambda p=home: self._on_open_doc(p))
            return False

        if nav_type != QtWebEngineCore.QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            return super().acceptNavigationRequest(url, nav_type, isMainFrame)

        scheme = url.scheme().lower()
        if scheme in ("http", "https"):
            QtGui.QDesktopServices.openUrl(url)
            return False

        if scheme == "file":
            target = Path(url.toLocalFile()).resolve()
            if self._current_path is not None and target == self._current_path.resolve() and url.hasFragment():
                return True
            if target.suffix.lower() == ".md" and target.is_file():
                QtCore.QTimer.singleShot(0, lambda p=target: self._on_open_doc(p))
                return False
            if target.exists():
                QtGui.QDesktopServices.openUrl(url)
                return False

        return False


def create_manual_window(
    doc_path: str | Path = "README.md",
    on_close: Optional[Callable[[], None]] = None,
    parent=None,
) -> BaseWebWindow:
    """Open the in-app User Manual inside a native dark-themed desktop window."""
    win = BaseWebWindow(
        title="EPOC-X Studio — User Manual",
        bg_hex="#080d18",
        width=1140,
        height=820,
        on_close=on_close,
        parent=parent,
    )

    home_path = _resolve_doc_path(doc_path)

    def _load_doc(target: Path) -> None:
        is_sub = home_path is not None and target.resolve() != home_path.resolve()
        page.set_current_doc(target)
        win.setWindowTitle(f"EPOC-X Studio — {target.name}")
        html_str = build_manual_html(target, is_subdoc=is_sub)
        win.view.setHtml(html_str, QtCore.QUrl.fromLocalFile(str(target)))

    page = _ManualWebPage(home_path=home_path, on_open_doc=_load_doc, parent=win.view)
    page.setBackgroundColor(QtGui.QColor("#080d18"))
    page.settings().setAttribute(
        QtWebEngineCore.QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
    )
    win.page = page
    win.view.setPage(page)
    win.view.setFocus()

    find_shortcut = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Find, win)
    find_shortcut.activated.connect(
        lambda: (win.view.setFocus(), page.runJavaScript("if (typeof openFindBar === 'function') openFindBar();"))
    )
    win._find_shortcut = find_shortcut

    if home_path is not None:
        _load_doc(home_path)
    else:
        win.view.setHtml(
            "<html><body style='background:#080d18;color:#e2e8f0;font-family:Segoe UI,sans-serif;padding:32px;'>"
            "<h2 style='color:#38bdf8;'>User Manual Not Found</h2>"
            "<p>Could not locate <code>README.md</code> in the repository root.</p>"
            "</body></html>"
        )
    return win


def main() -> int:
    app = ensure_qt_app()
    win = create_manual_window()
    install_sigint_handler(win, lambda: (win.shutdown(), app.quit()))
    win.show()
    apply_dark_titlebar(win)
    try:
        return app.exec()
    finally:
        win.shutdown()
