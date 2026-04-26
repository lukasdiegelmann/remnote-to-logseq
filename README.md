# remnote-to-logseq

<div align="center">

<img src="https://github.com/logseq.png" width="72" alt="Logseq">

Migrate your **RemNote** data to **Logseq** or **Obsidian** — stdlib only, no dependencies.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

</div>

---

## What it does

| Source | Target | Subcommand |
|--------|--------|------------|
| RemNote SQLite DB (`remnote.db`) | Logseq graph | `logseq` |
| RemNote `.rem` ZIP export | Logseq graph | `logseq-rem` |
| RemNote SQLite DB | Obsidian vault (MD + HTML) | `obsidian` |

**Preserved from RemNote:**
- Full document hierarchy as pages / namespaces
- Inline formatting (bold, italic, colours, headings, LaTeX)
- Wiki-style references → Logseq `[[Page]]` links or Obsidian `[[wikilinks]]`
- Flashcards (`#card`, cloze, bidirectional) with `card-last-direction`
- Local and remote image assets (copied / downloaded into `assets/`)
- PDFs with Logseq-native highlight annotations (`.edn` sidecar + `hls__*.md`)
- Block IDs (`id::`) so `((block-refs))` resolve

---

## Setup

```bash
git clone https://github.com/lukasdiegelmann/remnote-to-logseq.git
cd remnote-to-logseq
python main.py --help
```

Python 3.10+ required. No third-party packages.

---

## Usage

### Logseq — from SQLite

Reads one or two `remnote.db` files and writes a Logseq file-based graph.

```bash
python main.py logseq \
  --main-db   ~/remnote/remnote-<id>/remnote.db \
  --main-files ~/remnote/remnote-<id>/files \
  --graph     ~/Documents/Logseq/Graphs/MyGraph
```

A second database (e.g. a secondary knowledge base) can be merged in under a
`lnotes/` namespace:

```bash
python main.py logseq \
  --main-db      ~/remnote/main/remnote.db \
  --secondary-db ~/remnote/lnotes/remnote.db \
  --graph        ~/Documents/Logseq/Graphs/MyGraph
```

### Logseq — from `.rem` export

Reads a `RemNoteExport_*.rem` ZIP (the portable export from RemNote's UI).

```bash
python main.py logseq-rem RemNoteExport_2024-01-01.rem \
  --graph       ~/Documents/Logseq/Graphs/MyGraph \
  --local-files ~/remnote/remnote-<id>/files
```

Pass `--no-download` to skip fetching remote images and PDFs.

### Obsidian

Writes Obsidian-flavoured Markdown and/or standalone HTML files.

```bash
python main.py obsidian \
  --db    ~/remnote/remnote-<id>/remnote.db \
  --files ~/remnote/remnote-<id>/files \
  --vault ~/Documents/Obsidian/MyVault \
  --mode  both   # markdown | html | both
```

---

## Project structure

```
main.py              # CLI entrypoint
src/
├── constants.py     # Shared RemNote constants (DOC_TAG, COLOR_MAP, …)
├── helpers.py       # Shared rem helpers (render_plain, rem_title, …)
├── progress.py      # Terminal progress bar
├── assets.py        # Asset download / copy manager
├── logseq/
│   ├── context.py   # DBContext  — loads a SQLite database
│   ├── rem_export.py# RemExport  — loads a .rem ZIP export
│   ├── render.py    # Shared block emission (render_inline, emit_block, …)
│   ├── pdf.py       # Logseq-native PDF highlight annotations
│   ├── from_sqlite.py
│   └── from_rem.py
└── obsidian/
    ├── render.py    # Markdown + HTML document rendering
    ├── index.py     # Index.md / Index.html generation
    └── from_sqlite.py
```

---

## Notes

- `DOC_TAG` in `src/constants.py` is the RemNote "Dokument" power-up ID.
  If your account uses a different ID, update it there.
- The `--secondary-db` flag for the `logseq` subcommand expects a separate
  RemNote database; pages are prefixed with the `lnotes/` namespace to avoid
  slug collisions.
- PDF highlights require the `.rem` export path (`logseq-rem`); the SQLite
  export does not include highlight geometry.
