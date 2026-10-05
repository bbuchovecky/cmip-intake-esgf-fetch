from __future__ import annotations

import inspect
from pathlib import Path

import intake_esgf
import intake_esgf.base
import pytest
from intake_esgf.database import create_download_database

from cmip_intake_esgf_fetch import local
from conftest import dataset_dir, dataset_row, esgf_relpath


def touch(path: Path, nbytes: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * nbytes)
    return path


REL = esgf_relpath("r1i1p1f1", "v20190308", "185001-201412")


def test_standard_layout(tmp_path):
    expected = touch(tmp_path / REL)
    assert local.get_local_file(REL, [tmp_path]) == expected


def test_synda_layout(tmp_path):
    expected = touch(tmp_path / REL.parent / "lai" / REL.name)
    assert local.get_local_file(REL, [tmp_path]) == expected


def test_both_layouts_prefers_standard(tmp_path):
    expected = touch(tmp_path / REL)
    touch(tmp_path / REL.parent / "lai" / REL.name)
    assert local.get_local_file(REL, [tmp_path]) == expected


def test_older_version_is_not_a_match(tmp_path):
    touch(dataset_dir(tmp_path, version="v20190101") / "lai" / REL.name)
    with pytest.raises(FileNotFoundError):
        local.get_local_file(REL, [tmp_path])


def test_roots_checked_in_order(tmp_path):
    mirror, cache = tmp_path / "mirror", tmp_path / "cache"
    touch(cache / REL)
    expected = touch(mirror / REL.parent / "lai" / REL.name)
    assert local.get_local_file(REL, [str(mirror), cache]) == expected


def test_patch_matches_intake_esgf_signature():
    """The patch relies on a private intake-esgf function; fail loudly if it changes."""
    original = local._ORIGINAL_GET_LOCAL_FILE
    assert original.__module__ == "intake_esgf.base"
    assert list(inspect.signature(original).parameters) == ["path", "dataroots"]
    assert list(inspect.signature(local.get_local_file).parameters) == ["path", "dataroots"]


def test_install_layout_resolver_reaches_partition_infos(tmp_path):
    """intake-esgf treats synda-layout mirror files as local instead of downloading them."""
    mirror, cache = tmp_path / "mirror", tmp_path / "cache"
    cache.mkdir()
    expected = touch(mirror / REL.parent / "lai" / REL.name)
    intake_esgf.conf.set(esg_dataroot=[str(mirror)], local_cache=[str(cache)])
    intake_esgf.conf["download_db"] = str(tmp_path / "download.db")
    create_download_database(tmp_path / "download.db")
    info = {"key": "k", "path": REL, "HTTPServer": ["https://example.org/x.nc"]}

    infos, _ = intake_esgf.base.partition_infos([dict(info)], False, False)
    assert len(infos["https"]) == 1  # unpatched: would be downloaded

    local.install_layout_resolver()
    infos, ds = intake_esgf.base.partition_infos([dict(info)], False, False)
    assert infos["https"] == []
    assert ds == {"k": [expected]}


def test_locate_dataset_counts_each_file_once(tmp_path):
    vdir = dataset_dir(tmp_path / "mirror")
    for name in ["lai_a_185001-194912.nc", "lai_a_195001-201412.nc"]:
        touch(vdir / name, 100)
        touch(vdir / "lai" / name, 100)
    roots = {"mirror": tmp_path / "mirror", "cache": tmp_path / "cache"}
    assert local.locate_dataset(dataset_row(), roots) == ("mirror", 2, 200)


def test_locate_dataset_falls_through_roots(tmp_path):
    touch(dataset_dir(tmp_path / "cache") / "lai_x.nc", 5)
    touch(dataset_dir(tmp_path / "mirror", version="v20180101") / "lai_x.nc", 5)
    roots = {"mirror": tmp_path / "mirror", "cache": tmp_path / "cache"}
    assert local.locate_dataset(dataset_row(), roots) == ("cache", 1, 5)
    assert local.locate_dataset(dataset_row(version="20200101"), roots) is None


def test_locate_dataset_skips_non_cmip6(tmp_path):
    row = dataset_row() | {"mip_era": "CMIP5", "project": "CMIP5"}
    assert local.locate_dataset(row, {"mirror": tmp_path}) is None
