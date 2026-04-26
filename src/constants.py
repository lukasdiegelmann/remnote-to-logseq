from __future__ import annotations
import re

DOC_TAG = "Fv0CqTL0lG4TwiBFK"
PDF_HIGHLIGHT_TAG = "YzACaf58NQJFRasvl"
PDF_PAGE_TAG = "7HXKPpjC9xxe0S265"

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

# HTML heading tags used in Obsidian HTML output (h1 reserved for doc title)
HEADING_TAG = {0: "h2", 1: "h3", 2: "h4", 3: "h5", 4: "h6", 5: "h6", 6: "h6"}

INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
