from __future__ import annotations

import copy
from pathlib import Path

import intake_esgf
import intake_esgf.base
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from cmip_intake_esgf_fetch.local import uninstall_layout_resolver

DRS = ("CMIP6", "CMIP", "NCAR", "CESM2", "historical")
TAIL = ("Lmon", "lai", "gn")


def dataset_dir(root: Path, member: str = "r1i1p1f1", version: str = "v20190308") -> Path:
    return root.joinpath(*DRS, member, *TAIL, version)


def esgf_relpath(member: str, version: str, time_range: str) -> Path:
    """Relative path intake-esgf builds for a file (ESGF publisher layout)."""
    name = f"lai_Lmon_CESM2_historical_{member}_gn_{time_range}.nc"
    return Path(*DRS, member, *TAIL, version, name)


def write_lai(path: Path, start: str, periods: int = 12) -> Path:
    """Write a tiny but valid monthly lai file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    time = pd.date_range(start, periods=periods, freq="MS")
    ds = xr.Dataset(
        {"lai": (("time", "lat", "lon"), np.ones((periods, 2, 3), dtype="float32"))},
        coords={"time": time, "lat": [-10.0, 10.0], "lon": [0.0, 120.0, 240.0]},
    )
    ds.to_netcdf(path)
    return path


def dataset_row(member: str = "r1i1p1f1", version: str = "20190308", size: float | None = None) -> dict:
    """A row shaped like intake-esgf's CMIP6 search dataframe."""
    return {
        "project": "CMIP6",
        "mip_era": "CMIP6",
        "activity_drs": "CMIP",
        "institution_id": "NCAR",
        "source_id": "CESM2",
        "experiment_id": "historical",
        "member_id": member,
        "table_id": "Lmon",
        "variable_id": "lai",
        "grid_label": "gn",
        "version": version,
        "instance_id": f"CMIP6.CMIP.NCAR.CESM2.historical.{member}.Lmon.lai.gn.v{version}",
        "size": size,
    }


@pytest.fixture(autouse=True)
def restore_intake_esgf():
    """Undo any intake-esgf configuration or patching a test performs."""
    saved = copy.deepcopy(dict(intake_esgf.conf))
    yield
    intake_esgf.conf.clear()
    intake_esgf.conf.update(saved)
    uninstall_layout_resolver()
