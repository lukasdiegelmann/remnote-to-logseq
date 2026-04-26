#!/usr/bin/env python3
"""Migrate RemNote SQLite DB to an Obsidian vault.

Mirrors RemNote's document hierarchy: every rem tagged with the "Dokument"
power-up becomes its own file, nested in folders matching the hierarchy of
Dokument-tagged ancestors. Non-Dokument top-level rems land in `Lose Notizen/`
as a flat list of stubs.

Two output modes share the same folder structure:

- markdown (.md)  — Obsidian-flavored Markdown. Inline references become
  wiki links, images embed as base64, span colours/heading-sizes are HTML
  spans. The original mode.

- html (.html)    — A richer presentation with real <h1..h6> headings, real
  <ul>/<li> trees, embedded CSS, MathJax for LaTeX, URL preview cards, and
  pure-image child rems floated to the right of their parent's text.

Pass `--mode {markdown,html,both}` to choose. Default: both.
"""
from __future__ import annotations

import argparse
import base64
import html as html_mod
import json
import mimetypes
import os
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

# --- paths -------------------------------------------------------------------

DB_PATH = Path("/home/lukasdiegelmann/remnote/remnote-666c2b227ea3bab0376d53ba/remnote.db")
FILES_DIR = Path("/home/lukasdiegelmann/remnote/remnote-666c2b227ea3bab0376d53ba/files")
VAULT = Path("/home/lukasdiegelmann/Documents/Obsidian/lukasdiegelmann/lukasdiegelmann")
ORPHAN_DIR_NAME = "Lose Notizen"

# --- RemNote constants -------------------------------------------------------

DOC_TAG = "Fv0CqTL0lG4TwiBFK"  # "Dokument" power-up rem id

COLOR_MAP = {
    0: None, 1: "#d8453e", 2: "#e6862b", 3: "#e8b53b",
    4: "#3a9450", 5: "#3865c2", 6: "#9b4dca", 7: "#8a8a8a",
}
HEADING_STYLE_MD = {
    0: "font-weight:700;font-size:1.5em",
    1: "font-weight:700;font-size:1.3em",
    2: "font-weight:700;font-size:1.15em",
    3: "font-weight:700",
    4: "font-weight:600",
    5: "font-weight:600",
    6: "font-weight:600",
}
# Map RemNote span-level h (0=biggest) to actual HTML heading tag for HTML mode.
# Reserve <h1> for the document title; rem-text headings start at <h2>.
HEADING_TAG = {0: "h2", 1: "h3", 2: "h4", 3: "h5", 4: "h6", 5: "h6", 6: "h6"}

# --- filesystem helpers ------------------------------------------------------

INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def slug_for_filename(text: str, max_len: int = 110) -> str:
    text = unicodedata.normalize("NFC", text)
    text = INVALID_FS_CHARS.sub(" ", text)
    text = text.replace("[", "(").replace("]", ")")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Unbenannt"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    return text


def fractional_index_key(f: str | None) -> tuple:
    return (1, "") if f is None else (0, f)


# --- shared text helpers -----------------------------------------------------

def render_plain(items, idmap) -> str:
    if not items:
        return ""
    out = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("i")
        if kind == "q":
            target = it.get("_id")
            if it.get("textOfDeletedRem"):
                out.append(render_plain(it["textOfDeletedRem"], idmap))
            elif target and target in idmap:
                out.append(rem_title(idmap[target], idmap))
        elif kind in ("m", "x"):
            out.append(it.get("text", ""))
        elif kind == "u":
            out.append(it.get("title") or it.get("url") or "")
        elif kind == "i":
            out.append(it.get("title") or "")
        elif kind == "a":
            out.append(it.get("url", ""))
    return "".join(out)


def rem_title(rem, idmap, *, fallback="Unbenannt") -> str:
    title = render_plain(rem.get("key") or [], idmap).strip()
    if not title:
        v = rem.get("value")
        if v:
            title = render_plain(v, idmap).strip()
    return title or fallback


def is_user_rem(doc: dict) -> bool:
    if doc.get("e") is not None:
        return False
    for f in ("rcrt", "rcrs", "rcre", "rcrp"):
        if doc.get(f) is not None:
            return False
    return True


def has_visible_content(doc: dict) -> bool:
    for f in ("key", "value"):
        v = doc.get(f)
        if isinstance(v, list) and len(v) > 0:
            return True
    return False


def is_document(doc: dict) -> bool:
    tp = doc.get("tp") or {}
    return isinstance(tp, dict) and DOC_TAG in tp


