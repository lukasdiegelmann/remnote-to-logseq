#!/usr/bin/env python3
"""Migrate a RemNote ``.rem`` JSON export into a Logseq file-based graph.

A ``.rem`` file is a ZIP archive containing ``rem.json`` (documents),
``cards.json`` (flashcards), ``metadata.json`` and assorted user data.
This script consumes that archive directly — no unpacking required — and
writes a Logseq graph (``pages/``, ``assets/``, ``logseq/config.edn``).

Mirrors ``migrate_logseq.py`` for rendering semantics so output matches what
the SQLite-based migrator produces, but reads from the JSON export so the
extra rems present only in the .rem file are picked up.

Stdlib only.

Usage:
    migrate_rem.py REM_FILE [--graph DIR] [--local-files DIR] [--no-download]

Notes on assets:
- The .rem export does NOT contain image/PDF blobs. ``%LOCAL_FILE%`` refs are
  resolved against ``--local-files DIR`` (e.g. an old RemNote ``files/`` dir);
  unresolved ones produce a warning and a raw-URL fallback.
- ``http(s)://`` image URLs are downloaded into ``assets/`` once unless
  ``--no-download`` is given.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path

# Stable namespace for deriving UUIDs from RemNote rem ids. Don't change.
HL_UUID_NS = uuid.UUID("d8d5a4ba-1c3e-4dc7-9a14-2d2b9a9b1e8f")


def rem_uuid(rid: str) -> str:
    return str(uuid.uuid5(HL_UUID_NS, rid))


def _fmt_dur(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class Progress:
    """Single-line stderr progress bar with ETA."""

    def __init__(self, total: int, label: str, width: int = 30):
        self.total = max(total, 0)
        self.label = label
        self.width = width
        self.n = 0
        self.start = time.monotonic()
        self.last_print = 0.0
        self.enabled = self.total > 0
        self.tty = sys.stderr.isatty()

    def tick(self, by: int = 1, suffix: str = "") -> None:
        self.n += by
        if not self.enabled:
            return
        now = time.monotonic()
        # TTY: refresh every 0.1s; non-TTY (log file): every 5s.
        min_gap = 0.1 if self.tty else 5.0
        if now - self.last_print < min_gap and self.n < self.total:
            return
        self.last_print = now
        elapsed = now - self.start
        frac = self.n / self.total if self.total else 1.0
        filled = int(self.width * frac)
        bar = "#" * filled + "-" * (self.width - filled)
        eta_s = _fmt_dur(elapsed / self.n * (self.total - self.n)) if self.n else "?"
        msg = (f"{self.label} [{bar}] {self.n}/{self.total} "
               f"elapsed {_fmt_dur(elapsed)} ETA {eta_s}")
        if suffix:
            msg += f" {suffix[:40]}"
        if self.tty:
            sys.stderr.write("\r" + msg.ljust(120)[:120])
        else:
            sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    def done(self) -> None:
        if not self.enabled:
            return
        elapsed = time.monotonic() - self.start
        line = f"{self.label}: {self.n}/{self.total} done in {_fmt_dur(elapsed)}"
        if self.tty:
            sys.stderr.write("\r" + line.ljust(120)[:120] + "\n")
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

# --- RemNote constants -------------------------------------------------------

DOC_TAG = "Fv0CqTL0lG4TwiBFK"
PDF_HIGHLIGHT_TAG = "YzACaf58NQJFRasvl"  # "PDF-Markierung"
PDF_PAGE_TAG = "7HXKPpjC9xxe0S265"       # "PDF-Seitenzahl"

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
    text = text.replace("___", " - ")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Unbenannt"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    return text


def fractional_index_key(f):
    return (1, "") if f is None else (0, f)


# --- text rendering ----------------------------------------------------------

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


def md_text_escape(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\n", "<br>")


# --- assets ------------------------------------------------------------------

class AssetManager:
    def __init__(self, local_files: Path | None, assets_out: Path,
                 download: bool):
        self.local_files = local_files
        self.assets_out = assets_out
        self.download = download
        self.cache: dict[str, str | None] = {}
        self.copied = 0
        self.downloaded = 0
        self.skipped_existing = 0
        self.download_failed: set[str] = set()
        self.dl_progress: Progress | None = None
        # Names already assigned to a URL during this run. Lets us reuse
        # existing files in assets/ across runs deterministically: the first
        # URL claims `foo.pdf`, the next collision becomes `foo_2.pdf`, etc.
        self.claimed_names: set[str] = set()

    def _claim_name(self, name: str) -> Path:
        """Pick an on-disk name for `name`, claiming it for this run.

        Existing files at the chosen name are kept (idempotent re-runs);
        we only bump the suffix when a different URL has already claimed it.
        """
        stem, ext = os.path.splitext(name)
        candidate = name
        n = 1
        while candidate.lower() in self.claimed_names:
            n += 1
            candidate = f"{stem}_{n}{ext}"
        self.claimed_names.add(candidate.lower())
        return self.assets_out / candidate

    def resolve_pdf(self, url: str, preferred_name: str) -> str | None:
        """Resolve a PDF URL using a human-readable filename.

        Cached by URL so repeated lookups for the same source PDF reuse the
        first chosen on-disk filename.
        """
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        safe = INVALID_FS_CHARS.sub("_", preferred_name) or "document.pdf"
        if not safe.lower().endswith(".pdf"):
            safe += ".pdf"
        if url.startswith("%LOCAL_FILE%"):
            hashname = url[len("%LOCAL_FILE%"):]
            src = (self.local_files / hashname) if self.local_files else None
            dest = self._claim_name(safe)
            if dest.exists():
                self.skipped_existing += 1
            elif src and src.exists():
                try:
                    shutil.copy2(src, dest)
                    self.copied += 1
                except OSError as e:
                    warn(f"PDF copy failed {safe}: {e}")
                    self.cache[url] = None
                    return None
            else:
                if not self.download:
                    self.cache[url] = None
                    return None
                s3_url = f"https://remnote-user-data.s3.amazonaws.com/{hashname}"
                if s3_url in self.download_failed:
                    self.cache[url] = None
                    return None
                if self.dl_progress:
                    self.dl_progress.tick(suffix=safe)
                if not self._download(s3_url, dest, log_label=hashname, timeout=60):
                    self.cache[url] = None
                    return None
        elif url.startswith(("http://", "https://")):
            dest = self._claim_name(safe)
            if dest.exists():
                self.skipped_existing += 1
            else:
                if not self.download or url in self.download_failed:
                    self.cache[url] = None
                    return None
                if self.dl_progress:
                    self.dl_progress.tick(suffix=safe)
                if not self._download(url, dest, log_label=url, timeout=60):
                    self.cache[url] = None
                    return None
        else:
            self.cache[url] = None
            return None
        rel = f"../assets/{dest.name}"
        self.cache[url] = rel
        return rel

    def _download(self, url: str, dest: Path, *, log_label: str,
                  timeout: int = 20) -> bool:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 RemNoteMigrator"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            dest.write_bytes(data)
            self.downloaded += 1
            return True
        except (urllib.error.URLError, OSError, ValueError) as e:
            warn(f"download failed {log_label}: {e}")
            self.download_failed.add(url)
            return False

    def resolve(self, url: str) -> str | None:
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        if url.startswith("%LOCAL_FILE%"):
            name = url[len("%LOCAL_FILE%"):]
            dest = self._claim_name(name)
            if dest.exists():
                self.skipped_existing += 1
            else:
                src = (self.local_files / name) if self.local_files else None
                if src and src.exists():
                    try:
                        shutil.copy2(src, dest)
                        self.copied += 1
                    except OSError as e:
                        warn(f"asset copy failed {name}: {e}")
                        self.cache[url] = None
                        return None
                elif self.download:
                    s3_url = f"https://remnote-user-data.s3.amazonaws.com/{name}"
                    if s3_url in self.download_failed:
                        self.cache[url] = None
                        return None
                    if self.dl_progress:
                        self.dl_progress.tick(suffix=name)
                    if not self._download(s3_url, dest, log_label=name, timeout=30):
                        self.cache[url] = None
                        return None
                else:
                    warn(f"unresolved local asset (no --local-files, downloads off): {name}")
                    self.cache[url] = None
                    return None
            rel = f"../assets/{dest.name}"
            self.cache[url] = rel
            return rel
        elif url.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(url)
            base = os.path.basename(parsed.path) or "remote"
            base = INVALID_FS_CHARS.sub("_", base)
            if "." not in base:
                base += ".bin"
            dest = self._claim_name(base)
            if dest.exists():
                self.skipped_existing += 1
            else:
                if not self.download or url in self.download_failed:
                    self.cache[url] = None
                    return None
                if self.dl_progress:
                    short = os.path.basename(parsed.path) or url
                    self.dl_progress.tick(suffix=short)
                # Inline download to also sniff Content-Type for .bin → ext.
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0 RemNoteMigrator"},
                    )
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        data = resp.read()
                        ctype = resp.headers.get("Content-Type", "")
                    if dest.suffix == ".bin":
                        ext = mimetypes.guess_extension(ctype.split(";")[0].strip() or "")
                        if ext:
                            new_dest = dest.with_suffix(ext)
                            self.claimed_names.discard(dest.name.lower())
                            self.claimed_names.add(new_dest.name.lower())
                            dest = new_dest
                    dest.write_bytes(data)
                    self.downloaded += 1
                except (urllib.error.URLError, OSError, ValueError) as e:
                    warn(f"download failed {url}: {e}")
                    self.download_failed.add(url)
                    self.cache[url] = None
                    return None
            rel = f"../assets/{dest.name}"
            self.cache[url] = rel
            return rel
        self.cache[url] = None
        return None


# --- export container --------------------------------------------------------

class RemExport:
    """Loaded .rem export with the same shape DBContext had in migrate_logseq.py."""

    def __init__(self, rem_path: Path, assets: AssetManager):
        self.rem_path = rem_path
        self.assets = assets
        self.idmap: dict[str, dict] = {}
        self.children: dict[str | None, list[str]] = defaultdict(list)
        self.doc_ids: set[str] = set()
        self.parent_doc_of: dict[str, str | None] = {}
        self.owner_page: dict[str, str] = {}
        self.doc_page_slug: dict[str, str] = {}
        self.card_dirs: dict[str, set[str]] = defaultdict(set)
        self.card_clozes: dict[str, set[str]] = defaultdict(set)
        # PDF rems: rid -> {"url": str, "name": str}; populated in load().
        self.pdf_info: dict[str, dict] = {}
        # Resolved PDF asset path per rid (filled later by main()).
        self.pdf_path: dict[str, str] = {}
        # Highlights grouped by owning PDF rem id. Each entry is the parsed
        # n.d JSON plus the highlight rem's _id and modified time.
        self.pdf_highlights: dict[str, list[dict]] = defaultdict(list)
        # Rems whose tree should not be emitted into normal page bodies
        # (PDF highlights and PDF page-number rems — surfaced via hls__ pages).
        self.skip_rids: set[str] = set()
        # PDF rid -> base filename stem used for hls__ page + assets dir.
        self.pdf_base: dict[str, str] = {}

    def load(self) -> None:
        with zipfile.ZipFile(self.rem_path) as z:
            with z.open("rem.json") as f:
                rem = json.load(f)
            cards = None
            try:
                with z.open("cards.json") as f:
                    cards = json.load(f)
            except KeyError:
                pass

        for d in rem.get("docs", []):
            rid = d.get("_id")
            if rid:
                self.idmap[rid] = d

        for rid, doc in self.idmap.items():
            self.children[doc.get("parent")].append(rid)
        for plist in self.children.values():
            plist.sort(key=lambda rid: fractional_index_key(self.idmap[rid].get("f")))

        self.doc_ids = {
            rid for rid, doc in self.idmap.items()
            if is_user_rem(doc) and is_document(doc) and has_visible_content(doc)
        }

        # Build PDF index. A "PDF rem" is the parent of a child with rcrp 'f.u'.
        for rid, doc in self.idmap.items():
            if doc.get("rcrp") != "f.u":
                continue
            parent = doc.get("parent")
            if not parent:
                continue
            url_v = doc.get("value")
            url = url_v[0] if isinstance(url_v, list) and url_v and isinstance(url_v[0], str) else None
            if not url:
                continue
            name = None
            for sib_id in self.children.get(parent, []):
                sib = self.idmap.get(sib_id)
                if sib and sib.get("rcrp") == "f.n":
                    nv = sib.get("value")
                    if isinstance(nv, list) and nv and isinstance(nv[0], str):
                        name = nv[0]
                        break
            if not name:
                # Fall back to PDF rem's title.
                name = rem_title(self.idmap[parent], self.idmap, fallback="document")
            self.pdf_info[parent] = {"url": url, "name": name}

        # Build highlight index. Highlights have tp containing PDF_HIGHLIGHT_TAG;
        # walk up parent chain to find the owning PDF rem (in pdf_info).
        for rid, doc in self.idmap.items():
            tp = doc.get("tp") or {}
            if not isinstance(tp, dict):
                continue
            is_hl = PDF_HIGHLIGHT_TAG in tp
            is_pp = PDF_PAGE_TAG in tp
            if not (is_hl or is_pp):
                continue
            self.skip_rids.add(rid)
            if not is_hl:
                continue
            # find n.d payload
            payload = None
            for cid in self.children.get(rid, []):
                c = self.idmap.get(cid)
                if not c or c.get("rcrp") != "n.d":
                    continue
                v = c.get("value")
                if isinstance(v, list) and v and isinstance(v[0], str):
                    try:
                        payload = json.loads(v[0])
                    except (json.JSONDecodeError, ValueError):
                        payload = None
                    break
            if not isinstance(payload, dict):
                continue
            cur = doc.get("parent")
            depth = 0
            owner = None
            while cur and depth < 20:
                if cur in self.pdf_info:
                    owner = cur
                    break
                cur = self.idmap.get(cur, {}).get("parent")
                depth += 1
            if owner is None:
                continue
            payload["_rid"] = rid
            payload["_m"] = doc.get("m") or 0
            self.pdf_highlights[owner].append(payload)

        if cards:
            for c in cards.get("docs", []):
                rid = c.get("rId")
                if not rid:
                    continue
                cv = c.get("c")
                if cv == "f" or cv == "b":
                    self.card_dirs[rid].add(cv)
                elif cv is None:
                    self.card_dirs[rid].add("f")
                else:
                    # cloze id
                    self.card_clozes[rid].add(str(cv))
                    self.card_dirs[rid]  # touch defaultdict

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

    def compute_pages(self) -> tuple[list[str], list[str]]:
        used: set[str] = set()
        sorted_docs = sorted(self.doc_ids, key=lambda r: len(self.doc_ancestors(r)))
        for rid in self.doc_ids:
            chain = self.doc_ancestors(rid)
            self.parent_doc_of[rid] = chain[-1] if chain else None
        for rid in sorted_docs:
            slug = slug_for_segment(rem_title(self.idmap[rid], self.idmap))
            slug = self._unique(slug, used, rid)
            self.doc_page_slug[rid] = slug
            used.add(slug.lower())

        orphan_top: list[str] = []
        for rid in self.children[None]:
            doc = self.idmap[rid]
            if is_user_rem(doc) and has_visible_content(doc) and rid not in self.doc_ids:
                orphan_top.append(rid)
        for rid in orphan_top:
            slug = slug_for_segment(rem_title(self.idmap[rid], self.idmap))
            slug = self._unique(slug, used, rid)
            self.doc_page_slug[rid] = slug
            used.add(slug.lower())

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
                continue
            self._assign_owner(cid, slug)


# --- inline rendering --------------------------------------------------------

def render_inline(items, ctx: RemExport) -> str:
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


def highlight_page_number(rid: str, ctx: RemExport) -> int | None:
    """For a PDF-Markierung rem, dig out the pageNumber from its n.d slot."""
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
        pn = data.get("position", {}).get("pageNumber") if isinstance(data, dict) else None
        if pn is None and isinstance(data, dict):
            pn = data.get("pageNumber")
        if isinstance(pn, (int, float)):
            return int(pn)
    return None


def is_highlight_rem(rid: str, ctx: RemExport) -> bool:
    doc = ctx.idmap.get(rid)
    if not doc:
        return False
    tp = doc.get("tp") or {}
    return isinstance(tp, dict) and PDF_HIGHLIGHT_TAG in tp


def block_tags_for(rid: str, ctx: RemExport) -> list[str]:
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
        if not title:
            continue
        if title.startswith("__") and title.endswith("__"):
            continue
        tags.append(f"#[[{title}]]")
    return tags


# --- emission ----------------------------------------------------------------

INDENT = "\t"

CARD_STATS = {
    "card_blocks": 0,
    "cloze_deletions": 0,
    "bidirectional": 0,
    "cloze_cards": 0,
}


def _card_direction_property(dirs: set[str]) -> str | None:
    has_f = "f" in dirs
    has_b = "b" in dirs
    if has_f and has_b:
        return "both"
    if has_b and not has_f:
        return "reverse"
    return None


def _render_key_with_clozes(items, ctx: RemExport, cloze_ids: set[str]) -> tuple[str, int]:
    base = render_inline(items, ctx).strip()
    if not cloze_ids:
        return base, 0
    parts: list[str] = []
    n = 0
    for i, _cid in enumerate(sorted(cloze_ids), start=1):
        parts.append(f"{{{{cloze region-{i}}}}}")
        n += 1
    cloze_block = " ".join(parts)
    if base:
        return f"{base}<br>{cloze_block}", n
    return cloze_block, n


def emit_block(rid: str, ctx: RemExport, depth: int, lines: list[str],
               current_page: str) -> None:
    doc = ctx.idmap.get(rid)
    if doc is None or not is_user_rem(doc):
        return
    if rid in ctx.skip_rids:
        return

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

    # PDF highlight: prepend page number.
    if is_highlight_rem(rid, ctx):
        pn = highlight_page_number(rid, ctx)
        if pn is not None:
            key_md = f"**S. {pn}:** {key_md}" if key_md else f"**S. {pn}**"

    tags = block_tags_for(rid, ctx)
    indent = INDENT * depth

    if is_card:
        question = key_md
        if not question and value_md:
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

    pdf_rel = ctx.pdf_path.get(rid)
    if pdf_rel:
        info = ctx.pdf_info.get(rid, {})
        name = info.get("name") or "document.pdf"
        lines.append(f"{indent}{INDENT}- [{md_text_escape(name)}]({pdf_rel})")
        base = ctx.pdf_base.get(rid)
        if base:
            lines.append(f"{indent}{INDENT}{INDENT}- [[hls__{base}]]")

    for cid in ctx.children.get(rid, []):
        emit_block(cid, ctx, depth + 1, lines, current_page)


def render_page(rid: str, ctx: RemExport) -> str:
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
        lines.append(f"- {value_md}")

    pdf_rel = ctx.pdf_path.get(rid)
    if pdf_rel:
        info = ctx.pdf_info.get(rid, {})
        name = info.get("name") or "document.pdf"
        lines.append(f"- [{md_text_escape(name)}]({pdf_rel})")
        base = ctx.pdf_base.get(rid)
        if base:
            lines.append(f"\t- [[hls__{base}]]")

    body_start = len(lines)
    for cid in ctx.children.get(rid, []):
        emit_block(cid, ctx, 0, lines, slug)

    if len(lines) == body_start and (not isinstance(value, list) or not value):
        lines.append("- ")

    return "\n".join(lines).rstrip() + "\n"


def collect_remote_urls(ctx: RemExport) -> set[str]:
    """Walk all rems and return the set of unique http(s) image URLs."""
    urls: set[str] = set()

    def walk(items):
        if not items:
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("i") == "i":
                u = it.get("url", "")
                if u.startswith(("http://", "https://")):
                    urls.add(u)
            inner = it.get("textOfDeletedRem")
            if inner:
                walk(inner)

    for doc in ctx.idmap.values():
        walk(doc.get("key"))
        walk(doc.get("value"))
    return urls


# --- PDF highlights (Logseq-native annotations) ------------------------------

def _edn_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _edn_num(x) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        # Logseq writes floats with a decimal point.
        return repr(x)
    return "0"


def _edn_rect(r: dict) -> str:
    keys = ("x1", "y1", "x2", "y2", "width", "height")
    parts = [f":{k} {_edn_num(r.get(k, 0))}" for k in keys]
    return "{" + ", ".join(parts) + "}"


def _edn_position(pos: dict) -> str:
    bounding = pos.get("boundingRect") or {}
    rects = pos.get("rects") or []
    page = int(pos.get("pageNumber") or bounding.get("pageNumber") or 1)
    rects_edn = "(" + " ".join(_edn_rect(r) for r in rects) + ")"
    return ("{:bounding " + _edn_rect(bounding)
            + ", :rects " + rects_edn
            + ", :page " + str(page) + "}")


def _edn_highlight(uuid_str: str, page: int, position_edn: str,
                   content_edn: str, color: str) -> str:
    return ("{:id #uuid " + _edn_str(uuid_str)
            + ", :page " + str(page)
            + ", :position " + position_edn
            + ", :content " + content_edn
            + ", :properties {:color " + _edn_str(color) + "}}")


def pdf_base_for(name: str) -> str:
    """Stem (without extension) used for hls__<base>.md and assets/<base>/."""
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name


def process_pdf_highlights(ctx: RemExport, assets_dir: Path,
                           pages_dir: Path) -> tuple[int, int]:
    """Emit Logseq-native annotations for every PDF that has highlights.

    Writes:
      - assets/<base>.edn         (sidecar consumed by Logseq's PDF viewer)
      - pages/hls__<base>.md      (one bullet per highlight, with id::)
      - assets/<base>/<page>_<uuid>_<stamp>.png  (area highlight screenshots)

    Highlight rems are added to ``ctx.skip_rids`` already (in load()), so
    they will not also appear in their PDF's normal doc page body.

    Returns (pdfs_with_hls, total_highlights_emitted).
    """
    pdfs_done = 0
    hls_done = 0
    # Collect all area-highlight image URLs first so we can give them a
    # progress bar of their own.
    area_jobs: list[tuple[str, Path]] = []  # (s3_url, dest)
    pdf_payloads: list[tuple[str, str, str, list]] = []  # (pdf_rid, base, pdf_rel, hls)

    for pdf_rid, hls in ctx.pdf_highlights.items():
        pdf_rel = ctx.pdf_path.get(pdf_rid)
        if not pdf_rel:
            continue
        pdf_filename = os.path.basename(pdf_rel)
        base = pdf_base_for(pdf_filename)
        ctx.pdf_base[pdf_rid] = base
        # Sort by page then by modified time for stable output.
        hls_sorted = sorted(
            hls,
            key=lambda h: (int((h.get("position") or {}).get("pageNumber")
                               or (h.get("position") or {}).get("boundingRect", {}).get("pageNumber")
                               or 0),
                           h.get("_m") or 0),
        )
        pdf_payloads.append((pdf_rid, base, pdf_rel, hls_sorted))
        for h in hls_sorted:
            content = h.get("content") or {}
            img_url = content.get("imageUrl") or ""
            if img_url.startswith("%LOCAL_FILE%"):
                hashname = img_url[len("%LOCAL_FILE%"):]
                pos = h.get("position") or {}
                page = int(pos.get("pageNumber")
                           or pos.get("boundingRect", {}).get("pageNumber")
                           or 1)
                uid = rem_uuid(h["_rid"])
                stamp = int(h.get("_m") or 0)
                pdf_subdir = assets_dir / base
                pdf_subdir.mkdir(parents=True, exist_ok=True)
                dest = pdf_subdir / f"{page}_{uid}_{stamp}.png"
                if not dest.exists():
                    s3 = f"https://remnote-user-data.s3.amazonaws.com/{hashname}"
                    area_jobs.append((s3, dest))

    # Download missing area-highlight PNGs.
    if area_jobs and ctx.assets.download:
        prog = Progress(len(area_jobs), "downloading hl images")
        for s3, dest in area_jobs:
            prog.tick(suffix=dest.name)
            try:
                req = urllib.request.Request(
                    s3,
                    headers={"User-Agent": "Mozilla/5.0 RemNoteMigrator"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                dest.write_bytes(data)
                ctx.assets.downloaded += 1
            except (urllib.error.URLError, OSError, ValueError) as e:
                warn(f"hl image download failed {dest.name}: {e}")
        prog.done()

    # Now emit .edn + hls__*.md per PDF.
    for pdf_rid, base, pdf_rel, hls_sorted in pdf_payloads:
        edn_entries: list[str] = []
        md_lines: list[str] = []
        # The PDF asset path is `../assets/<file>`. From `pages/hls__*.md`'s
        # POV, that resolves the same way as for any other page.
        pdf_filename = os.path.basename(pdf_rel)
        md_lines.append(f"file:: [{pdf_filename}]({pdf_rel})")
        md_lines.append(f"file-path:: {pdf_rel}")
        md_lines.append("")

        any_emitted = 0
        for h in hls_sorted:
            pos = h.get("position") or {}
            page = int(pos.get("pageNumber")
                       or pos.get("boundingRect", {}).get("pageNumber")
                       or 1)
            uid = rem_uuid(h["_rid"])
            stamp = int(h.get("_m") or 0)
            content = h.get("content") or {}
            img_url = content.get("imageUrl") or ""
            text = content.get("text") or ""
            color = "yellow"

            if img_url:
                # Area highlight.
                content_edn = ("{:text " + _edn_str("[:span]")
                               + ", :image " + str(stamp) + "}")
                # Logseq area highlights have empty :rects and use bounding only.
                position_edn = _edn_position({
                    "boundingRect": pos.get("boundingRect") or {},
                    "rects": [],
                    "pageNumber": page,
                })
                md_text = "[:span]"
                md_extra = ["hl-type:: area", f"hl-stamp:: {stamp}"]
            else:
                content_edn = "{:text " + _edn_str(text) + "}"
                position_edn = _edn_position(pos)
                md_text = text or "[empty]"
                md_extra = []

            edn_entries.append(_edn_highlight(uid, page, position_edn,
                                              content_edn, color))
            md_lines.append(f"- {md_text}")
            md_lines.append("  ls-type:: annotation")
            md_lines.append(f"  hl-page:: {page}")
            md_lines.append(f"  hl-color:: {color}")
            md_lines.append(f"  id:: {uid}")
            for extra in md_extra:
                md_lines.append(f"  {extra}")
            any_emitted += 1
            hls_done += 1

        edn = "{:highlights [" + " ".join(edn_entries) + "], :extra {}}"
        (assets_dir / f"{base}.edn").write_text(edn, encoding="utf-8")
        (pages_dir / f"hls__{base}.md").write_text(
            "\n".join(md_lines).rstrip() + "\n", encoding="utf-8")
        if any_emitted:
            pdfs_done += 1

    return pdfs_done, hls_done


# --- main --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("rem_file", type=Path, help="Path to RemNoteExport_*.rem")
    p.add_argument("--graph", type=Path,
                   default=Path.home() / "Documents/Logseq/Graphs/2LD-rem",
                   help="Output Logseq graph dir (default: ~/Documents/Logseq/Graphs/2LD-rem)")
    p.add_argument("--local-files", type=Path, default=None,
                   help="Directory holding %%LOCAL_FILE%% asset blobs (e.g. old RemNote files/ dir)")
    p.add_argument("--no-download", action="store_true",
                   help="Do not download remote http(s) assets; emit raw URLs instead")
    args = p.parse_args(argv)

    if not args.rem_file.exists():
        print(f"error: {args.rem_file} not found", file=sys.stderr)
        return 2

    sys.setrecursionlimit(200000)
    graph: Path = args.graph
    pages_dir = graph / "pages"
    assets_dir = graph / "assets"
    journals_dir = graph / "journals"
    logseq_dir = graph / "logseq"
    for d in (graph, pages_dir, assets_dir, journals_dir, logseq_dir):
        d.mkdir(parents=True, exist_ok=True)
    (logseq_dir / "config.edn").write_text(
        "{:meta/version 1\n :preferred-format :markdown}\n",
        encoding="utf-8",
    )

    assets = AssetManager(args.local_files, assets_dir, download=not args.no_download)
    ctx = RemExport(args.rem_file, assets)
    ctx.load()
    print(f"loaded {len(ctx.idmap)} rems, {len(ctx.doc_ids)} docs, "
          f"{len(ctx.card_dirs)} card rems ({len(ctx.card_clozes)} with cloze)",
          file=sys.stderr)

    sorted_docs, orphans = ctx.compute_pages()
    print(f"{len(sorted_docs)} doc pages, {len(orphans)} orphan pages",
          file=sys.stderr)

    # Resolve PDFs first (separate progress bar so timing is clearer).
    if ctx.pdf_info:
        pdf_will_download = 0
        pdf_local_hit = 0
        for v in ctx.pdf_info.values():
            u = v["url"]
            if u.startswith(("http://", "https://")):
                pdf_will_download += 1
            elif u.startswith("%LOCAL_FILE%"):
                hashname = u[len("%LOCAL_FILE%"):]
                if args.local_files and (args.local_files / hashname).exists():
                    pdf_local_hit += 1
                else:
                    pdf_will_download += 1
        print(f"PDFs total: {len(ctx.pdf_info)} "
              f"(to download: {pdf_will_download}, local hit: {pdf_local_hit})",
              file=sys.stderr)
        if not args.no_download and pdf_will_download:
            assets.dl_progress = Progress(pdf_will_download, "downloading PDFs")
        for rid, info in ctx.pdf_info.items():
            rel = assets.resolve_pdf(info["url"], info["name"])
            if rel:
                ctx.pdf_path[rid] = rel
        if assets.dl_progress:
            assets.dl_progress.done()
            assets.dl_progress = None

    # PDF highlights: download missing area-hl PNGs, emit .edn + hls__ pages.
    if ctx.pdf_highlights:
        n_pdfs_hls = sum(1 for r in ctx.pdf_highlights if r in ctx.pdf_path)
        n_hls = sum(len(v) for r, v in ctx.pdf_highlights.items() if r in ctx.pdf_path)
        print(f"PDF highlights: {n_hls} across {n_pdfs_hls} PDFs", file=sys.stderr)
        pdfs_done, hls_done = process_pdf_highlights(ctx, assets_dir, pages_dir)
        print(f"  emitted hls__ pages: {pdfs_done}, highlights: {hls_done}",
              file=sys.stderr)

    if not args.no_download:
        remote_urls = collect_remote_urls(ctx)
        print(f"remote image URLs to fetch: {len(remote_urls)}", file=sys.stderr)
        if remote_urls:
            assets.dl_progress = Progress(len(remote_urls), "downloading images")

    page_total = len(sorted_docs) + len(orphans)
    page_prog = Progress(page_total, "rendering")
    n_pages = 0
    for rid in sorted_docs + orphans:
        slug = ctx.doc_page_slug[rid]
        try:
            body = render_page(rid, ctx)
        except RecursionError:
            warn(f"recursion limit on rem {rid}; skipping")
            page_prog.tick()
            continue
        (pages_dir / f"{slug}.md").write_text(body, encoding="utf-8")
        n_pages += 1
        page_prog.tick(suffix=slug)
    page_prog.done()
    if assets.dl_progress:
        assets.dl_progress.done()

    print("\n=== Summary ===", file=sys.stderr)
    print(f"  pages written: {n_pages}", file=sys.stderr)
    print(f"  PDFs resolved: {len(ctx.pdf_path)}/{len(ctx.pdf_info)}", file=sys.stderr)
    print(f"  assets copied: {assets.copied}", file=sys.stderr)
    print(f"  assets downloaded: {assets.downloaded}", file=sys.stderr)
    print(f"  assets reused (already on disk): {assets.skipped_existing}", file=sys.stderr)
    print(f"  download failures: {len(assets.download_failed)}", file=sys.stderr)
    print(f"  card blocks: {CARD_STATS['card_blocks']}", file=sys.stderr)
    print(f"  cloze cards: {CARD_STATS['cloze_cards']}", file=sys.stderr)
    print(f"  cloze deletions: {CARD_STATS['cloze_deletions']}", file=sys.stderr)
    print(f"  bidirectional cards: {CARD_STATS['bidirectional']}", file=sys.stderr)
    print(f"  warnings: {len(WARNINGS)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
