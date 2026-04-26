"""DBContext: loads a RemNote SQLite database into an in-memory rem graph."""
from __future__ import annotations
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from ..assets import AssetManager
from ..helpers import (fractional_index_key, has_visible_content, is_document,
                       is_user_rem, rem_title, slug_for_segment)


class DBContext:
    def __init__(self, db_path: Path, files_dir: Path, namespace_prefix: str,
                 assets: AssetManager):
        self.db_path = db_path
        self.files_dir = files_dir
        self.namespace_prefix = namespace_prefix
        self.assets = assets
        self.idmap: dict[str, dict] = {}
        self.children: dict[str | None, list[str]] = defaultdict(list)
        self.doc_ids: set[str] = set()
        self.parent_doc_of: dict[str, str | None] = {}
        self.owner_page: dict[str, str] = {}
        self.doc_page_slug: dict[str, str] = {}
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
            try:
                ccur = conn.execute(
                    "SELECT json_extract(doc,'$.rId'), json_extract(doc,'$.c')"
                    " FROM cards"
                )
                for rid, c in ccur:
                    if not rid:
                        continue
                    if c in ("f", "b"):
                        self.card_dirs[rid].add(c)
                    elif c is None:
                        self.card_dirs[rid].add("f")
                    else:
                        self.card_clozes[rid].add(str(c))
                        self.card_dirs[rid]  # touch defaultdict
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
        for rid, doc in self.idmap.items():
            self.children[doc.get("parent")].append(rid)
        for plist in self.children.values():
            plist.sort(
                key=lambda r: fractional_index_key(self.idmap[r].get("f")))
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
        sorted_docs = sorted(
            self.doc_ids, key=lambda r: len(self.doc_ancestors(r)))
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
            if (is_user_rem(doc) and has_visible_content(doc)
                    and rid not in self.doc_ids):
                orphan_top.append(rid)
        for rid in orphan_top:
            slug = slug_for_segment(rem_title(self.idmap[rid], self.idmap))
            slug = self._unique(slug, used_slugs, rid)
            self.doc_page_slug[rid] = slug
            used_slugs.add(slug.lower())

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
