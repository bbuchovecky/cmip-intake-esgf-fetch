from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class WorkflowConfig:
    raw: dict[str, Any]

    @property
    def paths(self) -> dict[str, Path]:
        return {k: Path(os.path.expandvars(str(v))).expanduser() for k, v in self.raw["paths"].items()}

    @property
    def request(self) -> dict[str, Any]:
        return self.raw["request"]

    @property
    def intake_esgf(self) -> dict[str, Any]:
        return self.raw.get("intake_esgf", {})

    @property
    def project_configs(self) -> dict[str, Any]:
        return self.raw["project_configs"]


def load_config(path: str | Path) -> WorkflowConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = WorkflowConfig(raw=raw)
    for key in ["root", "local_cache", "manifests", "logs"]:
        cfg.paths[key].mkdir(parents=True, exist_ok=True)
    return cfg
