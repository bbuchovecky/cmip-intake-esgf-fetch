from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .catalog import scan_directory
from .config import load_config
from .fetch import fetch
from .search import build_manifest


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # intake-esgf logs one line per file; keep those in its log file, not the terminal.
    logging.getLogger("intake-esgf").propagate = verbose


def partial_downloads(cache: Path) -> list[Path]:
    """Files left behind by interrupted downloads: ``*.nc<suffix>`` but not ``*.nc``."""
    return sorted(p for p in cache.rglob("*.nc*") if p.is_file() and not p.name.endswith(".nc"))


def _expand(path: Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser().resolve()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="cmip-intake-fetch",
        description=(
            "Find CMIP data with intake-esgf, reuse files already on glade, download the rest, "
            "and write an intake-esm catalog."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="Search, download what is missing, and write the catalog.")
    p.add_argument("--config", required=True, type=Path, help="Path to YAML configuration")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and report where each dataset would come from; download nothing.",
    )
    p.add_argument("--progress", action="store_true", help="Show intake-esgf download progress bars.")

    p = sub.add_parser("search", help="Search ESGF and write the manifests only.")
    p.add_argument("--config", required=True, type=Path, help="Path to YAML configuration")

    p = sub.add_parser("scan", help="Catalog the NetCDF files under any directory.")
    p.add_argument("--dir", required=True, type=Path, help="Directory to scan recursively.")
    p.add_argument("--output", required=True, type=Path, help="Output CSV path.")
    p.add_argument("--pattern", default="*.nc", help="Glob for files to include (default: *.nc).")
    p.add_argument(
        "--regex",
        action="store_true",
        help=r"Treat --pattern as a regex on the filename, e.g. '.*[AL]mon_CESM2_historical_.*\.nc'.",
    )
    p.add_argument("--no-open", action="store_true", help="Catalog paths and filename metadata only.")
    p.add_argument("--workers", type=int, default=1, help="Worker processes (1 is safest on glade).")
    p.add_argument("--esm-json", action="store_true", help="Also write an intake-esm JSON descriptor.")

    p = sub.add_parser("clean", help="List (or delete) partial downloads in the cache.")
    p.add_argument("--config", required=True, type=Path, help="Path to YAML configuration")
    p.add_argument("--delete", action="store_true", help="Delete the files instead of listing them.")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.command == "scan":
        scan_directory(
            _expand(args.dir),
            _expand(args.output),
            pattern=args.pattern,
            regex=args.regex,
            open_files=not args.no_open,
            workers=args.workers,
            esm_json=args.esm_json,
        )
        return

    cfg = load_config(args.config)

    if args.command == "fetch":
        fetch(cfg, dry_run=args.dry_run, progress=args.progress)
    elif args.command == "search":
        for key, path in build_manifest(cfg).items():
            print(f"{key}: {path}")
    elif args.command == "clean":
        files = partial_downloads(cfg.paths["local_cache"])
        for path in files:
            print(path)
            if args.delete:
                path.unlink()
        print(f"{'Deleted' if args.delete else 'Found'} {len(files)} partial download(s).")


if __name__ == "__main__":
    main()
