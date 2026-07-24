#!/usr/bin/env python3
"""Recursively catalog an intake-esgf cache directory.

This is a lightweight alternative to the package `inventory` command. It scans an
ESGF cache directory for files ending exactly in `.nc`, optionally opens each file
with xarray, extracts useful CMIP/grid/time metadata, and writes a CSV table.

Examples
--------
python catalog_esgf_cache.py \
  --cache-dir "${SCRATCH}/cmip_intake_esgf_fetch/esgf_cache" \
  --output "${SCRATCH}/cmip_intake_esgf_fetch/manifests/esgf_cache_catalog.csv"

# Faster metadata-only filename/path catalog, without opening NetCDF files:
python catalog_esgf_cache.py --cache-dir /path/to/esgf_cache --output catalog.csv --no-open

# Use multiple processes. This may help for many files but can stress shared filesystems:
python catalog_esgf_cache.py --cache-dir /path/to/esgf_cache --output catalog.csv --workers 4
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr



OUTPUT_COLUMNS = [
    "status",
    "error",
    "path",
    "relative_path",
    "filename",
    "size_bytes",
    "modified_utc",
    "mip_era",
    "project_id",
    "activity_id",
    "institution_id",
    "source_id",
    "model_id",
    "experiment_id",
    "member_id",
    "ensemble",
    "table_id",
    "frequency",
    "realm",
    "variable_id",
    "grid_label",
    "time_range",
    "version",
    "tracking_id",
    "nominal_resolution",
    "data_vars",
    "dims",
    "time_name",
    "n_time",
    "time_start",
    "time_stop",
    "lat_name",
    "lon_name",
    "y_name",
    "x_name",
    "n_lat",
    "n_lon",
    "approx_dlat",
    "approx_dlon",
    "has_bounds",
    "has_areacella",
    "has_sftlf",
]


CMIP6_FILENAME_RE = re.compile(
    r"^(?P<variable_id>[^_]+)_"
    r"(?P<table_id>[^_]+)_"
    r"(?P<source_id>[^_]+)_"
    r"(?P<experiment_id>[^_]+)_"
    r"(?P<member_id>r\d+i\d+p\d+f\d+)_"
    r"(?P<grid_label>[^_]+)"
    r"(?:_(?P<time_range>[^.]+))?\.nc$"
)

CMIP5_FILENAME_RE = re.compile(
    r"^(?P<variable_id>[^_]+)_"
    r"(?P<table_id>[^_]+)_"
    r"(?P<model_id>[^_]+)_"
    r"(?P<experiment_id>[^_]+)_"
    r"(?P<ensemble>r\d+i\d+p\d+)"
    r"(?:_(?P<time_range>[^.]+))?\.nc$"
)


def utc_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(v) for v in value)
    if isinstance(value, dict):
        return ";".join(f"{k}:{v}" for k, v in value.items())
    return str(value)


def safe_attr(attrs: dict[str, Any], *names: str) -> str:
    for name in names:
        value = attrs.get(name)
        if value not in (None, ""):
            return stringify(value)
    return ""


def parse_filename(path: Path) -> dict[str, str]:
    """Infer common CMIP fields from a CMIP-style filename."""
    name = path.name
    out: dict[str, str] = {}

    match = CMIP6_FILENAME_RE.match(name)
    if match:
        out.update({k: v for k, v in match.groupdict().items() if v})
        out["mip_era"] = "CMIP6"
        return out

    match = CMIP5_FILENAME_RE.match(name)
    if match:
        out.update({k: v for k, v in match.groupdict().items() if v})
        out["mip_era"] = "CMIP5"
        return out

    # Fallback: variable is usually the first token.
    parts = name.removesuffix(".nc").split("_")
    if parts:
        out["variable_id"] = parts[0]
    if len(parts) > 1:
        out["table_id"] = parts[1]
    return out


def find_coord_by_standard_name(ds: Any, standard_names: set[str]) -> str:
    for name in list(ds.coords) + list(ds.variables):
        attrs = getattr(ds[name], "attrs", {})
        if attrs.get("standard_name") in standard_names:
            return name
    return ""


def find_first_existing(ds: Any, names: list[str]) -> str:
    for name in names:
        if name in ds.coords or name in ds.variables or name in ds.dims:
            return name
    return ""


def coord_spacing(ds: Any, name: str) -> str:
    """Estimate median coordinate spacing for one-dimensional coordinates."""
    if not name or name not in ds.variables:
        return ""
    var = ds[name]
    if var.ndim != 1 or var.size < 2:
        return ""
    try:
        values = np.asarray(var.values, dtype="float64")
        values = values[np.isfinite(values)]
        if values.size < 2:
            return ""
        diffs = np.diff(np.sort(values))
        diffs = diffs[np.isfinite(diffs) & (np.abs(diffs) > 0)]
        if diffs.size == 0:
            return ""
        return f"{float(np.median(np.abs(diffs))):.6g}"
    except Exception:
        return ""


def dimension_size(ds: Any, name: str) -> str:
    if not name:
        return ""
    if name in ds.sizes:
        return str(ds.sizes[name])
    if name in ds.variables:
        shape = ds[name].shape
        if len(shape) == 1:
            return str(shape[0])
    return ""


def infer_xy_names(ds: Any, lat_name: str, lon_name: str) -> tuple[str, str]:
    """Infer y/x dimension names for regular or curvilinear grids."""
    y_name = ""
    x_name = ""

    if lat_name in ds.variables:
        lat_dims = ds[lat_name].dims
        if len(lat_dims) == 1:
            y_name = lat_dims[0]
        elif len(lat_dims) >= 2:
            y_name = lat_dims[-2]
            x_name = lat_dims[-1]

    if lon_name in ds.variables:
        lon_dims = ds[lon_name].dims
        if len(lon_dims) == 1:
            x_name = lon_dims[0]
        elif len(lon_dims) >= 2:
            y_name = y_name or lon_dims[-2]
            x_name = x_name or lon_dims[-1]

    y_name = y_name or find_first_existing(ds, ["lat", "latitude", "y", "j", "rlat"])
    x_name = x_name or find_first_existing(ds, ["lon", "longitude", "x", "i", "rlon"])
    return y_name, x_name


def has_bounds(ds: Any) -> str:
    for name in ds.variables:
        attrs = getattr(ds[name], "attrs", {})
        bounds_name = attrs.get("bounds")
        if bounds_name and bounds_name in ds.variables:
            return "true"
    for candidate in ["lat_bnds", "lon_bnds", "lat_bounds", "lon_bounds", "vertices_latitude", "vertices_longitude"]:
        if candidate in ds.variables:
            return "true"
    return "false"


def time_summary(ds: Any) -> tuple[str, str, str, str]:
    time_name = find_first_existing(ds, ["time"])
    if not time_name:
        time_name = find_coord_by_standard_name(ds, {"time"})
    if not time_name or time_name not in ds.variables:
        return "", "", "", ""

    n_time = str(ds.sizes.get(time_name, ds[time_name].size))
    try:
        values = ds[time_name].values
        if values.size == 0:
            return time_name, n_time, "", ""
        return time_name, n_time, stringify(values[0]), stringify(values[-1])
    except Exception:
        return time_name, n_time, "", ""


def open_dataset_metadata(path: Path) -> dict[str, str]:
    if xr is None:
        raise RuntimeError("xarray is required unless --no-open is used")

    # Try decoded times first; retry without decoding if time decoding fails.
    try:
        ds = xr.open_dataset(path, decode_times=True, chunks=None)
    except Exception:
        ds = xr.open_dataset(path, decode_times=False, chunks=None)

    try:
        attrs = dict(ds.attrs)
        out = {
            "mip_era": safe_attr(attrs, "mip_era", "project_id", "project"),
            "project_id": safe_attr(attrs, "project_id", "project"),
            "activity_id": safe_attr(attrs, "activity_id", "activity"),
            "institution_id": safe_attr(attrs, "institution_id", "institute_id", "institute"),
            "source_id": safe_attr(attrs, "source_id", "model_id", "model"),
            "model_id": safe_attr(attrs, "model_id", "source_id", "model"),
            "experiment_id": safe_attr(attrs, "experiment_id", "experiment"),
            "member_id": safe_attr(attrs, "variant_label", "member_id", "realization_index"),
            "ensemble": safe_attr(attrs, "ensemble", "variant_label", "parent_variant_label"),
            "table_id": safe_attr(attrs, "table_id", "mip_table", "cmor_table"),
            "frequency": safe_attr(attrs, "frequency"),
            "realm": safe_attr(attrs, "realm", "modeling_realm"),
            "grid_label": safe_attr(attrs, "grid_label"),
            "version": safe_attr(attrs, "version", "data_specs_version"),
            "tracking_id": safe_attr(attrs, "tracking_id"),
            "nominal_resolution": safe_attr(attrs, "nominal_resolution"),
            "data_vars": ";".join(ds.data_vars),
            "dims": ";".join(f"{k}:{v}" for k, v in ds.sizes.items()),
            "has_bounds": has_bounds(ds),
            "has_areacella": "true" if "areacella" in ds.variables else "false",
            "has_sftlf": "true" if "sftlf" in ds.variables else "false",
        }

        filename_info = parse_filename(path)
        out["variable_id"] = safe_attr(attrs, "variable_id") or filename_info.get("variable_id", "")

        time_name, n_time, time_start, time_stop = time_summary(ds)
        out.update(
            {
                "time_name": time_name,
                "n_time": n_time,
                "time_start": time_start,
                "time_stop": time_stop,
            }
        )

        lat_name = find_first_existing(ds, ["lat", "latitude"]) or find_coord_by_standard_name(
            ds, {"latitude", "grid_latitude"}
        )
        lon_name = find_first_existing(ds, ["lon", "longitude"]) or find_coord_by_standard_name(
            ds, {"longitude", "grid_longitude"}
        )
        y_name, x_name = infer_xy_names(ds, lat_name, lon_name)

        out.update(
            {
                "lat_name": lat_name,
                "lon_name": lon_name,
                "y_name": y_name,
                "x_name": x_name,
                "n_lat": dimension_size(ds, y_name),
                "n_lon": dimension_size(ds, x_name),
                "approx_dlat": coord_spacing(ds, lat_name),
                "approx_dlon": coord_spacing(ds, lon_name),
            }
        )
        return out
    finally:
        ds.close()


def catalog_one(args: tuple[str, str, bool]) -> dict[str, str]:
    path_str, cache_dir_str, open_files = args
    path = Path(path_str)
    cache_dir = Path(cache_dir_str)

    filename_info = parse_filename(path)
    row = {col: "" for col in OUTPUT_COLUMNS}
    row.update(filename_info)

    row.update(
        {
            "path": str(path),
            "relative_path": str(path.relative_to(cache_dir)) if path.is_relative_to(cache_dir) else str(path),
            "filename": path.name,
            "size_bytes": str(path.stat().st_size),
            "modified_utc": utc_mtime(path),
            "status": "ok",
        }
    )

    if open_files:
        try:
            metadata = open_dataset_metadata(path)
            # Preserve filename-derived fields when global attributes are absent.
            for key, value in metadata.items():
                if value or not row.get(key):
                    row[key] = value
            for key, value in filename_info.items():
                row[key] = row.get(key) or value
        except Exception as exc:
            row["status"] = "error"
            row["error"] = f"{type(exc).__name__}: {exc}"

    return row


def find_netcdf_files(cache_dir: Path) -> list[Path]:
    """Return files ending exactly in `.nc`, excluding temp files like `.ncabc123`."""
    return sorted(path for path in cache_dir.rglob("*.nc") if path.is_file() and path.name.endswith(".nc"))


def write_csv(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Catalog valid NetCDF files in an intake-esgf cache.")
    parser.add_argument(
        "--cache-dir",
        required=True,
        type=Path,
        help="Root intake-esgf cache directory to recursively scan.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output CSV path.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes. Use 1 for safest behavior on shared filesystems.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open NetCDF files; only catalog path, size, mtime, and filename-derived metadata.",
    )
    parser.add_argument(
        "--include-errors",
        action="store_true",
        help="Keep rows for files that cannot be opened. By default, they are kept; this option is retained for clarity.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cache_dir = Path(os.path.expandvars(str(args.cache_dir))).expanduser().resolve()
    output = Path(os.path.expandvars(str(args.output))).expanduser().resolve()

    if not cache_dir.exists():
        raise FileNotFoundError(f"Cache directory does not exist: {cache_dir}")
    if not cache_dir.is_dir():
        raise NotADirectoryError(f"Cache path is not a directory: {cache_dir}")

    files = find_netcdf_files(cache_dir)
    open_files = not args.no_open
    tasks = [(str(path), str(cache_dir), open_files) for path in files]

    if args.workers <= 1:
        rows = [catalog_one(task) for task in tasks]
    else:
        rows = []
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(catalog_one, task) for task in tasks]
            for future in as_completed(futures):
                rows.append(future.result())
        rows.sort(key=lambda row: row["path"])

    write_csv(rows, output)

    n_ok = sum(row["status"] == "ok" for row in rows)
    n_error = sum(row["status"] != "ok" for row in rows)
    print(f"Scanned cache: {cache_dir}")
    print(f"NetCDF files ending exactly in .nc: {len(files)}")
    print(f"Readable/cataloged successfully: {n_ok}")
    print(f"Errors: {n_error}")
    print(f"Wrote CSV: {output}")


if __name__ == "__main__":
    main()