def is_pure_image_rem(doc: dict) -> bool:
    """True if the rem's content is entirely image elements (no text)."""
    if doc.get("value"):
        return False
    key = doc.get("key") or []
    if not key:
        return False
    has_image = False
    for it in key:
        if isinstance(it, str):
            if it.strip():
                return False
            continue
        if not isinstance(it, dict):
            return False
        if it.get("i") == "i":
            has_image = True
        else:
            return False
    return has_image


def collect_image_urls(items) -> list[dict]:
    return [it for it in items
            if isinstance(it, dict) and it.get("i") == "i" and it.get("url")]


# --- markdown rendering ------------------------------------------------------

def render_md(items, idmap, owner_file, current_owner, image_cache):
    if not items:
        return ""
    parts: list[str] = []
    for it in items:
        if isinstance(it, str):
            parts.append(_md_inline_escape(it))
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("i")
        if kind == "q":
            tid = it.get("_id")
            if it.get("textOfDeletedRem"):
                parts.append(render_md(
                    it["textOfDeletedRem"], idmap, owner_file,
                    current_owner, image_cache,
                ))
            elif tid and tid in idmap:
                target = idmap[tid]
                title = rem_title(target, idmap)
                target_file = owner_file.get(tid)
                if not target_file:
                    parts.append(_md_inline_escape(title))
                elif target_file == current_owner:
                    parts.append(_md_inline_escape(title))
                else:
                    basename = Path(target_file).stem
                    if basename == title:
                        parts.append(f"[[{basename}]]")
                    else:
                        parts.append(f"[[{basename}|{title}]]")
        elif kind == "m":
            parts.append(_md_format_text_span(it))
        elif kind == "x":
            text = it.get("text", "")
            if not text:
                continue
            tc = it.get("tc")
            color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
            rendered = (f"\n$$\n{text}\n$$\n"
                        if it.get("block") else f"${text}$")
            if color:
                rendered = f'<span style="color:{color}">{rendered}</span>'
            parts.append(rendered)
        elif kind == "i":
            url = it.get("url", "")
            embedded = _embed_image_md(url, image_cache)
            if embedded:
                parts.append(embedded)
            elif url:
                parts.append(f"![]({url})")
        elif kind == "u":
            url = it.get("url", "")
            title = (it.get("title") or url).strip() or url
            if url:
                parts.append(f"[{title}]({url})")
        elif kind == "a":
            url = it.get("url", "")
            if url:
                parts.append(f"[{url}]({url})")
    return "".join(parts)


def _md_format_text_span(it: dict) -> str:
    text = it.get("text", "")
    if text == "":
        return ""
    text = _md_inline_escape(text)

    inline_url = it.get("iUrl")
    if inline_url:
        text = f"[{text}]({inline_url})"

    if it.get("b"):
        text = f"**{text}**"
    if it.get("l"):
        text = f"*{text}*"
    if it.get("u"):
        text = f"<u>{text}</u>"
    if it.get("sup"):
        text = f"<sup>{text}</sup>"

    style_parts: list[str] = []
    tc = it.get("tc")
    color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
    if color:
        style_parts.append(f"color:{color}")
    h = it.get("h")
    if isinstance(h, int) and h in HEADING_STYLE_MD:
        style_parts.append(HEADING_STYLE_MD[h])
    if style_parts:
        text = f'<span style="{";".join(style_parts)}">{text}</span>'
    return text


