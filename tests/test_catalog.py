from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from cmip_intake_esgf_fetch import catalog
from conftest import dataset_dir, dataset_row, write_lai

# Columns et_unc's CMIPESGFLoader and regrid_cmip_esgf.py read from the catalog CSV.
LOADER_COLUMNS = {"path", "experiment_id", "source_id", "member_id", "variable_id", "table_id"}


def test_esm_rows(tmp_path):
    roots = {"mirror": tmp_path / "mirror", "cache": tmp_path / "cache"}
    paths = [
        tmp_path / "cache" / "x" / "lai_Lmon_CESM2_historical_r1i1p1f1_gn_195001-201412.nc",
        tmp_path / "mirror" / "x" / "lai_Lmon_CESM2_historical_r1i1p1f1_gn_185001-194912.nc",
    ]
    rows = catalog.esm_rows(dataset_row(), paths, roots)
    assert [r["time_range"] for r in rows] == ["195001-201412", "185001-194912"]
    assert [r["location"] for r in rows] == ["cache", "mirror"]
    assert rows[0]["activity_id"] == "CMIP"
    assert rows[0]["version"] == "v20190308"
    assert set(catalog.ESM_COLUMNS) >= LOADER_COLUMNS
    assert set(rows[0]) == set(catalog.ESM_COLUMNS)


def test_esm_rows_unwraps_list_facets():
    row = dataset_row() | {"source_id": ["CESM2"]}
    (out,) = catalog.esm_rows(row, ["/elsewhere/lai_Lmon_CESM2_historical_r1i1p1f1_gn.nc"], {})
    assert out["source_id"] == "CESM2"
    assert out["location"] == "other"
    assert out["time_range"] == ""


def test_append_csv_writes_header_once(tmp_path):
    out = tmp_path / "cat.csv"
    row = {c: c for c in catalog.ESM_COLUMNS}
    catalog.append_csv([row], out)
    catalog.append_csv([row, row], out)
    df = pd.read_csv(out)
    assert list(df.columns) == catalog.ESM_COLUMNS
    assert len(df) == 3


def test_parse_drs_path_both_layouts():
    base = "/m/CMIP6/CMIP/NCAR/CESM2/historical/r1i1p1f1/Lmon/lai/gn/v20190308"
    expected = {"activity_id": "CMIP", "institution_id": "NCAR", "version": "v20190308"}
    assert catalog.parse_drs_path(Path(base, "lai_x.nc")) == expected
    assert catalog.parse_drs_path(Path(base, "lai", "lai_x.nc")) == expected
    assert catalog.parse_drs_path(Path("/tmp/lai_x.nc")) == {}


def test_find_netcdf_files_skips_partial_downloads(tmp_path):
    for name in ["lai_Lmon_CESM2_a.nc", "pr_Amon_CESM2_a.nc", "lai_Lmon_CESM2_b.ncX1y2"]:
        (tmp_path / name).write_text("")
    assert [p.name for p in catalog.find_netcdf_files(tmp_path)] == [
        "lai_Lmon_CESM2_a.nc",
        "pr_Amon_CESM2_a.nc",
    ]
    assert [p.name for p in catalog.find_netcdf_files(tmp_path, r"lai_.*", regex=True)] == [
        "lai_Lmon_CESM2_a.nc"
    ]


def test_esm_catalog_opens_with_intake_esm(tmp_path):
    """Two members, each split into two time files across mirror and cache, aggregate into one dataset."""
    import dask
    import intake

    roots = {"mirror": tmp_path / "mirror", "cache": tmp_path / "cache"}
    rows = []
    for member in ["r1i1p1f1", "r2i1p1f1"]:
        first = write_lai(
            dataset_dir(roots["mirror"], member) / "lai" / f"lai_Lmon_CESM2_historical_{member}_gn_200001-200012.nc",
            "2000-01-01",
        )
        second = write_lai(
            dataset_dir(roots["cache"], member) / f"lai_Lmon_CESM2_historical_{member}_gn_200101-200112.nc",
            "2001-01-01",
        )
        rows += catalog.esm_rows(dataset_row(member), [first, second], roots)
    csv_path = tmp_path / "out" / "cat.csv"
    catalog.append_csv(rows, csv_path)
    json_path = catalog.write_esm_descriptor(csv_path)
    assert json.loads(json_path.read_text())["catalog_file"] == "cat.csv"

    cat = intake.open_esm_datastore(str(json_path))
    # netCDF4/HDF5 builds without thread safety segfault when intake-esm opens files on threads.
    with dask.config.set(scheduler="synchronous"):
        dsets = cat.to_dataset_dict(progressbar=False)
    assert len(dsets) == 1
    (ds,) = dsets.values()
    assert ds.sizes["member_id"] == 2
    assert ds.sizes["time"] == 24
