"""RemExport: loads a RemNote .rem ZIP export into an in-memory rem graph."""
from __future__ import annotations
import json
import re
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path

from ..assets import AssetManager
from ..constants import DOC_TAG, PDF_HIGHLIGHT_TAG, PDF_PAGE_TAG
from ..helpers import (fractional_index_key, has_visible_content, is_document,
                       is_user_rem, rem_title, slug_for_segment)

HL_UUID_NS = uuid.UUID("d8d5a4ba-1c3e-4dc7-9a14-2d2b9a9b1e8f")


def rem_uuid(rid: str) -> str:
    return str(uuid.uuid5(HL_UUID_NS, rid))


class RemExport:
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
        self.pdf_info: dict[str, dict] = {}
        self.pdf_path: dict[str, str] = {}
        self.pdf_highlights: dict[str, list[dict]] = defaultdict(list)
        self.skip_rids: set[str] = set()
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
            plist.sort(
                key=lambda r: fractional_index_key(self.idmap[r].get("f")))

        self.doc_ids = {
            rid for rid, doc in self.idmap.items()
            if is_user_rem(doc) and is_document(doc) and has_visible_content(doc)
        }

        # Build PDF index
        for rid, doc in self.idmap.items():
            if doc.get("rcrp") != "f.u":
                continue
            parent = doc.get("parent")
            if not parent:
                continue
            url_v = doc.get("value")
            url = (url_v[0] if isinstance(url_v, list) and url_v
                   and isinstance(url_v[0], str) else None)
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
                name = rem_title(self.idmap[parent], self.idmap, fallback="document")
            self.pdf_info[parent] = {"url": url, "name": name}

        # Build highlight index
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
                if cv in ("f", "b"):
                    self.card_dirs[rid].add(cv)
                elif cv is None:
                    self.card_dirs[rid].add("f")
                else:
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
        sorted_docs = sorted(
            self.doc_ids, key=lambda r: len(self.doc_ancestors(r)))
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
            if (is_user_rem(doc) and has_visible_content(doc)
                    and rid not in self.doc_ids):
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
