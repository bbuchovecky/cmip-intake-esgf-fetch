from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .catalog import catalog_from_config
from .config import load_config
from .download import download_selected
from .inventory import build_inventory
from .search import build_manifest


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="cmip-intake-fetch",
        description="Discover, download, inventory, and catalog CMIP data using intake-esgf.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ["build-manifest", "download", "inventory"]:
        p = sub.add_parser(name)
        p.add_argument("--config", required=True, type=Path, help="Path to YAML configuration")

    catalog_parser = sub.add_parser(
        "catalog",
        help="Recursively catalog the configured intake-esgf local cache.",
    )
    catalog_parser.add_argument("--config", required=True, type=Path, help="Path to YAML configuration")
    catalog_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open NetCDF files with xarray; catalog paths and filename metadata only.",
    )

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.command == "catalog":
        print(catalog_from_config(args.config, no_open=args.no_open))
        return

    cfg = load_config(args.config)

    if args.command == "build-manifest":
        outputs = build_manifest(cfg)
        for key, path in outputs.items():
            print(f"{key}: {path}")
    elif args.command == "download":
        print(download_selected(cfg))
    elif args.command == "inventory":
        print(build_inventory(cfg))
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
