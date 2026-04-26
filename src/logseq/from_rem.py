"""Migrate a RemNote .rem ZIP export to a Logseq graph."""
from __future__ import annotations
import sys
from pathlib import Path

from ..assets import AssetManager
from ..helpers import WARNINGS, reset_warnings, warn
from ..progress import Progress
from .pdf import collect_remote_urls, process_pdf_highlights
from .rem_export import RemExport
from .render import CARD_STATS, render_page, reset_card_stats


def run(
    rem_file: Path,
    graph: Path,
    local_files: Path | None = None,
    download: bool = True,
) -> int:
    reset_warnings()
    reset_card_stats()

    if not rem_file.exists():
        print(f"error: {rem_file} not found", file=sys.stderr)
        return 2

    sys.setrecursionlimit(200_000)

    pages_dir = graph / "pages"
    assets_dir = graph / "assets"
    journals_dir = graph / "journals"
    logseq_dir = graph / "logseq"
    for d in (graph, pages_dir, assets_dir, journals_dir, logseq_dir):
        d.mkdir(parents=True, exist_ok=True)
    (logseq_dir / "config.edn").write_text(
        "{:meta/version 1\n :preferred-format :markdown}\n", encoding="utf-8")

    assets = AssetManager(local_files, assets_dir, download=download)
    ctx = RemExport(rem_file, assets)
    ctx.load()
    print(
        f"loaded {len(ctx.idmap)} rems, {len(ctx.doc_ids)} docs, "
        f"{len(ctx.card_dirs)} card rems ({len(ctx.card_clozes)} with cloze)",
        file=sys.stderr,
    )

    sorted_docs, orphans = ctx.compute_pages()
    print(f"{len(sorted_docs)} doc pages, {len(orphans)} orphan pages",
          file=sys.stderr)

    # Resolve PDFs
    if ctx.pdf_info:
        pdf_will_download = sum(
            1 for v in ctx.pdf_info.values()
            if not (v["url"].startswith("%LOCAL_FILE%")
                    and local_files
                    and (local_files / v["url"][len("%LOCAL_FILE%"):]).exists())
        )
        print(
            f"PDFs total: {len(ctx.pdf_info)} (to fetch: {pdf_will_download})",
            file=sys.stderr,
        )
        if download and pdf_will_download:
            assets.dl_progress = Progress(pdf_will_download, "downloading PDFs")
        for rid, info in ctx.pdf_info.items():
            rel = assets.resolve_pdf(info["url"], info["name"])
            if rel:
                ctx.pdf_path[rid] = rel
        if assets.dl_progress:
            assets.dl_progress.done()
            assets.dl_progress = None

    # PDF highlights
    if ctx.pdf_highlights:
        n_hls = sum(len(v) for r, v in ctx.pdf_highlights.items()
                    if r in ctx.pdf_path)
        n_pdfs = sum(1 for r in ctx.pdf_highlights if r in ctx.pdf_path)
        print(f"PDF highlights: {n_hls} across {n_pdfs} PDFs", file=sys.stderr)
        pdfs_done, hls_done = process_pdf_highlights(ctx, assets_dir, pages_dir)
        print(f"  emitted hls__ pages: {pdfs_done}, highlights: {hls_done}",
              file=sys.stderr)

    # Remote images
    if download:
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
    print(f"  assets reused: {assets.skipped_existing}", file=sys.stderr)
    print(f"  download failures: {len(assets.download_failed)}", file=sys.stderr)
    print(f"  card blocks: {CARD_STATS['card_blocks']}", file=sys.stderr)
    print(f"  cloze cards: {CARD_STATS['cloze_cards']}", file=sys.stderr)
    print(f"  cloze deletions: {CARD_STATS['cloze_deletions']}", file=sys.stderr)
    print(f"  bidirectional cards: {CARD_STATS['bidirectional']}", file=sys.stderr)
    print(f"  warnings: {len(WARNINGS)}", file=sys.stderr)
    return 0