def _md_inline_escape(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\n", "<br>")


def _embed_image_md(url: str, cache: dict[str, str]) -> str | None:
    if not url:
        return None
    if url in cache:
        return cache[url]
    if url.startswith("%LOCAL_FILE%"):
        data = _load_local_image(url)
        if not data:
            cache[url] = ""
            return None
        mime, b64 = data
        result = f"![](data:{mime};base64,{b64})"
    else:
        result = f"![]({url})"
    cache[url] = result
    return result


# --- HTML rendering ----------------------------------------------------------

HTML_CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
               Helvetica, Arial, sans-serif;
  max-width: 980px;
  margin: 2em auto;
  padding: 0 2em 4em;
  line-height: 1.55;
  color: #1f1f1f;
  background: #fafafa;
}
nav.breadcrumbs {
  font-size: 0.9em;
  color: #777;
  margin-bottom: 1em;
}
nav.breadcrumbs a { color: #5a4dca; text-decoration: none; }
nav.breadcrumbs a:hover { text-decoration: underline; }
h1.doc-title {
  font-size: 2em;
  margin: 0 0 0.6em;
  border-bottom: 2px solid #e3e3e3;
  padding-bottom: 0.25em;
  line-height: 1.2;
}
.doc-descriptor { color: #666; margin: 0 0 1.5em; font-style: italic; }
ul.rem-tree, ul.rem-tree ul {
  padding-left: 1.4em;
  margin: 0.3em 0;
}
ul.rem-tree li {
  margin: 0.35em 0;
  position: relative;
}
ul.rem-tree li::after { content: ""; display: block; clear: both; }
ul.rem-tree li.no-bullet { list-style: none; }
.descriptor-sep { color: #999; margin: 0 0.35em; }
img.rem-img {
  max-width: 100%;
  height: auto;
  border-radius: 5px;
  display: block;
}
img.rem-img.float-right {
  float: right;
  max-width: 36%;
  margin: 0 0 0.7em 1.1em;
}
img.rem-img.float-left {
  float: left;
  max-width: 36%;
  margin: 0 1.1em 0.7em 0;
}
.q-link {
  color: #5a4dca;
  text-decoration: none;
  border-bottom: 1px dotted #b9b3eb;
}
.q-link:hover { color: #3a2eb3; border-bottom-color: #3a2eb3; }
.url-card {
  display: inline-block;
  border: 1px solid #e1e1e1;
  padding: 0.5em 0.8em;
  border-radius: 6px;
  background: #fff;
  text-decoration: none;
  color: inherit;
  margin: 0.3em 0;
  max-width: 32em;
}
.url-card:hover { border-color: #5a4dca; }
.url-card .url-title {
  font-weight: 600;
  color: #5a4dca;
  display: block;
}
.url-card .url-site { font-size: 0.8em; color: #999; }
.url-card .url-desc {
  font-size: 0.88em;
  color: #555;
  margin-top: 0.25em;
}
.subdocs {
  margin-top: 2.5em;
  padding-top: 1.2em;
  border-top: 1px solid #e3e3e3;
}
.subdocs h2 { font-size: 1.15em; margin: 0 0 0.6em; color: #555; }
.subdocs ul { padding-left: 1.4em; }
.todo-done { text-decoration: line-through; opacity: 0.55; }
.math-block { margin: 0.6em 0; overflow-x: auto; }
li.heading-rem > .rem-text > h2,
li.heading-rem > .rem-text > h3,
li.heading-rem > .rem-text > h4,
li.heading-rem > .rem-text > h5,
li.heading-rem > .rem-text > h6 { display: inline; margin: 0; }
""".strip()


def render_html(items, idmap, owner_file, current_owner, image_cache_html,
                html_paths) -> str:
    if not items:
        return ""
    parts: list[str] = []
    for it in items:
        if isinstance(it, str):
            parts.append(_html_text(it))
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("i")
        if kind == "q":
            tid = it.get("_id")
            if it.get("textOfDeletedRem"):
                parts.append(render_html(
                    it["textOfDeletedRem"], idmap, owner_file, current_owner,
                    image_cache_html, html_paths,
                ))
            elif tid and tid in idmap:
                target = idmap[tid]
                title = rem_title(target, idmap)
                target_file = owner_file.get(tid)
                if not target_file:
                    parts.append(_html_text(title))
                elif target_file == current_owner:
                    parts.append(_html_text(title))
                else:
                    href = _html_link_href(current_owner, target_file,
                                           html_paths)
                    parts.append(
                        f'<a class="q-link" href="{html_mod.escape(href)}">'
                        f'{_html_text(title)}</a>'
                    )
        elif kind == "m":
            parts.append(_html_format_text_span(it))
        elif kind == "x":
            text = it.get("text", "")
            if not text:
                continue
            tc = it.get("tc")
            color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
            if it.get("block"):
                rendered = (f'<div class="math-block">'
                            f'\\[{html_mod.escape(text)}\\]</div>')
            else:
                rendered = f"\\({html_mod.escape(text)}\\)"
            if color:
                rendered = (f'<span style="color:{color}">'
                            f'{rendered}</span>')
            parts.append(rendered)
        elif kind == "i":
            url = it.get("url", "")
            src = _embed_image_html(url, image_cache_html)
            if src:
                parts.append(f'<img class="rem-img" src="{src}" alt="">')
        elif kind == "u":
            url = it.get("url", "")
            title = (it.get("title") or url).strip() or url
            site = (it.get("siteName") or "").strip()
            desc = (it.get("description") or "").strip()
            if url:
                if desc or site:
                    inner = (f'<span class="url-title">'
                             f'{html_mod.escape(title)}</span>')
                    if site:
                        inner += (f'<span class="url-site"> · '
                                  f'{html_mod.escape(site)}</span>')
                    if desc:
                        inner += (f'<span class="url-desc">'
                                  f'{html_mod.escape(desc)}</span>')
                    parts.append(
                        f'<a class="url-card" href="{html_mod.escape(url)}" '
                        f'target="_blank" rel="noopener">{inner}</a>'
                    )
                else:
                    parts.append(
                        f'<a href="{html_mod.escape(url)}" '
                        f'target="_blank" rel="noopener">'
                        f'{html_mod.escape(title)}</a>'
                    )
        elif kind == "a":
            url = it.get("url", "")
            if url:
                parts.append(
                    f'<a href="{html_mod.escape(url)}" '
                    f'target="_blank" rel="noopener">'
                    f'{html_mod.escape(url)}</a>'
                )
    return "".join(parts)


def _html_format_text_span(it: dict) -> str:
    text = it.get("text", "")
    if text == "":
        return ""
    out = _html_text(text)

    inline_url = it.get("iUrl")
    if inline_url:
        out = (f'<a href="{html_mod.escape(inline_url)}" '
               f'target="_blank" rel="noopener">{out}</a>')

    if it.get("b"):
        out = f"<strong>{out}</strong>"
    if it.get("l"):
        out = f"<em>{out}</em>"
    if it.get("u"):
        out = f"<u>{out}</u>"
    if it.get("sup"):
        out = f"<sup>{out}</sup>"

    style_parts: list[str] = []
    tc = it.get("tc")
    color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
    if color:
        style_parts.append(f"color:{color}")
    if style_parts:
        out = f'<span style="{";".join(style_parts)}">{out}</span>'

    h = it.get("h")
    if isinstance(h, int) and h in HEADING_TAG:
        tag = HEADING_TAG[h]
        out = f"<{tag}>{out}</{tag}>"
    return out


def _html_text(s: str) -> str:
    return html_mod.escape(s).replace("\r\n", "\n").replace("\n", "<br>")


def _embed_image_html(url: str, cache: dict[str, str]) -> str | None:
    if not url:
        return None
    if url in cache:
        return cache[url] or None
    if url.startswith("%LOCAL_FILE%"):
        data = _load_local_image(url)
        if not data:
            cache[url] = ""
            return None
        mime, b64 = data
        src = f"data:{mime};base64,{b64}"
    else:
        src = url
    cache[url] = src
    return src


def _html_link_href(current_owner: str, target_owner: str,
                    html_paths: dict[str, Path]) -> str:
    """Compute a relative .html link from current to target file."""
    src = html_paths.get(current_owner)
    dst = html_paths.get(target_owner)
    if src is None or dst is None:
        # Fallback: just basename
        return Path(target_owner).with_suffix(".html").name
    return os.path.relpath(dst, start=src.parent)


# --- shared image loader -----------------------------------------------------

_LOAD_CACHE: dict[str, tuple[str, str] | None] = {}


def _load_local_image(url: str) -> tuple[str, str] | None:
    """Return (mime, base64) for a %LOCAL_FILE% URL, cached on disk reads."""
    if url in _LOAD_CACHE:
        return _LOAD_CACHE[url]
    name = url[len("%LOCAL_FILE%"):]
    path = FILES_DIR / name
    if not path.exists():
        _LOAD_CACHE[url] = None
        return None
    try:
        data = path.read_bytes()
    except OSError:
        _LOAD_CACHE[url] = None
        return None
    mime, _ = mimetypes.guess_type(path.name) or (None, None)
    if not mime:
        mime = "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    _LOAD_CACHE[url] = (mime, encoded)
    return _LOAD_CACHE[url]


# --- main migration ----------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--mode", choices=("markdown", "html", "both"),
                   default="both",
                   help="Which output format to produce. Default: both.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    do_md = args.mode in ("markdown", "both")
    do_html = args.mode in ("html", "both")

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute("SELECT _id, doc FROM quanta")
    idmap: dict[str, dict] = {}
    for _id, doc_json in cur:
        try:
            idmap[_id] = json.loads(doc_json)
        except json.JSONDecodeError:
            continue
    conn.close()
    print(f"Loaded {len(idmap)} rems", file=sys.stderr)

    children: dict[str | None, list[str]] = defaultdict(list)
    for rid, doc in idmap.items():
        children[doc.get("parent")].append(rid)
    for plist in children.values():
        plist.sort(key=lambda rid: fractional_index_key(idmap[rid].get("f")))

    doc_ids: set[str] = {
        rid for rid, doc in idmap.items()
        if is_user_rem(doc) and is_document(doc) and has_visible_content(doc)
    }
    print(f"Dokument-tagged user rems: {len(doc_ids)}", file=sys.stderr)

    orphan_top: list[str] = []
    for rid in children[None]:
        doc = idmap[rid]
        if (is_user_rem(doc) and has_visible_content(doc)
                and rid not in doc_ids):
            orphan_top.append(rid)
    print(f"Orphan top-level rems: {len(orphan_top)}", file=sys.stderr)

    sys.setrecursionlimit(50000)

    def doc_ancestors(rid: str) -> list[str]:
        chain: list[str] = []
        cur_id: str | None = rid
        while cur_id is not None:
            doc = idmap.get(cur_id)
            if doc is None:
                break
            if cur_id in doc_ids and cur_id != rid:
                chain.append(cur_id)
            cur_id = doc.get("parent")
        chain.reverse()
        return chain

    has_subdocs: dict[str, bool] = {rid: False for rid in doc_ids}
    parent_doc_of: dict[str, str | None] = {}
    for rid in doc_ids:
        chain = doc_ancestors(rid)
        parent = chain[-1] if chain else None
        parent_doc_of[rid] = parent
        if parent is not None:
            has_subdocs[parent] = True

    used_in_dir: dict[Path, dict[str, int]] = defaultdict(dict)
    rem_path_md: dict[str, Path] = {}

    def folder_for(rid: str) -> Path:
        ancestors = doc_ancestors(rid)
        path = VAULT
        for aid in ancestors:
            seg = slug_for_filename(rem_title(idmap[aid], idmap), max_len=80)
            path = path / seg
        return path

    def assign_path(rid: str) -> Path:
        parent_folder = folder_for(rid)
        title = rem_title(idmap[rid], idmap)
        base = slug_for_filename(title)
        if has_subdocs.get(rid):
            folder = parent_folder / base
            path = folder / f"{base}.md"
        else:
            path = parent_folder / f"{base}.md"
        dir_used = used_in_dir[path.parent]
        n = dir_used.get(base.lower(), 0)
        dir_used[base.lower()] = n + 1
        if n > 0:
            new_base = f"{base} ({n+1})"
            if has_subdocs.get(rid):
                folder = parent_folder / new_base
                path = folder / f"{new_base}.md"
            else:
                path = parent_folder / f"{new_base}.md"
        return path

    sorted_docs = sorted(doc_ids, key=lambda r: len(doc_ancestors(r)))
    for rid in sorted_docs:
        rem_path_md[rid] = assign_path(rid)

    orphan_dir = VAULT / ORPHAN_DIR_NAME
    for rid in orphan_top:
        title = rem_title(idmap[rid], idmap)
        base = slug_for_filename(title)
        n = used_in_dir[orphan_dir].get(base.lower(), 0)
        used_in_dir[orphan_dir][base.lower()] = n + 1
        name = base if n == 0 else f"{base} ({n+1})"
        rem_path_md[rid] = orphan_dir / f"{name}.md"

    # Map every rem (including non-document descendants) to its owner file.
    owner_file: dict[str, str] = {}

    def vault_relative(p: Path) -> str:
        return str(p.relative_to(VAULT)).replace("\\", "/")

    def assign_owner(rid: str, file_rel: str) -> None:
        owner_file[rid] = file_rel
        for cid in children.get(rid, []):
            cdoc = idmap[cid]
            if not is_user_rem(cdoc):
                continue
            if cid in doc_ids:
                continue
            assign_owner(cid, file_rel)

    for rid, p in rem_path_md.items():
        owner_file_rel = vault_relative(p)
        assign_owner(rid, owner_file_rel)

    # HTML paths share the same folder structure but with .html extension.
    html_paths: dict[str, Path] = {
        rid: p.with_suffix(".html") for rid, p in rem_path_md.items()
    }
    # Map by owner-file-relative key so render_html can resolve targets.
    html_paths_by_owner: dict[str, Path] = {}
    for rid, p in rem_path_md.items():
        html_paths_by_owner[vault_relative(p)] = p.with_suffix(".html")

    image_cache_md: dict[str, str] = {}
    image_cache_html: dict[str, str] = {}

    md_written = 0
    html_written = 0
    for rid in sorted_docs + orphan_top:
        path_md = rem_path_md[rid]
        path_md.parent.mkdir(parents=True, exist_ok=True)
        if do_md:
            body_lines = render_document_md(
                rid, idmap, children, doc_ids, owner_file, rem_path_md,
                image_cache_md,
            )
            path_md.write_text(
                "\n".join(body_lines).rstrip() + "\n", encoding="utf-8",
            )
            md_written += 1
        if do_html:
            html_text = render_document_html(
                rid, idmap, children, doc_ids, owner_file,
                html_paths_by_owner, image_cache_html, parent_doc_of,
                rem_path_md,
            )
            path_md.with_suffix(".html").write_text(
                html_text, encoding="utf-8",
            )
            html_written += 1

    if do_md:
        print(f"Wrote {md_written} markdown files", file=sys.stderr)
    if do_html:
        print(f"Wrote {html_written} html files", file=sys.stderr)

    write_index_md(doc_ids, orphan_top, idmap, rem_path_md, parent_doc_of,
                   do_md, do_html)
    return 0


# ---- Markdown document renderer --------------------------------------------

def render_document_md(rid, idmap, children, doc_ids, owner_file, rem_path,
                       image_cache):
    doc = idmap[rid]
    file_rel = owner_file[rid]
    lines: list[str] = []

    value = doc.get("value")
    if isinstance(value, list) and value:
        v_md = render_md(value, idmap, owner_file, file_rel,
                         image_cache).strip()
        if v_md:
            lines.append(v_md)
            lines.append("")

    body: list[str] = []
    for cid in children.get(rid, []):
        emit_child_md(cid, idmap, children, doc_ids, owner_file, rem_path,
                      image_cache, file_rel, body, depth=1)
    if body:
        lines.extend(body)

    sub_doc_links: list[str] = []
    for cid in children.get(rid, []):
        if cid in doc_ids:
            sub_doc_links.append(_doc_link_md(cid, idmap, rem_path))
    if sub_doc_links:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("## Unterdokumente")
        lines.append("")
        lines.extend(sub_doc_links)
    return lines


def emit_child_md(rid, idmap, children, doc_ids, owner_file, rem_path,
                  image_cache, current_owner, lines, *, depth):
    doc = idmap[rid]
    if not is_user_rem(doc):
        return
    if rid in doc_ids:
        return
    if not has_visible_content(doc) and not children.get(rid):
        return

    key_md = render_md(doc.get("key") or [], idmap, owner_file,
                       current_owner, image_cache).strip()
    value = doc.get("value")
    value_md = ""
    if isinstance(value, list) and value:
        value_md = render_md(value, idmap, owner_file, current_owner,
                             image_cache).strip()
    text = key_md
    if value_md:
        text = f"{text} :: {value_md}" if text else value_md
    indent = "  " * (depth - 1)
    if text:
        lines.append(f"{indent}- {text}")

    for cid in children.get(rid, []):
        emit_child_md(cid, idmap, children, doc_ids, owner_file, rem_path,
                      image_cache, current_owner, lines,
                      depth=depth + 1)


def _doc_link_md(rid: str, idmap, rem_path) -> str:
    title = rem_title(idmap[rid], idmap)
    basename = rem_path[rid].stem
    if basename == title:
        return f"- [[{basename}]]"
    return f"- [[{basename}|{title}]]"


# ---- HTML document renderer -------------------------------------------------

def render_document_html(rid, idmap, children, doc_ids, owner_file,
                         html_paths_by_owner, image_cache, parent_doc_of,
                         rem_path_md):
    doc = idmap[rid]
    title = rem_title(idmap[rid], idmap)
    file_rel = owner_file[rid]

    body_parts: list[str] = []

    # Breadcrumbs back to ancestor documents.
    crumbs = []
    cur_id = rid
    chain: list[str] = []
    while True:
        parent = parent_doc_of.get(cur_id)
        if parent is None:
            break
        chain.append(parent)
        cur_id = parent
    if chain:
        crumb_html: list[str] = []
        for aid in reversed(chain):
            href = _html_link_href(file_rel, owner_file[aid],
                                   html_paths_by_owner)
            crumb_html.append(
                f'<a href="{html_mod.escape(href)}">'
                f'{html_mod.escape(rem_title(idmap[aid], idmap))}</a>'
            )
        index_href = os.path.relpath(VAULT / "Index.html",
                                     start=html_paths_by_owner[file_rel].parent)
        crumb_html.insert(0, f'<a href="{html_mod.escape(index_href)}">Index</a>')
        body_parts.append(
            '<nav class="breadcrumbs">'
            + " &rsaquo; ".join(crumb_html)
            + " &rsaquo; <span>"
            + html_mod.escape(title)
            + "</span></nav>"
        )
    else:
        index_href = os.path.relpath(VAULT / "Index.html",
                                     start=html_paths_by_owner[file_rel].parent)
        body_parts.append(
            f'<nav class="breadcrumbs">'
            f'<a href="{html_mod.escape(index_href)}">Index</a>'
            f' &rsaquo; <span>{html_mod.escape(title)}</span></nav>'
        )

    body_parts.append(
        f'<h1 class="doc-title">{html_mod.escape(title)}</h1>'
    )

    value = doc.get("value")
    if isinstance(value, list) and value:
        v_html = render_html(value, idmap, owner_file, file_rel,
                             image_cache, html_paths_by_owner).strip()
        if v_html:
            body_parts.append(f'<p class="doc-descriptor">{v_html}</p>')

    items_html = []
    for cid in children.get(rid, []):
        emit_child_html(cid, idmap, children, doc_ids, owner_file,
                        html_paths_by_owner, image_cache, file_rel,
                        items_html)
    if items_html:
        body_parts.append('<ul class="rem-tree">' + "".join(items_html)
                          + "</ul>")

    sub_links = []
    for cid in children.get(rid, []):
        if cid in doc_ids:
            sub_title = rem_title(idmap[cid], idmap)
            href = _html_link_href(file_rel, owner_file[cid],
                                   html_paths_by_owner)
            sub_links.append(
                f'<li><a class="q-link" href="{html_mod.escape(href)}">'
                f'{html_mod.escape(sub_title)}</a></li>'
            )
    if sub_links:
        body_parts.append(
            '<aside class="subdocs"><h2>Unterdokumente</h2><ul>'
            + "".join(sub_links) + "</ul></aside>"
        )

    return _html_page(title, "\n".join(body_parts))


def emit_child_html(rid, idmap, children, doc_ids, owner_file,
                    html_paths_by_owner, image_cache, current_owner,
                    out: list[str]):
    doc = idmap[rid]
    if not is_user_rem(doc):
        return
    if rid in doc_ids:
        return  # Document children appear in "Unterdokumente"
    if not has_visible_content(doc) and not children.get(rid):
        return

    # Separate child rems into floated images vs. normal children.
    raw_children = list(children.get(rid, []))
    floated_imgs: list[str] = []
    normal_children: list[str] = []
    for cid in raw_children:
        cdoc = idmap.get(cid)
        if cdoc is None or not is_user_rem(cdoc):
            continue
        if cid in doc_ids:
            normal_children.append(cid)
            continue
        if is_pure_image_rem(cdoc) and not children.get(cid):
            floated_imgs.append(cid)
        else:
            normal_children.append(cid)

    # Build the rem's own text.
    key_html = render_html(doc.get("key") or [], idmap, owner_file,
                           current_owner, image_cache,
                           html_paths_by_owner).strip()
    value = doc.get("value")
    value_html = ""
    if isinstance(value, list) and value:
        value_html = render_html(value, idmap, owner_file, current_owner,
                                 image_cache, html_paths_by_owner).strip()

    floated_html = ""
    for fid in floated_imgs:
        fdoc = idmap[fid]
        for it in fdoc.get("key") or []:
            if isinstance(it, dict) and it.get("i") == "i":
                src = _embed_image_html(it.get("url", ""), image_cache)
                if src:
                    floated_html += (
                        f'<img class="rem-img float-right" '
                        f'src="{src}" alt="">'
                    )

    # If the rem itself is a heading (every text span has same h), wrap text.
    heading_tag = _detect_heading_tag(doc.get("key") or [])

    # Build the visible text fragment.
    text_html = key_html
    if value_html:
        if text_html:
            text_html = (f'{text_html} <span class="descriptor-sep">::</span> '
                         f'{value_html}')
        else:
            text_html = value_html

    if heading_tag and text_html:
        text_html = f"<{heading_tag}>{text_html}</{heading_tag}>"

    classes = []
    if doc.get("noBullet"):
        classes.append("no-bullet")
    if heading_tag:
        classes.append("heading-rem")
    cls_attr = f' class="{" ".join(classes)}"' if classes else ""

    inner = floated_html
    if text_html:
        inner += f'<span class="rem-text">{text_html}</span>'

    sub_html: list[str] = []
    for cid in normal_children:
        if cid in doc_ids:
            sub_title = rem_title(idmap[cid], idmap)
            href = _html_link_href(current_owner, owner_file[cid],
                                   html_paths_by_owner)
            sub_html.append(
                f'<li><a class="q-link" href="{html_mod.escape(href)}">'
                f'{html_mod.escape(sub_title)}</a></li>'
            )
        else:
            emit_child_html(cid, idmap, children, doc_ids, owner_file,
                            html_paths_by_owner, image_cache,
                            current_owner, sub_html)
    if sub_html:
        inner += '<ul>' + "".join(sub_html) + '</ul>'

    out.append(f"<li{cls_attr}>{inner}</li>")


def _detect_heading_tag(items) -> str | None:
    """If every meaningful span has the same `h` value, return the HTML tag."""
    levels = set()
    has_text = False
    for it in items:
        if isinstance(it, str):
            if it.strip():
                return None
            continue
        if not isinstance(it, dict):
            continue
        if it.get("i") not in ("m", "x"):
            continue
        if not it.get("text", "").strip():
            continue
        has_text = True
        h = it.get("h")
        if not isinstance(h, int) or h not in HEADING_TAG:
            return None
        levels.add(h)
    if has_text and len(levels) == 1:
        return HEADING_TAG[next(iter(levels))]
    return None


def _html_page(title: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="de">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{html_mod.escape(title)}</title>\n"
        f"<style>{HTML_CSS}</style>\n"
        '<script>window.MathJax = {tex: {inlineMath: [["\\\\(","\\\\)"],'
        '["$","$"]], displayMath: [["\\\\[","\\\\]"],["$$","$$"]]}};</script>\n'
        '<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/'
        'tex-mml-chtml.js"></script>\n'
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n</html>\n"
    )


# ---- Index ------------------------------------------------------------------

def write_index_md(doc_ids, orphan_top, idmap, rem_path, parent_doc_of,
                   do_md, do_html):
    if do_md:
        path = VAULT / "Index.md"
        roots = sorted(
            (rid for rid in doc_ids if parent_doc_of.get(rid) is None),
            key=lambda r: rem_title(idmap[r], idmap).lower(),
        )
        body = ["# Index",
                "",
                "Auto-generiert aus der RemNote-Migration.",
                "",
                f"## Dokumente ({len(roots)})",
                "",
                "Die Hauptdokumente, jeweils mit verschachtelter Ordner-"
                "struktur entsprechend der RemNote-Hierarchie.",
                ""]
        for rid in roots:
            body.append(_doc_link_md(rid, idmap, rem_path))
        body.append("")
        body.append(f"## Lose Notizen ({len(orphan_top)})")
        body.append("")
        body.append("Top-Level-Rems aus RemNote, die nicht als Dokument"
                    " markiert waren – meist atomare Konzepte, die aus"
                    " anderen Notizen heraus referenziert werden.")
        body.append("")
        for rid in sorted(orphan_top,
                          key=lambda r: rem_title(idmap[r], idmap).lower()):
            body.append(f"- [[{rem_path[rid].stem}]]")
        body.append("")
        path.write_text("\n".join(body), encoding="utf-8")

    if do_html:
        roots = sorted(
            (rid for rid in doc_ids if parent_doc_of.get(rid) is None),
            key=lambda r: rem_title(idmap[r], idmap).lower(),
        )
        sections = []
        sections.append('<h1 class="doc-title">Index</h1>')
        sections.append('<p class="doc-descriptor">Auto-generiert aus der'
                        ' RemNote-Migration.</p>')
        sections.append(f"<h2>Dokumente ({len(roots)})</h2>")
        sections.append('<p>Die Hauptdokumente, jeweils mit verschachtelter'
                        ' Ordnerstruktur entsprechend der RemNote-'
                        'Hierarchie.</p>')
        sections.append("<ul>")
        for rid in roots:
            title = rem_title(idmap[rid], idmap)
            href = os.path.relpath(rem_path[rid].with_suffix(".html"),
                                   start=VAULT)
            sections.append(
                f'<li><a class="q-link" href="{html_mod.escape(href)}">'
                f'{html_mod.escape(title)}</a></li>'
            )
        sections.append("</ul>")
        sections.append(f"<h2>Lose Notizen ({len(orphan_top)})</h2>")
        sections.append("<p>Top-Level-Rems aus RemNote, die nicht als"
                        " Dokument markiert waren – meist atomare"
                        " Konzepte.</p>")
        sections.append("<ul>")
        for rid in sorted(orphan_top,
                          key=lambda r: rem_title(idmap[r], idmap).lower()):
            title = rem_title(idmap[rid], idmap)
            href = os.path.relpath(rem_path[rid].with_suffix(".html"),
                                   start=VAULT)
            sections.append(
                f'<li><a class="q-link" href="{html_mod.escape(href)}">'
                f'{html_mod.escape(title)}</a></li>'
            )
        sections.append("</ul>")
        (VAULT / "Index.html").write_text(
            _html_page("Index", "\n".join(sections)), encoding="utf-8",
        )


if __name__ == "__main__":
    sys.exit(main())
