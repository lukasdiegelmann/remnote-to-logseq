"""Generate Index.md / Index.html for the Obsidian vault."""
from __future__ import annotations
import html as html_mod
import os
from pathlib import Path

from ..helpers import rem_title
from .render import html_page, _doc_link_md, _html_link_href


def write_index(doc_ids, orphan_top, idmap, rem_path, parent_doc_of,
                vault: Path, do_md: bool, do_html: bool) -> None:
    roots = sorted(
        (rid for rid in doc_ids if parent_doc_of.get(rid) is None),
        key=lambda r: rem_title(idmap[r], idmap).lower(),
    )
    orphans_sorted = sorted(
        orphan_top, key=lambda r: rem_title(idmap[r], idmap).lower())

    if do_md:
        body = [
            "# Index", "",
            "Auto-generiert aus der RemNote-Migration.", "",
            f"## Dokumente ({len(roots)})", "",
            "Die Hauptdokumente, jeweils mit verschachtelter Ordnerstruktur "
            "entsprechend der RemNote-Hierarchie.", "",
        ]
        for rid in roots:
            body.append(_doc_link_md(rid, idmap, rem_path))
        body += [
            "", f"## Lose Notizen ({len(orphan_top)})", "",
            "Top-Level-Rems aus RemNote, die nicht als Dokument markiert "
            "waren – meist atomare Konzepte, die aus anderen Notizen heraus "
            "referenziert werden.", "",
        ]
        for rid in orphans_sorted:
            body.append(f"- [[{rem_path[rid].stem}]]")
        body.append("")
        (vault / "Index.md").write_text("\n".join(body), encoding="utf-8")

    if do_html:
        sections = [
            '<h1 class="doc-title">Index</h1>',
            '<p class="doc-descriptor">Auto-generiert aus der RemNote-Migration.</p>',
            f"<h2>Dokumente ({len(roots)})</h2>",
            "<p>Die Hauptdokumente, jeweils mit verschachtelter Ordnerstruktur "
            "entsprechend der RemNote-Hierarchie.</p>",
            "<ul>",
        ]
        for rid in roots:
            title = rem_title(idmap[rid], idmap)
            href = os.path.relpath(rem_path[rid].with_suffix(".html"),
                                   start=vault)
            sections.append(
                f'<li><a class="q-link" href="{html_mod.escape(href)}">'
                f'{html_mod.escape(title)}</a></li>')
        sections += [
            "</ul>",
            f"<h2>Lose Notizen ({len(orphan_top)})</h2>",
            "<p>Top-Level-Rems aus RemNote, die nicht als Dokument markiert "
            "waren – meist atomare Konzepte.</p>",
            "<ul>",
        ]
        for rid in orphans_sorted:
            title = rem_title(idmap[rid], idmap)
            href = os.path.relpath(rem_path[rid].with_suffix(".html"),
                                   start=vault)
            sections.append(
                f'<li><a class="q-link" href="{html_mod.escape(href)}">'
                f'{html_mod.escape(title)}</a></li>')
        sections.append("</ul>")
        (vault / "Index.html").write_text(
            html_page("Index", "\n".join(sections)), encoding="utf-8")
