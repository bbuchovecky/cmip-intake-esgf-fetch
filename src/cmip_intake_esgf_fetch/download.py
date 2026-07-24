from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from intake_esgf import ESGFCatalog

from .config import WorkflowConfig
from .intake_setup import configure_intake_esgf
from .search import _query_for, _set_project, subset_catalog_to_manifest

LOGGER = logging.getLogger(__name__)


def download_selected(cfg: WorkflowConfig) -> Path:
    """Download selected manifest rows using intake-esgf and record local paths."""
    configure_intake_esgf(cfg)
    manifest_path = cfg.paths["manifests"] / "cmip_intake_manifest_selected.csv"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Selected manifest not found: {manifest_path}")

    manifest = pd.read_csv(manifest_path)
    rows: list[dict[str, str]] = []

    for (project, variable), _ in manifest.groupby(["requested_project", "requested_variable"]):
        project = str(project)
        variable = str(variable)
        pcfg = cfg.project_configs[project]
        cat = ESGFCatalog()
        _set_project(cat, pcfg.get("intake_project", project.lower()))

        if variable in cfg.request.get("area_variables", []):
            tables = pcfg.get("fixed_table_values", cfg.request.get("fixed_tables", []))
            include_time_bounds = False
        else:
            tables = cfg.request.get("variable_tables", [])
            include_time_bounds = True
        query = _query_for(cfg, project, variable, tables, include_time_bounds=include_time_bounds)

        LOGGER.info("Re-searching before download: %s %s", project, variable)
        try:
            cat.search(**query, quiet=True)
        except TypeError:
            cat.search(**query)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Search failed before download for %s %s: %s", project, variable, exc)
            continue

        cat = subset_catalog_to_manifest(cat, manifest, project, variable)
        if getattr(cat, "df", pd.DataFrame()).empty:
            LOGGER.warning("No selected catalog rows for %s %s", project, variable)
            continue

        LOGGER.info("Downloading/accessing %d datasets for %s %s", len(cat.df), project, variable)
        try:
            path_dict = cat.to_path_dict(minimal_keys=False, quiet=True)
        except TypeError:
            path_dict = cat.to_path_dict(minimal_keys=False)

        for key, paths in path_dict.items():
            if isinstance(paths, (str, Path)):
                paths = [paths]
            for path in paths:
                rows.append(
                    {
                        "requested_project": project,
                        "requested_variable": variable,
                        "dataset_key": str(key),
                        "path": str(path),
                    }
                )

    out = cfg.paths["manifests"] / "cmip_intake_downloaded_paths.csv"
    pd.DataFrame(rows).drop_duplicates().to_csv(out, index=False)
    return out
