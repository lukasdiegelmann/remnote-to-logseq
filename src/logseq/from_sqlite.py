"""Migrate one or two RemNote SQLite databases to a Logseq graph."""
from __future__ import annotations
import sys
from pathlib import Path

from ..assets import AssetManager
from ..helpers import WARNINGS, reset_warnings
from .context import DBContext
from .render import CARD_STATS, render_page, reset_card_stats


def run(
    main_db: Path,
    main_files: Path,
    graph: Path,
    secondary_db: Path | None = None,
    secondary_files: Path | None = None,
) -> int:
    reset_warnings()
    reset_card_stats()
    sys.setrecursionlimit(200_000)

    pages_dir = graph / "pages"
    assets_dir = graph / "assets"
    journals_dir = graph / "journals"
    logseq_dir = graph / "logseq"
    for d in (graph, pages_dir, assets_dir, journals_dir, logseq_dir):
        d.mkdir(parents=True, exist_ok=True)
    (logseq_dir / "config.edn").write_text(
        "{:meta/version 1\n :preferred-format :markdown}\n", encoding="utf-8")

    assets = AssetManager(main_files, assets_dir)
    used_slugs: set[str] = set()

    db_specs = [(main_db, main_files, "")]
    if secondary_db:
        db_specs.append((secondary_db, secondary_files or Path(), "lnotes"))

    contexts = []
    for db_path, files_dir, ns in db_specs:
        ctx = DBContext(db_path, files_dir, ns, assets)
        ctx.load()
        print(
            f"[{db_path.name}] loaded {len(ctx.idmap)} rems, "
            f"{len(ctx.doc_ids)} docs, {len(ctx.card_dirs)} card rems "
            f"({len(ctx.card_clozes)} with cloze)",
            file=sys.stderr,
        )
        sorted_docs, orphans = ctx.compute_pages(used_slugs)
        print(
            f"[{db_path.name}] {len(sorted_docs)} doc pages, "
            f"{len(orphans)} orphan pages",
            file=sys.stderr,
        )
        contexts.append((ctx, sorted_docs, orphans))

    counts: dict[str, int] = {}
    for ctx, sorted_docs, orphans in contexts:
        n = 0
        for rid in sorted_docs + orphans:
            slug = ctx.doc_page_slug[rid]
            try:
                body = render_page(rid, ctx)
            except RecursionError:
                from ..helpers import warn
                warn(f"recursion limit on rem {rid}; skipping")
                continue
            (pages_dir / f"{slug}.md").write_text(body, encoding="utf-8")
            n += 1
        label = ctx.db_path.name
        if ctx.namespace_prefix:
            label += f" ({ctx.namespace_prefix})"
        counts[label] = n

    print("\n=== Summary ===", file=sys.stderr)
    for k, v in counts.items():
        print(f"  pages [{k}]: {v}", file=sys.stderr)
    print(f"  assets copied: {assets.copied}", file=sys.stderr)
    print(f"  assets downloaded: {assets.downloaded}", file=sys.stderr)
    print(f"  download failures: {len(assets.download_failed)}", file=sys.stderr)
    print(f"  card blocks: {CARD_STATS['card_blocks']}", file=sys.stderr)
    print(f"  cloze cards: {CARD_STATS['cloze_cards']}", file=sys.stderr)
    print(f"  cloze deletions: {CARD_STATS['cloze_deletions']}", file=sys.stderr)
    print(f"  bidirectional cards: {CARD_STATS['bidirectional']}", file=sys.stderr)
    print(f"  warnings: {len(WARNINGS)}", file=sys.stderr)
    return 0
