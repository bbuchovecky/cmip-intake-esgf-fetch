"""Write intake-esm catalogs of CMIP files.

Two producers share this module:

- ``fetch`` builds rows from intake-esgf search results plus the local path of each file
  (``esm_rows``), appends them with ``append_csv`` and describes them with
  ``write_esm_descriptor``. No files are opened.
- ``scan`` walks an arbitrary directory and optionally opens every file to record grid and
  time metadata (``scan_directory``).
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable, Mapping
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

# ---------------------------------------------------------------------------------------------
# intake-esm catalog written by `fetch`
# ---------------------------------------------------------------------------------------------

ESM_COLUMNS = [
    "activity_id",
    "institution_id",
    "source_id",
    "experiment_id",
    "member_id",
    "table_id",
    "variable_id",
    "grid_label",
    "version",
    "time_range",
    "location",
    "path",
]

# Catalog column -> intake-esgf dataframe columns that may hold it (CMIP6 names first).
FACET_ALIASES: dict[str, tuple[str, ...]] = {
    "activity_id": ("activity_drs", "activity_id"),
    "institution_id": ("institution_id", "institute"),
    "source_id": ("source_id", "model"),
    "experiment_id": ("experiment_id", "experiment"),
    "member_id": ("member_id", "ensemble"),
    "table_id": ("table_id", "cmor_table"),
    "variable_id": ("variable_id", "variable"),
    "grid_label": ("grid_label",),
    "version": ("version",),
}

ESM_GROUPBY = ["activity_id", "institution_id", "source_id", "experiment_id", "table_id", "grid_label"]


def first_facet(row: Mapping[str, Any], names: Iterable[str]) -> str:
    for name in names:
        value = row.get(name)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is not None and str(value) not in ("", "nan"):
            return str(value)
    return ""


def location_of(path: Path, roots: Mapping[str, Path]) -> str:
    """Return the name of the first root containing ``path``, or ``"other"``."""
    for name, root in roots.items():
        if Path(path).is_relative_to(Path(root)):
            return name
    return "other"


def esm_rows(
    dataset_row: Mapping[str, Any],
    paths: Iterable[str | Path],
    roots: Mapping[str, Path],
) -> list[dict[str, str]]:
    """Build catalog rows for one dataset from its intake-esgf dataframe row and file paths.

    ``roots`` maps a location label (e.g. ``mirror``, ``cdg``, ``cache``) to its directory and
    fills the ``location`` column.
    """
    facets = {col: first_facet(dataset_row, names) for col, names in FACET_ALIASES.items()}
    if facets["version"]:
        facets["version"] = "v" + facets["version"].removeprefix("v")
    rows = []
    for path in sorted(Path(p) for p in paths):
        row = dict(facets)
        row["time_range"] = parse_filename(path).get("time_range", "")
        row["location"] = location_of(path, roots)
        row["path"] = str(path)
        rows.append(row)
    return rows


def append_csv(rows: list[dict[str, str]], output: Path, columns: list[str] = ESM_COLUMNS) -> None:
    """Append rows to ``output``, writing the header if the file is new or empty."""
    output.parent.mkdir(parents=True, exist_ok=True)
    new = not output.exists() or output.stat().st_size == 0
    with output.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerows(rows)


def write_esm_descriptor(csv_path: Path, json_path: Path | None = None, description: str = "") -> Path:
    """Write an esmcat 0.1.0 JSON descriptor so ``intake.open_esm_datastore(json_path)`` works.

    The layout follows NCAR's ``glade-cmip6.json``: one dataset per (activity, institution,
    source, experiment, table, grid), files joined along ``time`` by ``time_range`` and members
    stacked along a new ``member_id`` dimension. ``catalog_file`` is stored relative to the JSON
    file, so keep the two files together.
    """
    csv_path = Path(csv_path)
    json_path = Path(json_path) if json_path is not None else csv_path.with_suffix(".json")
    descriptor = {
        "esmcat_version": "0.1.0",
        "id": csv_path.stem,
        "description": description or f"CMIP files cataloged by cmip-intake-esgf-fetch ({csv_path.name})",
        "catalog_file": csv_path.name if json_path.parent == csv_path.parent else str(csv_path),
        "attributes": [{"column_name": c, "vocabulary": ""} for c in FACET_ALIASES],
        "assets": {"column_name": "path", "format": "netcdf"},
        "aggregation_control": {
            "variable_column_name": "variable_id",
            "groupby_attrs": ESM_GROUPBY,
            "aggregations": [
                {"type": "union", "attribute_name": "variable_id"},
                {
                    "type": "join_existing",
                    "attribute_name": "time_range",
                    "options": {"dim": "time", "coords": "minimal", "compat": "override"},
                },
                {
                    "type": "join_new",
                    "attribute_name": "member_id",
                    "options": {"coords": "minimal", "compat": "override"},
                },
            ],
        },
    }
    json_path.write_text(json.dumps(descriptor, indent=2) + "\n")
    return json_path


# ---------------------------------------------------------------------------------------------
# Directory scan (formerly scripts/catalog_esgf_cache.py)
# ---------------------------------------------------------------------------------------------

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
    """Return a file modification time as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def stringify(value: Any) -> str:
    """Convert common metadata objects to compact strings for CSV output."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(v) for v in value)
    if isinstance(value, dict):
        return ";".join(f"{k}:{v}" for k, v in value.items())
    return str(value)


def safe_attr(attrs: dict[str, Any], *names: str) -> str:
    """Return the first non-empty attribute value from a list of candidate names."""
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

    parts = name.removesuffix(".nc").split("_")
    if parts:
        out["variable_id"] = parts[0]
    if len(parts) > 1:
        out["table_id"] = parts[1]
    return out


def parse_drs_path(path: Path) -> dict[str, str]:
    """Infer activity, institution and version from a CMIP6 DRS directory path.

    Handles both ``.../<grid>/vYYYYMMDD/<file>`` and ``.../<grid>/vYYYYMMDD/<variable>/<file>``.
    """
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if part != "CMIP6" or len(parts) < i + 11:
            continue
        # activity/institution/source/experiment/member/table/variable/grid, then version
        drs = parts[i + 1 : i + 9]
        version = parts[i + 9]
        if re.fullmatch(r"v\d{8}", version):
            return {"activity_id": drs[0], "institution_id": drs[1], "version": version}
    return {}


def find_coord_by_standard_name(ds: Any, standard_names: set[str]) -> str:
    """Find the first coordinate or variable with a matching CF standard_name."""
    for name in list(ds.coords) + list(ds.variables):
        attrs = getattr(ds[name], "attrs", {})
        if attrs.get("standard_name") in standard_names:
            return name
    return ""


def find_first_existing(ds: Any, names: list[str]) -> str:
    """Return the first name present as a coordinate, variable, or dimension."""
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
    """Return the size of a dimension or one-dimensional variable."""
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
    """Return true if the dataset appears to contain coordinate bounds variables."""
    for name in ds.variables:
        attrs = getattr(ds[name], "attrs", {})
        bounds_name = attrs.get("bounds")
        if bounds_name and bounds_name in ds.variables:
            return "true"
    candidates = [
        "lat_bnds",
        "lon_bnds",
        "lat_bounds",
        "lon_bounds",
        "vertices_latitude",
        "vertices_longitude",
    ]
    for candidate in candidates:
        if candidate in ds.variables:
            return "true"
    return "false"


def time_summary(ds: Any) -> tuple[str, str, str, str]:
    """Return time coordinate name, count, start, and stop values."""
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
    """Open a NetCDF file with xarray and extract CMIP/grid/time metadata."""
    if xr is None:
        raise RuntimeError("xarray is required unless --no-open is used")

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
    """Catalog one NetCDF file. Designed to be multiprocessing-safe."""
    path_str, cache_dir_str, open_files = args
    path = Path(path_str)
    cache_dir = Path(cache_dir_str)

    filename_info = parse_filename(path)
    drs_info = parse_drs_path(path)
    filename_info.update(drs_info)
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
            for key, value in metadata.items():
                if value or not row.get(key):
                    row[key] = value
            for key, value in filename_info.items():
                row[key] = row.get(key) or value
            # The DRS directory version beats the `version` attribute (often data_specs_version).
            row.update(drs_info)
        except Exception as exc:
            row["status"] = "error"
            row["error"] = f"{type(exc).__name__}: {exc}"

    return row


def find_netcdf_files(cache_dir: Path, pattern: str = "*.nc", regex: bool = False) -> list[Path]:
    """Return files matching ``pattern`` that end exactly in .nc (skipping temp files like .ncabc123).

    ``pattern`` is a glob, or a regular expression matched against the filename if ``regex``.
    """
    if regex:
        repatt = re.compile(pattern)
        candidates = (p for p in cache_dir.rglob("*.nc") if repatt.match(p.name))
    else:
        candidates = cache_dir.rglob(pattern)
    return sorted(p for p in candidates if p.is_file() and p.name.endswith(".nc"))


def write_csv(rows: list[dict[str, str]], output: Path) -> None:
    """Write catalog rows to CSV."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def scan_directory(
    cache_dir: Path,
    output: Path,
    *,
    pattern: str = "*.nc",
    regex: bool = False,
    open_files: bool = True,
    workers: int = 1,
    esm_json: bool = False,
) -> Path:
    """Recursively catalog the NetCDF files under a directory.

    Parameters
    ----------
    cache_dir
        Directory to recursively scan (e.g. an intake-esgf cache).
    output
        Output CSV path.
    pattern, regex
        Glob (or, with ``regex``, a regular expression on the filename) selecting files.
    open_files
        If true, open each NetCDF file with xarray and extract metadata.
        If false, only path, size, modification time, and filename/DRS metadata are cataloged.
    workers
        Number of worker processes. Use one worker for safest behavior on shared filesystems.
    esm_json
        Also write an intake-esm JSON descriptor next to ``output``.

    Returns
    -------
    pathlib.Path
        Path to the written CSV catalog.
    """
    cache_dir = Path(cache_dir).expanduser().resolve()
    output = Path(output).expanduser().resolve()

    if not cache_dir.exists():
        raise FileNotFoundError(f"Cache directory does not exist: {cache_dir}")
    if not cache_dir.is_dir():
        raise NotADirectoryError(f"Cache path is not a directory: {cache_dir}")

    files = find_netcdf_files(cache_dir, pattern, regex)
    tasks = [(str(path), str(cache_dir), open_files) for path in files]

    if workers <= 1:
        rows = [catalog_one(task) for task in tasks]
    else:
        rows = []
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(catalog_one, task) for task in tasks]
            for future in as_completed(futures):
                rows.append(future.result())
        rows.sort(key=lambda row: row["path"])

    write_csv(rows, output)
    if esm_json:
        print(f"Wrote JSON: {write_esm_descriptor(output)}")

    n_ok = sum(row["status"] == "ok" for row in rows)
    n_error = sum(row["status"] != "ok" for row in rows)
    print(f"Scanned cache: {cache_dir}")
    print(f"NetCDF files matching {pattern!r}: {len(files)}")
    print(f"Readable/cataloged successfully: {n_ok}")
    print(f"Errors: {n_error}")
    print(f"Wrote CSV: {output}")

    return output
