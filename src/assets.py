from __future__ import annotations
import mimetypes
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import INVALID_FS_CHARS
from .helpers import warn

if TYPE_CHECKING:
    from .progress import Progress


class AssetManager:
    def __init__(self, local_files: Path | None, assets_out: Path,
                 download: bool = True):
        self.local_files = local_files
        self.assets_out = assets_out
        self.download = download
        self.cache: dict[str, str | None] = {}
        self.copied = 0
        self.downloaded = 0
        self.skipped_existing = 0
        self.download_failed: set[str] = set()
        self.claimed_names: set[str] = set()
        self.dl_progress: "Progress | None" = None

    def _claim_name(self, name: str) -> Path:
        stem, ext = os.path.splitext(name)
        candidate = name
        n = 1
        while candidate.lower() in self.claimed_names:
            n += 1
            candidate = f"{stem}_{n}{ext}"
        self.claimed_names.add(candidate.lower())
        return self.assets_out / candidate

    def resolve_pdf(self, url: str, preferred_name: str) -> str | None:
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
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "Mozilla/5.0 RemNoteMigrator"},
                    )
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        data = resp.read()
                        ctype = resp.headers.get("Content-Type", "")
                    if dest.suffix == ".bin":
                        ext = mimetypes.guess_extension(
                            ctype.split(";")[0].strip() or "")
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

    def _download(self, url: str, dest: Path, *, log_label: str,
                  timeout: int = 20) -> bool:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 RemNoteMigrator"},
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
