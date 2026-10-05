from __future__ import annotations

import copy
import logging
from datetime import date
from pathlib import Path
from typing import Any

import intake_esgf
import pandas as pd
from intake_esgf import ESGFCatalog

from .config import WorkflowConfig
from .intake_setup import configure_intake_esgf

LOGGER = logging.getLogger(__name__)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_hashable(value: Any) -> Any:
    """Recursively convert container values to hashable equivalents."""
    if isinstance(value, list):
        return tuple(_to_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple((k, _to_hashable(v)) for k, v in sorted(value.items(), key=lambda item: str(item[0])))
    if isinstance(value, set):
        return tuple(sorted(_to_hashable(v) for v in value))
    return value


def _drop_duplicates_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate rows even when object columns contain unhashable values."""
    if df.empty:
        return df

    normalized = df.copy()
    for col in normalized.columns:
        if normalized[col].dtype == "object":
            normalized[col] = normalized[col].map(_to_hashable)

    keep_mask = ~normalized.duplicated(keep="first")
    return df.loc[keep_mask].reset_index(drop=True)


def _date_bounds(cfg: WorkflowConfig) -> tuple[str, str]:
    start_year = int(cfg.request["start_year"])
    end_year = cfg.request.get("end_year") or date.today().year
    return f"{start_year:04d}-01-01", f"{int(end_year):04d}-12-31"


def _set_project(cat: ESGFCatalog, project_name: str) -> None:
    """Set intake-esgf project when available; harmless if the API changes."""
    try:
        projects = intake_esgf.projects.projects
        if project_name.lower() in projects:
            cat.project = projects[project_name.lower()]
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Could not set intake-esgf project %s: %s", project_name, exc)


def _query_for(
    cfg: WorkflowConfig,
    project: str,
    variable: str,
    table_values: list[str],
    include_time_bounds: bool = True,
) -> dict[str, Any]:
    pcfg = cfg.project_configs[project]
    query = copy.deepcopy(pcfg.get("base_facets", {}))
    query[pcfg["variable_facet"]] = variable
    if table_values:
        query[pcfg["table_facet"]] = table_values

    if include_time_bounds:
        file_start, file_end = _date_bounds(cfg)
        # intake-esgf supports file_start/file_end in recent releases; if an index ignores these,
        # downstream time subsetting should still be done during analysis.
        query["file_start"] = file_start
        query["file_end"] = file_end
    return query


def _tables_for(cfg: WorkflowConfig, project: str, variable: str) -> tuple[list[str], bool]:
    """Return (table values, include time bounds) for a requested variable."""
    req = cfg.request
    if variable in req.get("area_variables", []):
        tables = cfg.project_configs[project].get("fixed_table_values", req.get("fixed_tables", []))
        return tables, False
    return req.get("variable_tables", []), True


def requested_groups(cfg: WorkflowConfig) -> list[tuple[str, str]]:
    """Return every (project, variable) pair in the request, in order."""
    req = cfg.request
    variables = req.get("variables", []) + req.get("area_variables", [])
    return [(project, variable) for project in req["projects"] for variable in variables]


def search_group(cfg: WorkflowConfig, project: str, variable: str) -> tuple[ESGFCatalog | None, pd.DataFrame]:
    """Search ESGF for one (project, variable) pair.

    Returns the live catalog, so callers can subset ``cat.df`` and download without searching
    again, and a copy of its dataframe annotated with the ``requested_*`` columns. The catalog
    is ``None`` when the search fails or finds nothing.
    """
    pcfg = cfg.project_configs[project]
    tables, include_time_bounds = _tables_for(cfg, project, variable)
    cat = ESGFCatalog()
    _set_project(cat, pcfg.get("intake_project", project.lower()))
    query = _query_for(cfg, project, variable, tables, include_time_bounds=include_time_bounds)
    LOGGER.info("Searching %s %s with %s", project, variable, query)
    try:
        cat.search(**query, quiet=True)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Search failed for %s %s: %s", project, variable, exc)
        return None, pd.DataFrame()

    df = getattr(cat, "df", None)
    if df is None or df.empty:
        return None, pd.DataFrame()
    df = df.copy()
    df["requested_project"] = project
    df["requested_variable"] = variable
    df["requested_tables"] = ",".join(tables)
    return cat, df


def _select_preferred_rows(df: pd.DataFrame, cfg: WorkflowConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        return df, df

    selected_parts: list[pd.DataFrame] = []
    rejected_parts: list[pd.DataFrame] = []

    req = cfg.request
    member_policy = req.get("member_policy", "first_member")
    grid_pref = _as_list(req.get("grid_label_preference"))

    for (project, variable), group in df.groupby(["requested_project", "requested_variable"], dropna=False):
        pcfg = cfg.project_configs[str(project)]
        source_col = pcfg.get("source_facet", "source_id")
        member_col = pcfg.get("member_facet", "member_id")
        grid_col = pcfg.get("grid_facet", "grid_label")
        table_col = pcfg.get("table_facet", "table_id")

        group = group.copy()
        group["_grid_rank"] = 999
        if grid_col in group.columns and grid_pref:
            rank = {g: i for i, g in enumerate(grid_pref)}
            group["_grid_rank"] = group[grid_col].map(rank).fillna(999).astype(int)

        table_pref = req.get("fixed_tables", []) if variable in req.get("area_variables", []) else req.get("variable_tables", [])
        group["_table_rank"] = 999
        if table_col in group.columns and table_pref:
            rank = {t: i for i, t in enumerate(table_pref)}
            group["_table_rank"] = group[table_col].map(rank).fillna(999).astype(int)

        group["_member_rank"] = 999
        preferred_members = req.get("preferred_members_cmip6" if project == "CMIP6" else "preferred_members_cmip5", [])
        if member_col in group.columns and preferred_members:
            rank = {m: i for i, m in enumerate(preferred_members)}
            group["_member_rank"] = group[member_col].map(rank).fillna(999).astype(int)

        sort_cols = ["_grid_rank", "_table_rank", "_member_rank"]
        if "version" in group.columns:
            sort_cols.append("version")
        group = group.sort_values(sort_cols, ascending=[True, True, True] + [False] * (len(sort_cols) - 3))

        if member_policy == "all_members" or source_col not in group.columns:
            chosen = group
        else:
            by_cols = [source_col]
            if variable in req.get("area_variables", []) and grid_col in group.columns:
                by_cols.append(grid_col)
            chosen = group.groupby(by_cols, dropna=False, as_index=False).head(1)

        rejected = group.drop(index=chosen.index)
        selected_parts.append(chosen)
        if not rejected.empty:
            rejected_parts.append(rejected)

    # Keep the input index so callers can subset the catalog the rows came from.
    selected = pd.concat(selected_parts) if selected_parts else pd.DataFrame()
    rejected = pd.concat(rejected_parts) if rejected_parts else pd.DataFrame()

    for frame in [selected, rejected]:
        for col in ["_grid_rank", "_table_rank", "_member_rank"]:
            if col in frame.columns:
                frame.drop(columns=col, inplace=True)
    return selected, rejected


def write_manifests(
    manifests_dir: Path, raw: pd.DataFrame, selected: pd.DataFrame, rejected: pd.DataFrame
) -> dict[str, Path]:
    """Write the raw, selected and rejected search manifests."""
    outputs = {
        "raw": manifests_dir / "cmip_intake_manifest_raw.csv",
        "selected": manifests_dir / "cmip_intake_manifest_selected.csv",
        "rejected": manifests_dir / "cmip_intake_manifest_rejected.csv",
    }
    raw.to_csv(outputs["raw"], index=False)
    selected.to_csv(outputs["selected"], index=False)
    rejected.to_csv(outputs["rejected"], index=False)
    return outputs


def build_manifest(cfg: WorkflowConfig) -> dict[str, Path]:
    """Search every requested (project, variable) and write the manifests, without downloading."""
    configure_intake_esgf(cfg)
    frames: list[pd.DataFrame] = []
    for project, variable in requested_groups(cfg):
        _, df = search_group(cfg, project, variable)
        if not df.empty:
            frames.append(df)
    raw = _drop_duplicates_safe(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    selected, rejected = _select_preferred_rows(raw, cfg)
    return write_manifests(cfg.paths["manifests"], raw, selected, rejected)
