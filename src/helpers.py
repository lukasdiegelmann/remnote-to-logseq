from __future__ import annotations
import re
import sys
import unicodedata

from .constants import DOC_TAG, INVALID_FS_CHARS

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARN: {msg}", file=sys.stderr)


def reset_warnings() -> None:
    WARNINGS.clear()


def slug_for_segment(text: str, max_len: int = 110) -> str:
    """Filesystem-safe slug for a Logseq page name (handles namespace separator)."""
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


def slug_for_filename(text: str, max_len: int = 110) -> str:
    """Filesystem-safe slug for an Obsidian file name."""
    text = unicodedata.normalize("NFC", text)
    text = INVALID_FS_CHARS.sub(" ", text)
    text = text.replace("[", "(").replace("]", ")")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Unbenannt"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    return text


def fractional_index_key(f) -> tuple:
    return (1, "") if f is None else (0, f)


def md_text_escape(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\n", "<br>")


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
