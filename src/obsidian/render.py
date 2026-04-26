"""Markdown and HTML rendering for the Obsidian output mode."""
from __future__ import annotations
import base64
import html as html_mod
import mimetypes
import os
from pathlib import Path
from typing import Any

from ..constants import COLOR_MAP, HEADING_STYLE_MD, HEADING_TAG
from ..helpers import has_visible_content, is_user_rem, rem_title

# ---------------------------------------------------------------------------
# HTML page CSS
# ---------------------------------------------------------------------------

HTML_CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
               Helvetica, Arial, sans-serif;
  max-width: 980px; margin: 2em auto; padding: 0 2em 4em;
  line-height: 1.55; color: #1f1f1f; background: #fafafa;
}
nav.breadcrumbs { font-size: 0.9em; color: #777; margin-bottom: 1em; }
nav.breadcrumbs a { color: #5a4dca; text-decoration: none; }
nav.breadcrumbs a:hover { text-decoration: underline; }
h1.doc-title {
  font-size: 2em; margin: 0 0 0.6em;
  border-bottom: 2px solid #e3e3e3; padding-bottom: 0.25em; line-height: 1.2;
}
.doc-descriptor { color: #666; margin: 0 0 1.5em; font-style: italic; }
ul.rem-tree, ul.rem-tree ul { padding-left: 1.4em; margin: 0.3em 0; }
ul.rem-tree li { margin: 0.35em 0; position: relative; }
ul.rem-tree li::after { content: ""; display: block; clear: both; }
ul.rem-tree li.no-bullet { list-style: none; }
.descriptor-sep { color: #999; margin: 0 0.35em; }
img.rem-img { max-width: 100%; height: auto; border-radius: 5px; display: block; }
img.rem-img.float-right { float: right; max-width: 36%; margin: 0 0 0.7em 1.1em; }
img.rem-img.float-left  { float: left;  max-width: 36%; margin: 0 1.1em 0.7em 0; }
.q-link { color: #5a4dca; text-decoration: none; border-bottom: 1px dotted #b9b3eb; }
.q-link:hover { color: #3a2eb3; border-bottom-color: #3a2eb3; }
.url-card {
  display: inline-block; border: 1px solid #e1e1e1; padding: 0.5em 0.8em;
  border-radius: 6px; background: #fff; text-decoration: none; color: inherit;
  margin: 0.3em 0; max-width: 32em;
}
.url-card:hover { border-color: #5a4dca; }
.url-card .url-title { font-weight: 600; color: #5a4dca; display: block; }
.url-card .url-site  { font-size: 0.8em; color: #999; }
.url-card .url-desc  { font-size: 0.88em; color: #555; margin-top: 0.25em; }
.subdocs { margin-top: 2.5em; padding-top: 1.2em; border-top: 1px solid #e3e3e3; }
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

# ---------------------------------------------------------------------------
# Image loader
# ---------------------------------------------------------------------------

_LOAD_CACHE: dict[str, tuple[str, str] | None] = {}


def _load_local_image(url: str, files_dir: Path) -> tuple[str, str] | None:
    if url in _LOAD_CACHE:
        return _LOAD_CACHE[url]
    name = url[len("%LOCAL_FILE%"):]
    path = files_dir / name
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


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_md(items, idmap, owner_file, current_owner, image_cache,
              files_dir: Path) -> str:
    if not items:
        return ""
    parts: list[str] = []
    for it in items:
        if isinstance(it, str):
            parts.append(_md_escape(it))
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("i")
        if kind == "q":
            tid = it.get("_id")
            if it.get("textOfDeletedRem"):
                parts.append(render_md(it["textOfDeletedRem"], idmap,
                                       owner_file, current_owner,
                                       image_cache, files_dir))
            elif tid and tid in idmap:
                target = idmap[tid]
                title = rem_title(target, idmap)
                target_file = owner_file.get(tid)
                if not target_file or target_file == current_owner:
                    parts.append(_md_escape(title))
                else:
                    basename = Path(target_file).stem
                    parts.append(f"[[{basename}]]" if basename == title
                                 else f"[[{basename}|{title}]]")
        elif kind == "m":
            parts.append(_md_format_span(it))
        elif kind == "x":
            text = it.get("text", "")
            if not text:
                continue
            tc = it.get("tc")
            color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
            rendered = (f"\n$$\n{text}\n$$\n" if it.get("block")
                        else f"${text}$")
            if color:
                rendered = f'<span style="color:{color}">{rendered}</span>'
            parts.append(rendered)
        elif kind == "i":
            url = it.get("url", "")
            parts.append(_embed_image_md(url, image_cache, files_dir)
                         or (f"![]({url})" if url else ""))
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


def _md_format_span(it: dict) -> str:
    text = it.get("text", "")
    if not text:
        return ""
    text = _md_escape(text)
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


def _md_escape(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\n", "<br>")


def _embed_image_md(url: str, cache: dict[str, str],
                    files_dir: Path) -> str | None:
    if not url:
        return None
    if url in cache:
        return cache[url] or None
    if url.startswith("%LOCAL_FILE%"):
        data = _load_local_image(url, files_dir)
        if not data:
            cache[url] = ""
            return None
        mime, b64 = data
        result = f"![](data:{mime};base64,{b64})"
    else:
        result = f"![]({url})"
    cache[url] = result
    return result


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def render_html(items, idmap, owner_file, current_owner, image_cache,
                html_paths, files_dir: Path) -> str:
    if not items:
        return ""
    parts: list[str] = []
    for it in items:
        if isinstance(it, str):
            parts.append(_ht(it))
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("i")
        if kind == "q":
            tid = it.get("_id")
            if it.get("textOfDeletedRem"):
                parts.append(render_html(it["textOfDeletedRem"], idmap,
                                         owner_file, current_owner,
                                         image_cache, html_paths, files_dir))
            elif tid and tid in idmap:
                target = idmap[tid]
                title = rem_title(target, idmap)
                target_file = owner_file.get(tid)
                if not target_file or target_file == current_owner:
                    parts.append(_ht(title))
                else:
                    href = _html_link_href(current_owner, target_file,
                                           html_paths)
                    parts.append(
                        f'<a class="q-link" href="{html_mod.escape(href)}">'
                        f'{_ht(title)}</a>')
        elif kind == "m":
            parts.append(_html_format_span(it))
        elif kind == "x":
            text = it.get("text", "")
            if not text:
                continue
            tc = it.get("tc")
            color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
            rendered = (f'<div class="math-block">\\[{html_mod.escape(text)}\\]</div>'
                        if it.get("block") else f"\\({html_mod.escape(text)}\\)")
            if color:
                rendered = f'<span style="color:{color}">{rendered}</span>'
            parts.append(rendered)
        elif kind == "i":
            url = it.get("url", "")
            src = _embed_image_html(url, image_cache, files_dir)
            if src:
                parts.append(f'<img class="rem-img" src="{src}" alt="">')
        elif kind == "u":
            url = it.get("url", "")
            title = (it.get("title") or url).strip() or url
            site = (it.get("siteName") or "").strip()
            desc = (it.get("description") or "").strip()
            if url:
                if desc or site:
                    inner = f'<span class="url-title">{html_mod.escape(title)}</span>'
                    if site:
                        inner += f'<span class="url-site"> · {html_mod.escape(site)}</span>'
                    if desc:
                        inner += f'<span class="url-desc">{html_mod.escape(desc)}</span>'
                    parts.append(
                        f'<a class="url-card" href="{html_mod.escape(url)}" '
                        f'target="_blank" rel="noopener">{inner}</a>')
                else:
                    parts.append(
                        f'<a href="{html_mod.escape(url)}" '
                        f'target="_blank" rel="noopener">'
                        f'{html_mod.escape(title)}</a>')
        elif kind == "a":
            url = it.get("url", "")
            if url:
                parts.append(
                    f'<a href="{html_mod.escape(url)}" '
                    f'target="_blank" rel="noopener">'
                    f'{html_mod.escape(url)}</a>')
    return "".join(parts)


def _html_format_span(it: dict) -> str:
    text = it.get("text", "")
    if not text:
        return ""
    out = _ht(text)
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


def _ht(s: str) -> str:
    return html_mod.escape(s).replace("\r\n", "\n").replace("\n", "<br>")


def _embed_image_html(url: str, cache: dict[str, str],
                      files_dir: Path) -> str | None:
    if not url:
        return None
    if url in cache:
        return cache[url] or None
    if url.startswith("%LOCAL_FILE%"):
        data = _load_local_image(url, files_dir)
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
    src = html_paths.get(current_owner)
    dst = html_paths.get(target_owner)
    if src is None or dst is None:
        return Path(target_owner).with_suffix(".html").name
    return os.path.relpath(dst, start=src.parent)


def html_page(title: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="de">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{html_mod.escape(title)}</title>\n"
        f"<style>{HTML_CSS}</style>\n"
        '<script>window.MathJax = {tex: {inlineMath: [["\\\\(","\\\\)"],'
        '["$","$"]], displayMath: [["\\\\[","\\\\]"],["$$","$$"]]}};</script>\n'
        '<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/'
        'tex-mml-chtml.js"></script>\n'
        "</head>\n<body>\n"
        f"{body_html}\n"
        "</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# Document renderers
# ---------------------------------------------------------------------------

def render_document_md(rid, idmap, children, doc_ids, owner_file, rem_path,
                       image_cache, files_dir: Path) -> list[str]:
    doc = idmap[rid]
    file_rel = owner_file[rid]
    lines: list[str] = []

    value = doc.get("value")
    if isinstance(value, list) and value:
        v_md = render_md(value, idmap, owner_file, file_rel,
                         image_cache, files_dir).strip()
        if v_md:
            lines.append(v_md)
            lines.append("")

    body: list[str] = []
    for cid in children.get(rid, []):
        _emit_child_md(cid, idmap, children, doc_ids, owner_file, rem_path,
                       image_cache, file_rel, body, files_dir, depth=1)
    if body:
        lines.extend(body)

    sub_links: list[str] = []
    for cid in children.get(rid, []):
        if cid in doc_ids:
            sub_links.append(_doc_link_md(cid, idmap, rem_path))
    if sub_links:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("## Unterdokumente")
        lines.append("")
        lines.extend(sub_links)
    return lines


def _emit_child_md(rid, idmap, children, doc_ids, owner_file, rem_path,
                   image_cache, current_owner, lines, files_dir, *, depth):
    doc = idmap[rid]
    if not is_user_rem(doc) or rid in doc_ids:
        return
    if not has_visible_content(doc) and not children.get(rid):
        return

    key_md = render_md(doc.get("key") or [], idmap, owner_file,
                       current_owner, image_cache, files_dir).strip()
    value = doc.get("value")
    value_md = ""
    if isinstance(value, list) and value:
        value_md = render_md(value, idmap, owner_file, current_owner,
                             image_cache, files_dir).strip()
    text = key_md
    if value_md:
        text = f"{text} :: {value_md}" if text else value_md
    indent = "  " * (depth - 1)
    if text:
        lines.append(f"{indent}- {text}")
    for cid in children.get(rid, []):
        _emit_child_md(cid, idmap, children, doc_ids, owner_file, rem_path,
                       image_cache, current_owner, lines, files_dir,
                       depth=depth + 1)


def _doc_link_md(rid: str, idmap, rem_path) -> str:
    title = rem_title(idmap[rid], idmap)
    basename = rem_path[rid].stem
    return f"- [[{basename}]]" if basename == title else f"- [[{basename}|{title}]]"


def render_document_html(rid, idmap, children, doc_ids, owner_file,
                         html_paths_by_owner, image_cache, parent_doc_of,
                         rem_path_md, vault: Path, files_dir: Path) -> str:
    import html as html_mod  # noqa: F811 (shadow is fine here)
    doc = idmap[rid]
    title = rem_title(doc, idmap)
    file_rel = owner_file[rid]
    body_parts: list[str] = []

    # Breadcrumbs
    cur_id = rid
    chain: list[str] = []
    while True:
        parent = parent_doc_of.get(cur_id)
        if parent is None:
            break
        chain.append(parent)
        cur_id = parent
    index_href = os.path.relpath(vault / "Index.html",
                                 start=html_paths_by_owner[file_rel].parent)
    crumb_parts: list[str] = [
        f'<a href="{html_mod.escape(index_href)}">Index</a>']
    for aid in reversed(chain):
        href = _html_link_href(file_rel, owner_file[aid], html_paths_by_owner)
        crumb_parts.append(
            f'<a href="{html_mod.escape(href)}">'
            f'{html_mod.escape(rem_title(idmap[aid], idmap))}</a>')
    body_parts.append(
        '<nav class="breadcrumbs">'
        + " &rsaquo; ".join(crumb_parts)
        + f' &rsaquo; <span>{html_mod.escape(title)}</span></nav>')
    body_parts.append(f'<h1 class="doc-title">{html_mod.escape(title)}</h1>')

    value = doc.get("value")
    if isinstance(value, list) and value:
        v_html = render_html(value, idmap, owner_file, file_rel,
                             image_cache, html_paths_by_owner, files_dir).strip()
        if v_html:
            body_parts.append(f'<p class="doc-descriptor">{v_html}</p>')

    items_html: list[str] = []
    for cid in children.get(rid, []):
        _emit_child_html(cid, idmap, children, doc_ids, owner_file,
                         html_paths_by_owner, image_cache, file_rel,
                         items_html, files_dir)
    if items_html:
        body_parts.append(
            '<ul class="rem-tree">' + "".join(items_html) + "</ul>")

    sub_links: list[str] = []
    for cid in children.get(rid, []):
        if cid in doc_ids:
            sub_title = rem_title(idmap[cid], idmap)
            href = _html_link_href(file_rel, owner_file[cid],
                                   html_paths_by_owner)
            sub_links.append(
                f'<li><a class="q-link" href="{html_mod.escape(href)}">'
                f'{html_mod.escape(sub_title)}</a></li>')
    if sub_links:
        body_parts.append(
            '<aside class="subdocs"><h2>Unterdokumente</h2><ul>'
            + "".join(sub_links) + "</ul></aside>")

    return html_page(title, "\n".join(body_parts))


def _emit_child_html(rid, idmap, children, doc_ids, owner_file,
                     html_paths_by_owner, image_cache, current_owner,
                     out: list[str], files_dir: Path):
    import html as html_mod  # noqa: F811
    doc = idmap[rid]
    if not is_user_rem(doc) or rid in doc_ids:
        return
    if not has_visible_content(doc) and not children.get(rid):
        return

    raw_children = list(children.get(rid, []))
    floated_imgs: list[str] = []
    normal_children: list[str] = []
    for cid in raw_children:
        cdoc = idmap.get(cid)
        if cdoc is None or not is_user_rem(cdoc):
            continue
        if cid in doc_ids:
            normal_children.append(cid)
        elif _is_pure_image_rem(cdoc) and not children.get(cid):
            floated_imgs.append(cid)
        else:
            normal_children.append(cid)

    key_html = render_html(doc.get("key") or [], idmap, owner_file,
                           current_owner, image_cache,
                           html_paths_by_owner, files_dir).strip()
    value = doc.get("value")
    value_html = ""
    if isinstance(value, list) and value:
        value_html = render_html(value, idmap, owner_file, current_owner,
                                 image_cache, html_paths_by_owner,
                                 files_dir).strip()

    floated_html = ""
    for fid in floated_imgs:
        fdoc = idmap[fid]
        for it in fdoc.get("key") or []:
            if isinstance(it, dict) and it.get("i") == "i":
                src = _embed_image_html(it.get("url", ""), image_cache,
                                        files_dir)
                if src:
                    floated_html += (
                        f'<img class="rem-img float-right" src="{src}" alt="">')

    heading_tag = _detect_heading_tag(doc.get("key") or [])
    text_html = key_html
    if value_html:
        text_html = (f'{text_html} <span class="descriptor-sep">::</span> '
                     f'{value_html}' if text_html else value_html)
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
                f'{html_mod.escape(sub_title)}</a></li>')
        else:
            _emit_child_html(cid, idmap, children, doc_ids, owner_file,
                             html_paths_by_owner, image_cache,
                             current_owner, sub_html, files_dir)
    if sub_html:
        inner += "<ul>" + "".join(sub_html) + "</ul>"

    out.append(f"<li{cls_attr}>{inner}</li>")


def _is_pure_image_rem(doc: dict) -> bool:
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


def _detect_heading_tag(items) -> str | None:
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
