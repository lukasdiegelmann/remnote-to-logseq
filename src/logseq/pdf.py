"""Emit Logseq-native PDF annotations from RemNote highlights."""
from __future__ import annotations
import os
import urllib.error
import urllib.request
from pathlib import Path

from .rem_export import RemExport, rem_uuid
from ..helpers import warn
from ..progress import Progress


# ---------------------------------------------------------------------------
# EDN helpers
# ---------------------------------------------------------------------------

def _edn_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _edn_num(x) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
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
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_pdf_highlights(ctx: RemExport, assets_dir: Path,
                           pages_dir: Path) -> tuple[int, int]:
    """Write .edn sidecar + hls__*.md pages for every PDF with highlights."""
    pdfs_done = 0
    hls_done = 0
    area_jobs: list[tuple[str, Path]] = []
    pdf_payloads: list[tuple[str, str, str, list]] = []

    for pdf_rid, hls in ctx.pdf_highlights.items():
        pdf_rel = ctx.pdf_path.get(pdf_rid)
        if not pdf_rel:
            continue
        pdf_filename = os.path.basename(pdf_rel)
        base = pdf_base_for(pdf_filename)
        ctx.pdf_base[pdf_rid] = base
        hls_sorted = sorted(
            hls,
            key=lambda h: (
                int((h.get("position") or {}).get("pageNumber")
                    or (h.get("position") or {}).get("boundingRect", {}).get("pageNumber")
                    or 0),
                h.get("_m") or 0,
            ),
        )
        pdf_payloads.append((pdf_rid, base, pdf_rel, hls_sorted))
        for h in hls_sorted:
            content = h.get("content") or {}
            img_url = content.get("imageUrl") or ""
            if img_url.startswith("%LOCAL_FILE%"):
                hashname = img_url[len("%LOCAL_FILE%"):]
                pos = h.get("position") or {}
                page = int(pos.get("pageNumber")
                           or pos.get("boundingRect", {}).get("pageNumber") or 1)
                uid = rem_uuid(h["_rid"])
                stamp = int(h.get("_m") or 0)
                pdf_subdir = assets_dir / base
                pdf_subdir.mkdir(parents=True, exist_ok=True)
                dest = pdf_subdir / f"{page}_{uid}_{stamp}.png"
                if not dest.exists():
                    s3 = f"https://remnote-user-data.s3.amazonaws.com/{hashname}"
                    area_jobs.append((s3, dest))

    if area_jobs and ctx.assets.download:
        prog = Progress(len(area_jobs), "downloading hl images")
        for s3, dest in area_jobs:
            prog.tick(suffix=dest.name)
            try:
                req = urllib.request.Request(
                    s3, headers={"User-Agent": "Mozilla/5.0 RemNoteMigrator"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                dest.write_bytes(data)
                ctx.assets.downloaded += 1
            except (urllib.error.URLError, OSError, ValueError) as e:
                warn(f"hl image download failed {dest.name}: {e}")
        prog.done()

    for pdf_rid, base, pdf_rel, hls_sorted in pdf_payloads:
        edn_entries: list[str] = []
        md_lines: list[str] = []
        pdf_filename = os.path.basename(pdf_rel)
        md_lines.append(f"file:: [{pdf_filename}]({pdf_rel})")
        md_lines.append(f"file-path:: {pdf_rel}")
        md_lines.append("")

        any_emitted = 0
        for h in hls_sorted:
            pos = h.get("position") or {}
            page = int(pos.get("pageNumber")
                       or pos.get("boundingRect", {}).get("pageNumber") or 1)
            uid = rem_uuid(h["_rid"])
            stamp = int(h.get("_m") or 0)
            content = h.get("content") or {}
            img_url = content.get("imageUrl") or ""
            text = content.get("text") or ""
            color = "yellow"

            if img_url:
                content_edn = ("{:text " + _edn_str("[:span]")
                               + ", :image " + str(stamp) + "}")
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


def collect_remote_urls(ctx: RemExport) -> set[str]:
    urls: set[str] = set()

    def walk(items) -> None:
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
