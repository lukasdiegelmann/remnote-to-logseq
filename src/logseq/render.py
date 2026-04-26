"""Shared Logseq inline rendering and block emission.

Works with both DBContext (SQLite) and RemExport (.rem ZIP) via duck typing.
The extra .rem-only fields (skip_rids, pdf_path, pdf_info, pdf_base) are
accessed via getattr so DBContext stays unchanged.
"""
from __future__ import annotations
import json
from typing import Any

from ..constants import COLOR_MAP, DOC_TAG, HEADING_STYLE_MD, PDF_HIGHLIGHT_TAG
from ..helpers import (has_visible_content, is_user_rem, md_text_escape,
                       rem_title, render_plain, slug_for_segment)

INDENT = "\t"

CARD_STATS = {
    "card_blocks": 0,
    "cloze_deletions": 0,
    "bidirectional": 0,
    "cloze_cards": 0,
}


def reset_card_stats() -> None:
    for k in CARD_STATS:
        CARD_STATS[k] = 0


# ---------------------------------------------------------------------------
# Inline rendering
# ---------------------------------------------------------------------------

def _format_text_span(it: dict) -> str:
    text = it.get("text", "")
    if text == "":
        return ""
    if it.get("q"):
        return f"`{text}`"
    text = md_text_escape(text)
    inline_url = it.get("iUrl")
    if inline_url:
        text = f"[{text}]({inline_url})"
    if it.get("b"):
        text = f"**{text}**"
    if it.get("l"):
        text = f"*{text}*"
    if it.get("u"):
        text = f"<u>{text}</u>"
    if it.get("str"):
        text = f"~~{text}~~"
    if it.get("sup"):
        text = f"<sup>{text}</sup>"
    if it.get("sub"):
        text = f"<sub>{text}</sub>"
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


def render_inline(items, ctx: Any) -> str:
    if not items:
        return ""
    parts: list[str] = []
    for it in items:
        if isinstance(it, str):
            parts.append(md_text_escape(it))
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("i")
        if kind == "q":
            tid = it.get("_id")
            if it.get("textOfDeletedRem"):
                parts.append(render_inline(it["textOfDeletedRem"], ctx))
            elif tid and tid in ctx.idmap:
                target_page = ctx.doc_page_slug.get(tid)
                if target_page:
                    parts.append(f"[[{target_page}]]")
                else:
                    parts.append(f"(({tid}))")
            elif tid:
                parts.append(f"(({tid}))")
        elif kind == "m":
            parts.append(_format_text_span(it))
        elif kind == "x":
            text = it.get("text", "")
            if not text:
                continue
            tc = it.get("tc")
            color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
            rendered = f"$${text}$$" if it.get("block") else f"${text}$"
            if color:
                rendered = f'<span style="color:{color}">{rendered}</span>'
            parts.append(rendered)
        elif kind == "i":
            url = it.get("url", "")
            rel = ctx.assets.resolve(url)
            alt = (it.get("title") or "").strip()
            if rel:
                parts.append(f"![{md_text_escape(alt)}]({rel})")
            elif url:
                parts.append(f"![{md_text_escape(alt)}]({url})")
        elif kind == "u":
            url = it.get("url", "")
            title = (it.get("title") or url).strip() or url
            if url:
                parts.append(f"[{md_text_escape(title)}]({url})")
        elif kind == "a":
            url = it.get("url", "")
            if url:
                parts.append(f"[{url}]({url})")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Block tags
# ---------------------------------------------------------------------------

def block_tags_for(rid: str, ctx: Any) -> list[str]:
    doc = ctx.idmap.get(rid) or {}
    tp = doc.get("tp") or {}
    if not isinstance(tp, dict):
        return []
    tags = []
    for tag_id in tp.keys():
        if tag_id == DOC_TAG:
            continue
        tdoc = ctx.idmap.get(tag_id)
        if tdoc is None:
            continue
        title = render_plain(tdoc.get("key") or [], ctx.idmap).strip()
        if not title or (title.startswith("__") and title.endswith("__")):
            continue
        tags.append(f"#[[{title}]]")
    return tags


# ---------------------------------------------------------------------------
# Card helpers
# ---------------------------------------------------------------------------

def _card_direction_property(dirs: set[str]) -> str | None:
    has_f = "f" in dirs
    has_b = "b" in dirs
    if has_f and has_b:
        return "both"
    if has_b and not has_f:
        return "reverse"
    return None


def _render_key_with_clozes(items, ctx: Any, cloze_ids: set[str]) -> tuple[str, int]:
    base = render_inline(items, ctx).strip()
    if not cloze_ids:
        return base, 0
    parts = [f"{{{{cloze region-{i}}}}}" for i, _ in enumerate(sorted(cloze_ids), 1)]
    cloze_block = " ".join(parts)
    if base:
        return f"{base}<br>{cloze_block}", len(parts)
    return cloze_block, len(parts)


# ---------------------------------------------------------------------------
# PDF highlight helpers (.rem exports only)
# ---------------------------------------------------------------------------

def _highlight_page_number(rid: str, ctx: Any) -> int | None:
    for cid in ctx.children.get(rid, []):
        c = ctx.idmap.get(cid)
        if not c or c.get("rcrp") != "n.d":
            continue
        v = c.get("value")
        if not (isinstance(v, list) and v and isinstance(v[0], str)):
            continue
        try:
            data = json.loads(v[0])
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        pn = data.get("position", {}).get("pageNumber")
        if pn is None:
            pn = data.get("pageNumber")
        if isinstance(pn, (int, float)):
            return int(pn)
    return None


# ---------------------------------------------------------------------------
# Block + page emission
# ---------------------------------------------------------------------------

