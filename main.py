#!/usr/bin/env python3
"""remnote-to-logseq — migrate RemNote data to Logseq or Obsidian.

Subcommands
-----------
  logseq      SQLite DB(s) → Logseq graph
  logseq-rem  .rem ZIP export → Logseq graph
  obsidian    SQLite DB → Obsidian vault (Markdown + HTML)
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Default paths (adjust to your setup)
_HOME = Path.home()
_REMNOTE = _HOME / "remnote" / "remnote-666c2b227ea3bab0376d53ba"
_LNOTES  = _HOME / "remnote" / "lnotes"


def _cmd_logseq(args: argparse.Namespace) -> int:
    from src.logseq.from_sqlite import run
    return run(
        main_db=args.main_db,
        main_files=args.main_files,
        graph=args.graph,
        secondary_db=args.secondary_db,
        secondary_files=args.secondary_files,
    )


def _cmd_logseq_rem(args: argparse.Namespace) -> int:
    from src.logseq.from_rem import run
    return run(
        rem_file=args.rem_file,
        graph=args.graph,
        local_files=args.local_files,
        download=not args.no_download,
    )


def _cmd_obsidian(args: argparse.Namespace) -> int:
    from src.obsidian.from_sqlite import run
    do_md   = args.mode in ("markdown", "both")
    do_html = args.mode in ("html", "both")
    return run(
        db_path=args.db,
        files_dir=args.files,
        vault=args.vault,
        do_md=do_md,
        do_html=do_html,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<subcommand>")
    sub.required = True

    # ------------------------------------------------------------------ logseq
    p_ls = sub.add_parser(
        "logseq",
        help="SQLite DB(s) → Logseq graph",
        description="Migrate one or two RemNote SQLite databases to a Logseq graph.",
    )
    p_ls.add_argument(
        "--main-db", type=Path,
        default=_REMNOTE / "remnote.db",
        metavar="PATH",
        help="Primary RemNote SQLite DB (default: %(default)s)",
    )
    p_ls.add_argument(
        "--main-files", type=Path,
        default=_REMNOTE / "files",
        metavar="DIR",
        help="Asset files directory for the primary DB (default: %(default)s)",
    )
    p_ls.add_argument(
        "--secondary-db", type=Path,
        default=_LNOTES / "remnote.db",
        metavar="PATH",
        help="Optional second DB, namespaced under 'lnotes' (default: %(default)s)",
    )
    p_ls.add_argument(
        "--secondary-files", type=Path,
        default=_LNOTES / "files",
        metavar="DIR",
        help="Asset files directory for the secondary DB (default: %(default)s)",
    )
    p_ls.add_argument(
        "--graph", type=Path,
        default=_HOME / "Documents/Logseq/Graphs/2LD",
        metavar="DIR",
        help="Output Logseq graph directory (default: %(default)s)",
    )
    p_ls.set_defaults(func=_cmd_logseq)

    # -------------------------------------------------------------- logseq-rem
    p_rem = sub.add_parser(
        "logseq-rem",
        help=".rem ZIP export → Logseq graph",
        description="Migrate a RemNote .rem ZIP export to a Logseq graph.",
    )
    p_rem.add_argument(
        "rem_file", type=Path, metavar="REM_FILE",
        help="Path to RemNoteExport_*.rem",
    )
    p_rem.add_argument(
        "--graph", type=Path,
        default=_HOME / "Documents/Logseq/Graphs/2LD-rem",
        metavar="DIR",
        help="Output Logseq graph directory (default: %(default)s)",
    )
    p_rem.add_argument(
        "--local-files", type=Path, default=None, metavar="DIR",
        help="Directory holding %%LOCAL_FILE%% asset blobs",
    )
    p_rem.add_argument(
        "--no-download", action="store_true",
        help="Do not download remote http(s) assets",
    )
    p_rem.set_defaults(func=_cmd_logseq_rem)

    # --------------------------------------------------------------- obsidian
    p_obs = sub.add_parser(
        "obsidian",
        help="SQLite DB → Obsidian vault",
        description="Migrate a RemNote SQLite database to an Obsidian vault.",
    )
    p_obs.add_argument(
        "--db", type=Path,
        default=_REMNOTE / "remnote.db",
        metavar="PATH",
        help="RemNote SQLite DB (default: %(default)s)",
    )
    p_obs.add_argument(
        "--files", type=Path,
        default=_REMNOTE / "files",
        metavar="DIR",
        help="Asset files directory (default: %(default)s)",
    )
    p_obs.add_argument(
        "--vault", type=Path,
        default=_HOME / "Documents/Obsidian/lukasdiegelmann/lukasdiegelmann",
        metavar="DIR",
        help="Output Obsidian vault directory (default: %(default)s)",
    )
    p_obs.add_argument(
        "--mode", choices=("markdown", "html", "both"), default="both",
        help="Output format (default: both)",
    )
    p_obs.set_defaults(func=_cmd_obsidian)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
