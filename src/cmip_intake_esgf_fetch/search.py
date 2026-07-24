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


def search_one(cfg: WorkflowConfig, project: str, variable: str, table_values: list[str]) -> pd.DataFrame:
    pcfg = cfg.project_configs[project]
    cat = ESGFCatalog()
    _set_project(cat, pcfg.get("intake_project", project.lower()))
    query = _query_for(cfg, project, variable, table_values)
    LOGGER.info("Searching %s %s with %s", project, variable, query)
    try:
        cat.search(**query, quiet=True)
    except TypeError:
        # Older/newer APIs may not accept quiet.
        cat.search(**query)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Search failed for %s %s: %s", project, variable, exc)
        return pd.DataFrame()

    df = getattr(cat, "df", pd.DataFrame()).copy()
    if df.empty:
        return df
    df["requested_project"] = project
    df["requested_variable"] = variable
    df["requested_tables"] = ",".join(table_values)
    return df


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

    selected = pd.concat(selected_parts, ignore_index=True) if selected_parts else pd.DataFrame()
    rejected = pd.concat(rejected_parts, ignore_index=True) if rejected_parts else pd.DataFrame()

    for frame in [selected, rejected]:
        for col in ["_grid_rank", "_table_rank", "_member_rank"]:
            if col in frame.columns:
                frame.drop(columns=col, inplace=True)
    return selected, rejected


def build_manifest(cfg: WorkflowConfig) -> dict[str, Path]:
    configure_intake_esgf(cfg)
    req = cfg.request
    manifests_dir = cfg.paths["manifests"]

    frames: list[pd.DataFrame] = []
    for project in req["projects"]:
        variables = req.get("variables", []) + req.get("area_variables", [])
        for variable in variables:
            if variable in req.get("area_variables", []):
                tables = cfg.project_configs[project].get("fixed_table_values", req.get("fixed_tables", []))
                include_time_bounds = False
            else:
                tables = req.get("variable_tables", [])
                include_time_bounds = True
            # Rebuild query here to allow fixed fields to skip time filters.
            pcfg = cfg.project_configs[project]
            cat = ESGFCatalog()
            _set_project(cat, pcfg.get("intake_project", project.lower()))
            query = _query_for(cfg, project, variable, tables, include_time_bounds=include_time_bounds)
            LOGGER.info("Searching %s %s with %s", project, variable, query)
            try:
                cat.search(**query, quiet=True)
            except TypeError:
                cat.search(**query)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Search failed for %s %s: %s", project, variable, exc)
                continue
            df = getattr(cat, "df", pd.DataFrame()).copy()
            if not df.empty:
                df["requested_project"] = project
                df["requested_variable"] = variable
                df["requested_tables"] = ",".join(tables)
                frames.append(df)

    raw = _drop_duplicates_safe(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    selected, rejected = _select_preferred_rows(raw, cfg)

    outputs = {
        "raw": manifests_dir / "cmip_intake_manifest_raw.csv",
        "selected": manifests_dir / "cmip_intake_manifest_selected.csv",
        "rejected": manifests_dir / "cmip_intake_manifest_rejected.csv",
    }
    raw.to_csv(outputs["raw"], index=False)
    selected.to_csv(outputs["selected"], index=False)
    rejected.to_csv(outputs["rejected"], index=False)
    return outputs


def subset_catalog_to_manifest(cat: ESGFCatalog, manifest: pd.DataFrame, project: str, variable: str) -> ESGFCatalog:
    """Subset an intake-esgf catalog to rows selected in the manifest."""
    out = copy.deepcopy(cat)
    df = getattr(cat, "df", pd.DataFrame()).copy()
    target = manifest[
        (manifest["requested_project"] == project) & (manifest["requested_variable"] == variable)
    ].copy()
    if df.empty or target.empty:
        out.df = pd.DataFrame()
        return out

    common_cols = [c for c in target.columns if c in df.columns and c not in {"size"}]
    id_cols = [c for c in ["id", "instance_id", "dataset_id"] if c in common_cols]
    if id_cols:
        mask = False
        for col in id_cols:
            mask = mask | df[col].astype(str).isin(set(target[col].dropna().astype(str)))
        out.df = df[mask].copy()
        return out

    key_cols = [
        c
        for c in ["mip_era", "activity_drs", "institution_id", "source_id", "experiment_id", "member_id", "table_id", "variable_id", "grid_label", "version"]
        if c in common_cols
    ]
    if not key_cols:
        out.df = df.iloc[0:0].copy()
        return out

    target_keys = set(target[key_cols].astype(str).agg("|".join, axis=1))
    df_keys = df[key_cols].astype(str).agg("|".join, axis=1)
    out.df = df[df_keys.isin(target_keys)].copy()
    return out