def emit_block(rid: str, ctx: Any, depth: int, lines: list[str],
               current_page: str) -> None:
    doc = ctx.idmap.get(rid)
    if doc is None or not is_user_rem(doc):
        return
    if rid in getattr(ctx, "skip_rids", set()):
        return
    if rid in ctx.doc_page_slug and ctx.doc_page_slug[rid] != current_page:
        indent = INDENT * depth
        lines.append(f"{indent}- [[{ctx.doc_page_slug[rid]}]]")
        lines.append(f"{indent}{INDENT}id:: {rid}")
        return

    has_kids = bool(ctx.children.get(rid))
    if not has_visible_content(doc) and not has_kids:
        return

    is_card = rid in ctx.card_dirs
    cloze_ids = ctx.card_clozes.get(rid, set())
    dirs = ctx.card_dirs.get(rid, set())
    direction_prop = _card_direction_property(dirs) if is_card else None

    value = doc.get("value")
    value_md = render_inline(value, ctx).strip() if isinstance(value, list) and value else ""

    if is_card and cloze_ids:
        key_md, n_cloze = _render_key_with_clozes(doc.get("key") or [], ctx, cloze_ids)
        CARD_STATS["cloze_deletions"] += n_cloze
        CARD_STATS["cloze_cards"] += 1
    else:
        key_md = render_inline(doc.get("key") or [], ctx).strip()

    # Prepend page number for PDF highlights (.rem export only)
    doc_tp = doc.get("tp") or {}
    if isinstance(doc_tp, dict) and PDF_HIGHLIGHT_TAG in doc_tp:
        pn = _highlight_page_number(rid, ctx)
        if pn is not None:
            key_md = f"**S. {pn}:** {key_md}" if key_md else f"**S. {pn}**"

    tags = block_tags_for(rid, ctx)
    indent = INDENT * depth

    if is_card:
        question = key_md or value_md
        if key_md and not value_md:
            pass
        elif not key_md and value_md:
            value_md = ""
        question_text = (question + " #card").strip()
        if tags:
            question_text = f"{question_text} {' '.join(tags)}".strip()
        lines.append(f"{indent}- {question_text}")
        lines.append(f"{indent}{INDENT}id:: {rid}")
        if direction_prop:
            lines.append(f"{indent}{INDENT}card-last-direction:: {direction_prop}")
        if direction_prop == "both":
            CARD_STATS["bidirectional"] += 1
        CARD_STATS["card_blocks"] += 1
        if value_md:
            lines.append(f"{indent}{INDENT}- {value_md}")
        for cid in ctx.children.get(rid, []):
            emit_block(cid, ctx, depth + 1, lines, current_page)
        return

    text = key_md
    if value_md:
        text = f"{text} :: {value_md}" if text else value_md
    if tags:
        text = f"{text} {' '.join(tags)}".strip()
    lines.append(f"{indent}- {text}" if text else f"{indent}-")
    lines.append(f"{indent}{INDENT}id:: {rid}")

    pdf_rel = getattr(ctx, "pdf_path", {}).get(rid)
    if pdf_rel:
        info = getattr(ctx, "pdf_info", {}).get(rid, {})
        name = info.get("name") or "document.pdf"
        lines.append(f"{indent}{INDENT}- [{md_text_escape(name)}]({pdf_rel})")
        base = getattr(ctx, "pdf_base", {}).get(rid)
        if base:
            lines.append(f"{indent}{INDENT}{INDENT}- [[hls__{base}]]")

    for cid in ctx.children.get(rid, []):
        emit_block(cid, ctx, depth + 1, lines, current_page)


def render_page(rid: str, ctx: Any) -> str:
    slug = ctx.doc_page_slug[rid]
    doc = ctx.idmap[rid]
    full_title = rem_title(doc, ctx.idmap)
    lines: list[str] = []

    last_seg = slug.split("___")[-1]
    if last_seg != slug_for_segment(full_title):
        lines.append(f"title:: {full_title}")
    lines.append(f"id:: {rid}")
    lines.append("")

    is_card = rid in ctx.card_dirs
    cloze_ids = ctx.card_clozes.get(rid, set())
    dirs = ctx.card_dirs.get(rid, set())
    value = doc.get("value")
    value_md = render_inline(value, ctx).strip() if isinstance(value, list) and value else ""

    if is_card:
        if cloze_ids:
            q, n_cloze = _render_key_with_clozes(doc.get("key") or [], ctx, cloze_ids)
            CARD_STATS["cloze_deletions"] += n_cloze
            CARD_STATS["cloze_cards"] += 1
        else:
            q = render_inline(doc.get("key") or [], ctx).strip() or full_title
        direction_prop = _card_direction_property(dirs)
        lines.append(f"- {q} #card")
        if direction_prop:
            lines.append(f"\tcard-last-direction:: {direction_prop}")
        if direction_prop == "both":
            CARD_STATS["bidirectional"] += 1
        CARD_STATS["card_blocks"] += 1
        if value_md:
            lines.append(f"\t- {value_md}")
    elif value_md:
        lines.append(f"- {value_md}")

    pdf_rel = getattr(ctx, "pdf_path", {}).get(rid)
    if pdf_rel:
        info = getattr(ctx, "pdf_info", {}).get(rid, {})
        name = info.get("name") or "document.pdf"
        lines.append(f"- [{md_text_escape(name)}]({pdf_rel})")
        base = getattr(ctx, "pdf_base", {}).get(rid)
        if base:
            lines.append(f"\t- [[hls__{base}]]")

    body_start = len(lines)
    for cid in ctx.children.get(rid, []):
        emit_block(cid, ctx, 0, lines, slug)

    if len(lines) == body_start and (not isinstance(value, list) or not value):
        lines.append("- ")

    return "\n".join(lines).rstrip() + "\n"
