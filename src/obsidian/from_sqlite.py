"""Migrate a RemNote SQLite database to an Obsidian vault (Markdown + HTML)."""
from __future__ import annotations
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from ..helpers import (fractional_index_key, has_visible_content, is_document,
                       is_user_rem, rem_title, slug_for_filename)
from .index import write_index
from .render import (render_document_html, render_document_md)

ORPHAN_DIR_NAME = "Lose Notizen"


def run(
    db_path: Path,
    files_dir: Path,
    vault: Path,
    do_md: bool = True,
    do_html: bool = True,
) -> int:
    sys.setrecursionlimit(50_000)

    conn = sqlite3.connect(str(db_path))
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
        plist.sort(key=lambda r: fractional_index_key(idmap[r].get("f")))

    doc_ids: set[str] = {
        rid for rid, doc in idmap.items()
        if is_user_rem(doc) and is_document(doc) and has_visible_content(doc)
    }
    print(f"Dokument-tagged user rems: {len(doc_ids)}", file=sys.stderr)

    orphan_top: list[str] = [
        rid for rid in children[None]
        if is_user_rem(idmap[rid]) and has_visible_content(idmap[rid])
        and rid not in doc_ids
    ]
    print(f"Orphan top-level rems: {len(orphan_top)}", file=sys.stderr)

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
        path = vault
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
            new_base = f"{base} ({n + 1})"
            if has_subdocs.get(rid):
                folder = parent_folder / new_base
                path = folder / f"{new_base}.md"
            else:
                path = parent_folder / f"{new_base}.md"
        return path

    sorted_docs = sorted(doc_ids, key=lambda r: len(doc_ancestors(r)))
    for rid in sorted_docs:
        rem_path_md[rid] = assign_path(rid)

    orphan_dir = vault / ORPHAN_DIR_NAME
    for rid in orphan_top:
        title = rem_title(idmap[rid], idmap)
        base = slug_for_filename(title)
        n = used_in_dir[orphan_dir].get(base.lower(), 0)
        used_in_dir[orphan_dir][base.lower()] = n + 1
        name = base if n == 0 else f"{base} ({n + 1})"
        rem_path_md[rid] = orphan_dir / f"{name}.md"

    def vault_relative(p: Path) -> str:
        return str(p.relative_to(vault)).replace("\\", "/")

    owner_file: dict[str, str] = {}

    def assign_owner(rid: str, file_rel: str) -> None:
        owner_file[rid] = file_rel
        for cid in children.get(rid, []):
            cdoc = idmap[cid]
            if not is_user_rem(cdoc) or cid in doc_ids:
                continue
            assign_owner(cid, file_rel)

    for rid, p in rem_path_md.items():
        assign_owner(rid, vault_relative(p))

    html_paths_by_owner: dict[str, Path] = {
        vault_relative(p): p.with_suffix(".html")
        for p in rem_path_md.values()
    }

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
                image_cache_md, files_dir,
            )
            path_md.write_text(
                "\n".join(body_lines).rstrip() + "\n", encoding="utf-8")
            md_written += 1
        if do_html:
            html_text = render_document_html(
                rid, idmap, children, doc_ids, owner_file, html_paths_by_owner,
                image_cache_html, parent_doc_of, rem_path_md, vault, files_dir,
            )
            path_md.with_suffix(".html").write_text(
                html_text, encoding="utf-8")
            html_written += 1

    if do_md:
        print(f"Wrote {md_written} markdown files", file=sys.stderr)
    if do_html:
        print(f"Wrote {html_written} html files", file=sys.stderr)

    write_index(doc_ids, orphan_top, idmap, rem_path_md, parent_doc_of,
                vault, do_md, do_html)
    return 0
