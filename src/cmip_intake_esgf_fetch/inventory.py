from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr

from .config import WorkflowConfig

LOGGER = logging.getLogger(__name__)


def _safe_attr(ds: xr.Dataset, names: list[str]) -> str | None:
    for name in names:
        if name in ds.attrs and ds.attrs[name] not in [None, ""]:
            return str(ds.attrs[name])
    return None


def _coord_name(ds: xr.Dataset, candidates: list[str], standard_names: list[str]) -> str | None:
    for name in candidates:
        if name in ds.coords or name in ds.variables:
            return name
    for name, var in ds.variables.items():
        if str(var.attrs.get("standard_name", "")) in standard_names:
            return name
    return None


def _approx_resolution(ds: xr.Dataset) -> tuple[float | None, float | None]:
    lat_name = _coord_name(ds, ["lat", "latitude", "nav_lat"], ["latitude"])
    lon_name = _coord_name(ds, ["lon", "longitude", "nav_lon"], ["longitude"])

    def spacing(name: str | None) -> float | None:
        if name is None or name not in ds.variables:
            return None
        arr = np.asarray(ds[name].values)
        if arr.ndim == 0:
            return None
        vals = np.unique(arr[np.isfinite(arr)])
        if vals.size < 2:
            return None
        diffs = np.diff(np.sort(vals))
        diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
        if diffs.size == 0:
            return None
        return float(np.nanmedian(diffs))

    return spacing(lat_name), spacing(lon_name)


def _time_summary(ds: xr.Dataset) -> tuple[str | None, str | None, int | None]:
    if "time" not in ds.variables:
        return None, None, None
    try:
        t = xr.decode_cf(ds[["time"]])["time"]
        if t.size == 0:
            return None, None, 0
        return str(t.values[0]), str(t.values[-1]), int(t.size)
    except Exception:  # noqa: BLE001
        vals = ds["time"].values
        if len(vals) == 0:
            return None, None, 0
        return str(vals[0]), str(vals[-1]), int(len(vals))


def inspect_file(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    row: dict[str, Any] = {"path": str(path), "file_size_bytes": path.stat().st_size}
    try:
        with xr.open_dataset(path, decode_times=False, chunks={}) as ds:
            data_vars = list(ds.data_vars)
            row.update(
                {
                    "data_vars": ";".join(data_vars),
                    "dims": ";".join(f"{k}={v}" for k, v in ds.sizes.items()),
                    "source_id": _safe_attr(ds, ["source_id", "model_id", "model"]),
                    "experiment_id": _safe_attr(ds, ["experiment_id", "experiment"]),
                    "member_id": _safe_attr(ds, ["member_id", "variant_label", "realization"]),
                    "table_id": _safe_attr(ds, ["table_id", "cmor_table"]),
                    "grid_label": _safe_attr(ds, ["grid_label"]),
                    "mip_era": _safe_attr(ds, ["mip_era", "project_id"]),
                    "tracking_id": _safe_attr(ds, ["tracking_id"]),
                    "has_areacella": "areacella" in ds.variables,
                    "has_sftlf": "sftlf" in ds.variables,
                }
            )
            row["lat_name"] = _coord_name(ds, ["lat", "latitude", "nav_lat"], ["latitude"])
            row["lon_name"] = _coord_name(ds, ["lon", "longitude", "nav_lon"], ["longitude"])
            row["approx_dlat_deg"], row["approx_dlon_deg"] = _approx_resolution(ds)
            row["time_start"], row["time_end"], row["ntime"] = _time_summary(ds)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to inspect %s: %s", path, exc)
        row["error"] = str(exc)
    return row


def build_inventory(cfg: WorkflowConfig) -> Path:
    paths_manifest = cfg.paths["manifests"] / "cmip_intake_downloaded_paths.csv"
    if paths_manifest.exists():
        paths = pd.read_csv(paths_manifest)["path"].dropna().drop_duplicates().tolist()
    else:
        paths = [str(p) for p in cfg.paths["local_cache"].rglob("*.nc")]

    rows = [inspect_file(p) for p in paths if Path(p).exists()]
    out = cfg.paths["manifests"] / "cmip_intake_inventory.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    return out
