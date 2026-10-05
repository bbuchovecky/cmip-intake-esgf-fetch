from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def expand_path(value: str | Path) -> Path:
    """Expand environment variables and ``~`` in a path."""
    return Path(os.path.expandvars(str(value))).expanduser()


@dataclass(frozen=True)
class WorkflowConfig:
    raw: dict[str, Any]

    @property
    def paths(self) -> dict[str, Path]:
        return {k: expand_path(v) for k, v in self.raw["paths"].items()}

    @property
    def request(self) -> dict[str, Any]:
        return self.raw["request"]

    @property
    def intake_esgf(self) -> dict[str, Any]:
        return self.raw.get("intake_esgf", {})

    @property
    def project_configs(self) -> dict[str, Any]:
        return self.raw["project_configs"]

    @property
    def dataroots(self) -> dict[str, Path]:
        """Named read-only roots searched before downloading, in priority order.

        ``intake_esgf.esg_dataroot`` may be a mapping of ``name: path`` or a plain list of paths
        (in which case each path is its own name).
        """
        roots = self.intake_esgf.get("esg_dataroot") or {}
        if isinstance(roots, dict):
            return {str(k): expand_path(v) for k, v in roots.items()}
        return {str(v): expand_path(v) for v in roots}

    @property
    def catalog_csv(self) -> Path:
        """Output intake-esm catalog CSV; the JSON descriptor is written next to it."""
        paths = self.paths
        return paths.get("catalog", paths["root"] / "catalogs" / "cmip_catalog.csv")


def load_config(path: str | Path) -> WorkflowConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = WorkflowConfig(raw=raw)
    for key in ["root", "local_cache", "manifests", "logs"]:
        cfg.paths[key].mkdir(parents=True, exist_ok=True)
    cfg.catalog_csv.parent.mkdir(parents=True, exist_ok=True)
    return cfg
