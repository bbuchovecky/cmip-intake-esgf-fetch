"""Search ESGF, reuse files already on glade, download only what is missing, and catalog it all.

For each requested (project, variable):

1. Search ESGF once and select datasets (grid/table/member preferences from the config).
2. Probe the configured dataroots (glade CMIP mirror, CDG) and the scratch cache for each
   selected dataset at its exact version, and record the result in ``fetch_plan.csv``.
3. Unless ``dry_run``, ask intake-esgf for the file paths. With the layout resolver installed,
   files already on a dataroot or in the cache come back as local paths; the rest are
   downloaded into the cache.
4. Append one catalog row per file to ``<catalog>.partial``.

When every group is done, the partial file is moved to ``paths.catalog`` and an intake-esm JSON
descriptor is written next to it. Re-running is cheap: nothing already local is downloaded again.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from .catalog import ESM_COLUMNS, first_facet, append_csv, esm_rows, write_esm_descriptor
from .config import WorkflowConfig
from .intake_setup import configure_intake_esgf
from .local import locate_dataset
from .search import _drop_duplicates_safe, _select_preferred_rows, requested_groups, search_group, write_manifests

LOGGER = logging.getLogger(__name__)

PLAN_COLUMNS = [
    "requested_project",
    "requested_variable",
    "dataset",
    "location",
    "local_files",
    "local_bytes",
    "esgf_bytes",
]


def location_roots(cfg: WorkflowConfig) -> dict[str, Path]:
    """Named roots in the order intake-esgf checks them: dataroots first, then the cache."""
    return {**cfg.dataroots, "cache": cfg.paths["local_cache"]}


def _dataset_id(row: pd.Series) -> str:
    instance_id = first_facet(row, ["instance_id"])
    if instance_id:
        return instance_id
    facets = ["mip_era", "activity_drs", "institution_id", "source_id", "experiment_id", "member_id",
              "table_id", "variable_id", "grid_label", "version"]
    return ".".join(first_facet(row, [f]) for f in facets)


def plan_group(selected: pd.DataFrame, roots: dict[str, Path]) -> pd.DataFrame:
    """Classify each selected dataset by where its files will come from.

    ``location`` is the first root holding any file of the dataset's exact version, or
    ``download``. ``esgf_bytes`` is ESGF's size for the whole dataset (all years), while only
    files overlapping the configured years are used, so ``local_bytes < esgf_bytes`` is normal.
    Individual files missing from a root are still downloaded during the real run.
    """
    rows: list[dict[str, Any]] = []
    for _, row in selected.iterrows():
        hit = locate_dataset(row, roots)
        location, n_files, n_bytes = hit if hit is not None else ("download", 0, 0)
        rows.append(
            {
                "requested_project": row["requested_project"],
                "requested_variable": row["requested_variable"],
                "dataset": _dataset_id(row),
                "location": location,
                "local_files": n_files,
                "local_bytes": n_bytes,
                "esgf_bytes": pd.to_numeric(row.get("size"), errors="coerce"),
            }
        )
    return pd.DataFrame(rows, columns=PLAN_COLUMNS)


def summarize_plan(plan: pd.DataFrame) -> str:
    """One line per location with dataset counts and sizes in GB."""
    if plan.empty:
        return "  (no datasets selected)"
    summary = plan.groupby("location").agg(
        datasets=("dataset", "size"),
        local_gb=("local_bytes", "sum"),
        esgf_gb=("esgf_bytes", "sum"),
    )
    summary[["local_gb", "esgf_gb"]] = (summary[["local_gb", "esgf_gb"]] / 1e9).round(1)
    return summary.to_string()


def fetch(cfg: WorkflowConfig, *, dry_run: bool = False, progress: bool = False) -> Path:
    """Run the whole workflow; return the catalog CSV (or the plan CSV for a dry run)."""
    configure_intake_esgf(cfg)
    roots = location_roots(cfg)
    manifests_dir = cfg.paths["manifests"]
    plan_path = manifests_dir / "fetch_plan.csv"
    catalog_csv = cfg.catalog_csv
    partial = catalog_csv.with_name(catalog_csv.name + ".partial")
    if not dry_run:
        partial.unlink(missing_ok=True)

    raw_frames: list[pd.DataFrame] = []
    selected_frames: list[pd.DataFrame] = []
    rejected_frames: list[pd.DataFrame] = []
    plan_frames: list[pd.DataFrame] = []

    for project, variable in requested_groups(cfg):
        cat, df = search_group(cfg, project, variable)
        if cat is None:
            LOGGER.warning("No datasets found for %s %s", project, variable)
            continue
        selected, rejected = _select_preferred_rows(df, cfg)
        plan = plan_group(selected, roots)
        LOGGER.info("%s %s: %d datasets selected\n%s", project, variable, len(selected), summarize_plan(plan))

        raw_frames.append(df)
        selected_frames.append(selected)
        rejected_frames.append(rejected)
        plan_frames.append(plan)
        write_manifests(
            manifests_dir,
            _drop_duplicates_safe(pd.concat(raw_frames, ignore_index=True)),
            pd.concat(selected_frames, ignore_index=True),
            pd.concat(rejected_frames, ignore_index=True),
        )
        pd.concat(plan_frames, ignore_index=True).to_csv(plan_path, index=False)

        if dry_run or selected.empty:
            continue

        # Subset the live catalog to the selection rather than searching ESGF a second time.
        cat.df = cat.df.loc[selected.index].copy()
        path_dict = cat.to_path_dict(minimal_keys=False, quiet=not progress)

        rows: list[dict[str, str]] = []
        for _, row in cat.df.iterrows():
            paths = path_dict.get(row["key"], [])
            if not paths:
                LOGGER.warning("No local files for %s", row["key"])
                continue
            rows.extend(esm_rows(row, paths, roots))
        append_csv(rows, partial, ESM_COLUMNS)
        LOGGER.info("%s %s: cataloged %d files", project, variable, len(rows))

    plan = pd.concat(plan_frames, ignore_index=True) if plan_frames else pd.DataFrame(columns=PLAN_COLUMNS)
    print(f"Fetch plan ({plan_path}):\n{summarize_plan(plan)}")
    if dry_run:
        return plan_path

    if not partial.exists():
        raise RuntimeError("No files were cataloged; see the log for search or download errors.")
    partial.replace(catalog_csv)
    json_path = write_esm_descriptor(catalog_csv)
    catalog = pd.read_csv(catalog_csv)
    print(f"Catalog: {catalog_csv} ({len(catalog)} files)\n{catalog['location'].value_counts().to_string()}")
    print(f"intake-esm descriptor: {json_path}")
    return catalog_csv
