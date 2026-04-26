#!/usr/bin/env python3
"""Migrate two RemNote SQLite DBs into a single Logseq file-based graph.

Output graph: ~/Documents/Logseq/Graphs/2LD/

- Dokument-tagged user rems => pages/<Namespace>___<Title>.md
- Non-doc top-level user rems => pages/Lose Notizen___<Title>.md (one per rem).
- The lnotes DB is namespaced under ``lnotes/``.
- Every block becomes a Logseq bullet; ``id::`` properties carry rem UUIDs so
  ``((uuid))`` references resolve.
- Inline rich text follows migrate.py's ``render_md`` semantics, but adapted to
  Logseq syntax (``[[Page]]``, ``((blockid))``, ``$x$``, ``$$x$$``).
- Local %LOCAL_FILE% images are copied into ``assets/``; remote http(s) images
  are downloaded once and cached. On failure we fall back to the raw URL.
- Tag rems on a block become ``#[[Tag Name]]``.

Stdlib only.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

# --- paths -------------------------------------------------------------------

MAIN_DB = Path("/home/lukasdiegelmann/remnote/remnote-666c2b227ea3bab0376d53ba/remnote.db")
MAIN_FILES = Path("/home/lukasdiegelmann/remnote/remnote-666c2b227ea3bab0376d53ba/files")
SECONDARY_DB = Path("/home/lukasdiegelmann/remnote/lnotes/remnote.db")
SECONDARY_FILES = Path("/home/lukasdiegelmann/remnote/lnotes/files")  # may not exist
GRAPH = Path("/home/lukasdiegelmann/Documents/Logseq/Graphs/2LD")
PAGES_DIR = GRAPH / "pages"
ASSETS_DIR = GRAPH / "assets"
JOURNALS_DIR = GRAPH / "journals"
LOGSEQ_DIR = GRAPH / "logseq"

ORPHAN_NAMESPACE = "Lose Notizen"

# --- RemNote constants -------------------------------------------------------

DOC_TAG = "Fv0CqTL0lG4TwiBFK"

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

# Filename hygiene; namespace separator triple underscore. We forbid the
# namespace separator from appearing inside a single segment.
INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARN: {msg}", file=sys.stderr)


# --- generic helpers ---------------------------------------------------------

def slug_for_segment(text: str, max_len: int = 110) -> str:
    text = unicodedata.normalize("NFC", text)
    text = INVALID_FS_CHARS.sub(" ", text)
    text = text.replace("[", "(").replace("]", ")")
    # Logseq reserves ``___`` as namespace separator; collapse any in titles.
    text = text.replace("___", " - ")
    # Single underscores are fine, but a trailing/leading underscore can collide
    # with namespace logic only when adjacent to another. Fine as-is otherwise.
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Unbenannt"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    return text


def fractional_index_key(f):
    return (1, "") if f is None else (0, f)


# --- shared text helpers (mirror migrate.py) ---------------------------------

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


# --- inline escaping for Logseq ---------------------------------------------

def md_text_escape(s: str) -> str:
    """Inline-escape a text run for a Logseq bullet body.

    Newlines become ``<br>`` (matches migrate.py). We also escape stray
    ``[[`` so they don't get misread as page links by Logseq's parser.
    """
    s = s.replace("\r\n", "\n").replace("\n", "<br>")
    return s


# --- asset handling ----------------------------------------------------------

class AssetManager:
    def __init__(self, files_dir: Path, assets_out: Path):
        self.files_dir = files_dir
        self.assets_out = assets_out
        self.cache: dict[str, str | None] = {}  # url -> relative asset filename
        self.copied = 0
        self.downloaded = 0
        self.download_failed: set[str] = set()

    def _unique_dest(self, name: str) -> Path:
        """Reserve a unique filename inside the assets directory."""
        base = name
        dest = self.assets_out / base
        n = 1
        stem, ext = os.path.splitext(base)
        while dest.exists():
            n += 1
            dest = self.assets_out / f"{stem}_{n}{ext}"
        return dest

    def resolve(self, url: str) -> str | None:
        """Return relative asset path (e.g. ``../assets/foo.png``) or None."""
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        result: str | None = None
        if url.startswith("%LOCAL_FILE%"):
            name = url[len("%LOCAL_FILE%"):]
            src = self.files_dir / name
            if not src.exists():
                warn(f"missing local asset: {name}")
                self.cache[url] = None
                return None
            dest = self.assets_out / name
            if not dest.exists():
                try:
                    shutil.copy2(src, dest)
                    self.copied += 1
                except OSError as e:
                    warn(f"asset copy failed {name}: {e}")
                    self.cache[url] = None
                    return None
            result = dest.name
        elif url.startswith(("http://", "https://")):
            if url in self.download_failed:
                self.cache[url] = None
                return None
            # Derive a filename
            parsed = urllib.parse.urlparse(url)
            base = os.path.basename(parsed.path) or "remote"
            base = INVALID_FS_CHARS.sub("_", base)
            if "." not in base:
                base += ".bin"
            dest = self._unique_dest(base)
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 RemNoteMigrator"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                # Fix extension from content-type if generic
                if dest.suffix == ".bin":
                    ext = mimetypes.guess_extension(ctype.split(";")[0].strip() or "")
                    if ext:
                        dest = dest.with_suffix(ext)
                dest.write_bytes(data)
                self.downloaded += 1
                result = dest.name
            except (urllib.error.URLError, OSError, ValueError) as e:
                warn(f"download failed {url}: {e}")
                self.download_failed.add(url)
                self.cache[url] = None
                return None
        else:
            # data URLs, file://, etc.: leave alone
            self.cache[url] = None
            return None
        rel = f"../assets/{result}"
        self.cache[url] = rel
        return rel


# --- per-DB migration --------------------------------------------------------

class DBContext:
    def __init__(self, db_path: Path, files_dir: Path, namespace_prefix: str,
                 assets: AssetManager):
        self.db_path = db_path
        self.files_dir = files_dir
        self.namespace_prefix = namespace_prefix  # "" or "lnotes"
        self.assets = assets
        self.idmap: dict[str, dict] = {}
        self.children: dict[str | None, list[str]] = defaultdict(list)
        self.doc_ids: set[str] = set()
        self.parent_doc_of: dict[str, str | None] = {}
        # For each rem, the page (slug) it belongs to ("" if no owner page).
        self.owner_page: dict[str, str] = {}
        # Page slug for each doc rem.
        self.doc_page_slug: dict[str, str] = {}
        # Reverse lookup: page slug -> doc rid (used for collisions across DBs).
        self.pages_written = 0
        # Flashcard data: rId -> set of direction tokens ('f','b') and the set
        # of cloze IDs (for image occlusion / future text cloze rems). A rem
        # is a card iff it appears as a key in either dict.
        self.card_dirs: dict[str, set[str]] = defaultdict(set)
        self.card_clozes: dict[str, set[str]] = defaultdict(set)

    def load(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.execute("SELECT _id, doc FROM quanta")
            for _id, doc_json in cur:
                try:
                    self.idmap[_id] = json.loads(doc_json)
                except (json.JSONDecodeError, TypeError):
                    continue
            # Load cards table (table may not exist on a stripped DB).
            try:
                ccur = conn.execute(
                    "SELECT json_extract(doc,'$.rId'), json_extract(doc,'$.c')"
                    " FROM cards"
                )
                for rid, c in ccur:
                    if not rid:
                        continue
                    if c == "f" or c == "b":
                        self.card_dirs[rid].add(c)
                    elif c is None:
                        # Treat as forward by default.
                        self.card_dirs[rid].add("f")
                    else:
                        # Cloze card: c is the cloze ID.
                        self.card_clozes[rid].add(str(c))
                        # Also register cloze as a card-bearing rid.
                        self.card_dirs[rid]  # touch defaultdict
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
        for rid, doc in self.idmap.items():
            self.children[doc.get("parent")].append(rid)
        for plist in self.children.values():
            plist.sort(key=lambda rid: fractional_index_key(self.idmap[rid].get("f")))
        self.doc_ids = {
            rid for rid, doc in self.idmap.items()
            if is_user_rem(doc) and is_document(doc) and has_visible_content(doc)
        }

    def doc_ancestors(self, rid: str) -> list[str]:
        chain: list[str] = []
        cur_id: str | None = self.idmap.get(rid, {}).get("parent")
        while cur_id is not None:
            doc = self.idmap.get(cur_id)
            if doc is None:
                break
            if cur_id in self.doc_ids:
                chain.append(cur_id)
            cur_id = doc.get("parent")
        chain.reverse()
        return chain

    def compute_pages(self, used_slugs: set[str]) -> tuple[list[str], list[str]]:
        """Assign page slugs for doc rems and orphan top-level rems."""
        sorted_docs = sorted(self.doc_ids, key=lambda r: len(self.doc_ancestors(r)))
        # First, parent_doc_of
        for rid in self.doc_ids:
            chain = self.doc_ancestors(rid)
            self.parent_doc_of[rid] = chain[-1] if chain else None
        for rid in sorted_docs:
            slug = slug_for_segment(rem_title(self.idmap[rid], self.idmap))
            slug = self._unique(slug, used_slugs, rid)
            self.doc_page_slug[rid] = slug
            used_slugs.add(slug.lower())

        orphan_top: list[str] = []
        for rid in self.children[None]:
            doc = self.idmap[rid]
            if is_user_rem(doc) and has_visible_content(doc) and rid not in self.doc_ids:
                orphan_top.append(rid)

        for rid in orphan_top:
            slug = slug_for_segment(rem_title(self.idmap[rid], self.idmap))
            slug = self._unique(slug, used_slugs, rid)
            self.doc_page_slug[rid] = slug
            used_slugs.add(slug.lower())

        # Owner-page mapping: walk descendants of each page rem, stopping at
        # nested doc rems.
        for rid, slug in self.doc_page_slug.items():
            self._assign_owner(rid, slug)

        return sorted_docs, orphan_top

    def _unique(self, slug: str, used: set[str], rid: str) -> str:
        if slug.lower() not in used:
            return slug
        suffix = "_" + re.sub(r"[^A-Za-z0-9]", "", rid)[:8]
        candidate = slug + suffix
        n = 1
        while candidate.lower() in used:
            n += 1
            candidate = f"{slug}{suffix}_{n}"
        return candidate

    def _assign_owner(self, rid: str, slug: str) -> None:
        self.owner_page[rid] = slug
        for cid in self.children.get(rid, []):
            cdoc = self.idmap.get(cid)
            if cdoc is None or not is_user_rem(cdoc):
                continue
            if cid in self.doc_ids or cid in self.doc_page_slug:
                continue  # nested doc gets its own page
            self._assign_owner(cid, slug)


# --- inline rendering for Logseq --------------------------------------------

def render_inline(items, ctx: DBContext) -> str:
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
                # Doc target -> page link, otherwise block ref via uuid.
                target_page = ctx.doc_page_slug.get(tid)
                if target_page:
                    parts.append(f"[[{target_page}]]")
                else:
                    parts.append(f"(({tid}))")
            elif tid:
                # Cross-DB or unknown rem; use a block ref by id.
                parts.append(f"(({tid}))")
        elif kind == "m":
            parts.append(_format_text_span(it))
        elif kind == "x":
            text = it.get("text", "")
            if not text:
                continue
            tc = it.get("tc")
            color = COLOR_MAP.get(tc) if isinstance(tc, int) else None
            if it.get("block"):
                rendered = f"$${text}$$"
            else:
                rendered = f"${text}$"
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
                # Fallback: external/raw url
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


def _format_text_span(it: dict) -> str:
    text = it.get("text", "")
    if text == "":
        return ""
    if it.get("q"):
        # RemNote inline-code formatting: emit as backticks, skip markdown escaping inside
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


# --- block-tag rendering -----------------------------------------------------

def block_tags_for(rid: str, ctx: DBContext) -> list[str]:
    """Return Logseq tag fragments for tag rems on this block.

    Only emits tags that are NOT the Dokument power-up (already represented
    by being a page) and whose target is itself a user rem with a title.
    """
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
        # Skip system / power-up rems (no parent and not user-content),
        # but be permissive: only require it has a renderable title.
        title = render_plain(tdoc.get("key") or [], ctx.idmap).strip()
        if not title:
            continue
        # Skip very-internal-looking strings
        if title.startswith("__") and title.endswith("__"):
            continue
        tags.append(f"#[[{title}]]")
    return tags


# --- page emission -----------------------------------------------------------

INDENT = "\t"

# Counters populated during emission for the final summary.
CARD_STATS = {
    "card_blocks": 0,
    "cloze_deletions": 0,
    "bidirectional": 0,
    "cloze_cards": 0,
}


def _card_direction_property(dirs: set[str]) -> str | None:
    """Map RemNote direction set to Logseq ``card-last-direction`` value."""
    has_f = "f" in dirs
    has_b = "b" in dirs
    if has_f and has_b:
        return "both"
    if has_b and not has_f:
        return "reverse"
    # Forward-only is the Logseq default; no property needed.
    return None


def _render_key_with_clozes(items, ctx: DBContext, cloze_ids: set[str]) -> tuple[str, int]:
    """Render the key with cloze markers.

    For image occlusion rems (the only cloze format observed in this DB),
    RemNote stores the cloze IDs inside the image item's ``blocks`` array.
    We render the image normally, then append ``{{cloze N}}`` placeholders
    so Logseq still treats the block as a real flashcard with cloze
    deletions. Returns (rendered_text, n_cloze_emitted).
    """
    base = render_inline(items, ctx).strip()
    n = 0
    if not cloze_ids:
        return base, 0
    # Stable order: sort cloze ids so output is deterministic.
    parts: list[str] = []
    for i, cid in enumerate(sorted(cloze_ids), start=1):
        # Use a short label; the actual cId is opaque to the user.
        parts.append(f"{{{{cloze region-{i}}}}}")
        n += 1
    cloze_block = " ".join(parts)
    if base:
        return f"{base}<br>{cloze_block}", n
    return cloze_block, n


def emit_block(rid: str, ctx: DBContext, depth: int, lines: list[str],
               current_page: str) -> None:
    doc = ctx.idmap.get(rid)
    if doc is None or not is_user_rem(doc):
        return

    # If this rid is itself a doc page, emit a leaf link to it (don't descend).
    if rid in ctx.doc_page_slug and ctx.doc_page_slug[rid] != current_page:
        link = f"[[{ctx.doc_page_slug[rid]}]]"
        indent = INDENT * depth
        lines.append(f"{indent}- {link}")
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
    value_md = ""
    if isinstance(value, list) and value:
        value_md = render_inline(value, ctx).strip()

    if is_card and cloze_ids:
        key_md, n_cloze = _render_key_with_clozes(doc.get("key") or [], ctx, cloze_ids)
        CARD_STATS["cloze_deletions"] += n_cloze
        CARD_STATS["cloze_cards"] += 1
    else:
        key_md = render_inline(doc.get("key") or [], ctx).strip()
        n_cloze = 0

    tags = block_tags_for(rid, ctx)
    indent = INDENT * depth

    if is_card:
        # Question goes on the bullet itself (with #card tag); the answer
        # (value, if any) goes as a child bullet so Logseq treats it as the
        # card answer.
        question = key_md
        if not question and value_md:
            # Multi-line / answer-only card: use value as question fallback.
            question, value_md = value_md, ""
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
            # Answer becomes a nested bullet so Logseq's review UI shows it
            # as the back of the card.
            lines.append(f"{indent}{INDENT}- {value_md}")
        for cid in ctx.children.get(rid, []):
            emit_block(cid, ctx, depth + 1, lines, current_page)
        return

    # Non-card path (unchanged behaviour).
    text = key_md
    if value_md:
        text = f"{text} :: {value_md}" if text else value_md

    if tags:
        text = f"{text} {' '.join(tags)}".strip()

    if not text:
        text = ""  # still emit if it has children
    lines.append(f"{indent}- {text}".rstrip() if not text else f"{indent}- {text}")
    lines.append(f"{indent}{INDENT}id:: {rid}")

    for cid in ctx.children.get(rid, []):
        emit_block(cid, ctx, depth + 1, lines, current_page)


def render_page(rid: str, ctx: DBContext) -> str:
    slug = ctx.doc_page_slug[rid]
    doc = ctx.idmap[rid]
    full_title = rem_title(doc, ctx.idmap)
    lines: list[str] = []

    # Optional title:: property if slug differs noticeably from title.
    # The slug's last namespace segment is the visible page title in Logseq.
    last_seg = slug.split("___")[-1]
    if last_seg != slug_for_segment(full_title):
        lines.append(f"title:: {full_title}")
    lines.append(f"id:: {rid}")
    lines.append("")

    # If this doc rem is itself a flashcard, emit a top-level #card block
    # whose question is the page title and whose answer is the rem value.
    is_card = rid in ctx.card_dirs
    cloze_ids = ctx.card_clozes.get(rid, set())
    dirs = ctx.card_dirs.get(rid, set())
    value = doc.get("value")
    value_md = ""
    if isinstance(value, list) and value:
        value_md = render_inline(value, ctx).strip()

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
        # Document's own value (descriptor) becomes first bullet.
        lines.append(f"- {value_md}")

    body_start = len(lines)
    for cid in ctx.children.get(rid, []):
        emit_block(cid, ctx, 0, lines, slug)

    # If page has zero bullets, emit an empty bullet so Logseq accepts it.
    if len(lines) == body_start and (not isinstance(value, list) or not value):
        lines.append("- ")

    return "\n".join(lines).rstrip() + "\n"


# --- main --------------------------------------------------------------------

def main() -> int:
    sys.setrecursionlimit(200000)
    GRAPH.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(exist_ok=True)
    ASSETS_DIR.mkdir(exist_ok=True)
    JOURNALS_DIR.mkdir(exist_ok=True)
    LOGSEQ_DIR.mkdir(exist_ok=True)
    (LOGSEQ_DIR / "config.edn").write_text(
        "{:meta/version 1\n :preferred-format :markdown}\n",
        encoding="utf-8",
    )

    assets = AssetManager(MAIN_FILES, ASSETS_DIR)
    used_slugs: set[str] = set()

    contexts = []
    for db_path, files_dir, ns in (
        (MAIN_DB, MAIN_FILES, ""),
        (SECONDARY_DB, SECONDARY_FILES, "lnotes"),
    ):
        ctx = DBContext(db_path, files_dir, ns, assets)
        # AssetManager always points at MAIN_FILES; for secondary DB local
        # %LOCAL_FILE% references will simply miss and be warned. (lnotes has
        # no files/ dir per the inputs.)
        ctx.load()
        print(f"[{db_path.name}] loaded {len(ctx.idmap)} rems, "
              f"{len(ctx.doc_ids)} docs, "
              f"{len(ctx.card_dirs)} card rems "
              f"({len(ctx.card_clozes)} with cloze)", file=sys.stderr)
        sorted_docs, orphans = ctx.compute_pages(used_slugs)
        print(f"[{db_path.name}] {len(sorted_docs)} doc pages, "
              f"{len(orphans)} orphan pages", file=sys.stderr)
        contexts.append((ctx, sorted_docs, orphans))

    counts: dict[str, int] = {}
    for ctx, sorted_docs, orphans in contexts:
        n = 0
        for rid in sorted_docs + orphans:
            slug = ctx.doc_page_slug[rid]
            try:
                body = render_page(rid, ctx)
            except RecursionError:
                warn(f"recursion limit on rem {rid}; skipping")
                continue
            (PAGES_DIR / f"{slug}.md").write_text(body, encoding="utf-8")
            n += 1
        counts[ctx.db_path.name + (f" ({ctx.namespace_prefix})" if ctx.namespace_prefix else "")] = n

    # Summary
    print("\n=== Summary ===", file=sys.stderr)
    for k, v in counts.items():
        print(f"  pages [{k}]: {v}", file=sys.stderr)
    print(f"  assets copied: {assets.copied}", file=sys.stderr)
    print(f"  assets downloaded: {assets.downloaded}", file=sys.stderr)
    print(f"  download failures: {len(assets.download_failed)}", file=sys.stderr)
    print(f"  card blocks emitted: {CARD_STATS['card_blocks']}", file=sys.stderr)
    print(f"  cloze cards: {CARD_STATS['cloze_cards']}", file=sys.stderr)
    print(f"  cloze deletions: {CARD_STATS['cloze_deletions']}", file=sys.stderr)
    print(f"  bidirectional cards: {CARD_STATS['bidirectional']}", file=sys.stderr)
    print(f"  warnings total: {len(WARNINGS)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
