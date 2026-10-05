"""End-to-end ``fetch`` with ESGF replaced by a fake catalog (no network)."""

from __future__ import annotations

from pathlib import Path

import intake_esgf.base
import pandas as pd
import pytest
import yaml

from cmip_intake_esgf_fetch import fetch as fetch_mod
from cmip_intake_esgf_fetch.config import load_config
from conftest import dataset_row, esgf_relpath


class FakeCatalog:
    """Mimics the parts of ESGFCatalog that fetch uses.

    ``to_path_dict`` resolves each file through intake-esgf's (possibly patched)
    ``get_local_file`` and "downloads" anything missing into the cache.
    """

    def __init__(self, df: pd.DataFrame, files: dict[str, list[Path]], dataroots, cache: Path):
        self.df = df
        self.files = files
        self.dataroots = dataroots
        self.cache = cache
        self.downloaded: list[Path] = []

    def to_path_dict(self, minimal_keys: bool = True, quiet: bool = False):
        assert minimal_keys is False
        self.df["key"] = self.df["instance_id"].str.rsplit(".", n=1).str[0]
        out = {}
        for _, row in self.df.iterrows():
            paths = []
            for rel in self.files[row["member_id"]]:
                try:
                    paths.append(intake_esgf.base.get_local_file(rel, self.dataroots + [self.cache]))
                except FileNotFoundError:
                    target = self.cache / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"downloaded")
                    self.downloaded.append(target)
                    paths.append(target)
            out[row["key"]] = paths
        return out


def write_config(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    cfg = {
        "paths": {
            "root": str(root),
            "local_cache": str(root / "cache"),
            "manifests": str(root / "manifests"),
            "logs": str(root / "logs"),
            "catalog": str(root / "catalogs" / "test.csv"),
        },
        "intake_esgf": {"esg_dataroot": {"mirror": str(tmp_path / "mirror")}},
        "request": {
            "projects": ["CMIP6"],
            "start_year": 1950,
            "variables": ["lai"],
            "variable_tables": ["Lmon"],
            "grid_label_preference": ["gn"],
            "member_policy": "all_members",
        },
        "project_configs": {
            "CMIP6": {
                "intake_project": "cmip6",
                "base_facets": {"experiment_id": "historical"},
                "variable_facet": "variable_id",
                "table_facet": "table_id",
                "source_facet": "source_id",
                "member_facet": "member_id",
                "grid_facet": "grid_label",
            }
        },
    }
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    """r1 is fully on the mirror (synda layout), r2 half on the mirror, r3 not at all."""
    cfg = load_config(write_config(tmp_path))
    mirror = tmp_path / "mirror"
    files = {
        m: [esgf_relpath(m, "v20190308", t) for t in ["185001-194912", "195001-201412"]]
        for m in ["r1i1p1f1", "r2i1p1f1", "r3i1p1f1"]
    }
    for rel in files["r1i1p1f1"] + files["r2i1p1f1"][:1]:
        target = mirror / rel.parent / "lai" / rel.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"mirror")
    df = pd.DataFrame([dataset_row(m, size=12) for m in files])
    fake = FakeCatalog(df, files, [mirror], cfg.paths["local_cache"])

    def fake_search_group(cfg_, project, variable):
        out = df.copy()
        out["requested_project"] = project
        out["requested_variable"] = variable
        out["requested_tables"] = "Lmon"
        return fake, out

    monkeypatch.setattr(fetch_mod, "search_group", fake_search_group)
    return cfg, fake, mirror


def test_dry_run_reports_without_downloading(scenario):
    cfg, fake, _ = scenario
    plan_path = fetch_mod.fetch(cfg, dry_run=True)
    plan = pd.read_csv(plan_path).set_index("dataset")["location"]
    assert sorted(plan) == ["download", "mirror", "mirror"]
    assert fake.downloaded == []
    assert not cfg.catalog_csv.exists()


def test_fetch_downloads_only_missing_files(scenario):
    cfg, fake, mirror = scenario
    out = fetch_mod.fetch(cfg)
    assert out == cfg.catalog_csv
    assert out.with_suffix(".json").exists()
    assert not out.with_name(out.name + ".partial").exists()

    # 1 missing r2 file + 2 r3 files downloaded; the 3 mirror files reused.
    assert len(fake.downloaded) == 3
    cat = pd.read_csv(out)
    assert len(cat) == 6
    assert cat.groupby("member_id")["location"].apply(sorted).to_dict() == {
        "r1i1p1f1": ["mirror", "mirror"],
        "r2i1p1f1": ["cache", "mirror"],
        "r3i1p1f1": ["cache", "cache"],
    }
    assert cat.loc[cat["location"] == "mirror", "path"].str.startswith(str(mirror)).all()
    assert (cat["version"] == "v20190308").all()

    # Second run: everything is local now.
    fake.downloaded.clear()
    fetch_mod.fetch(cfg)
    assert fake.downloaded == []
    assert pd.read_csv(out).equals(cat)
