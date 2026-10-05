"""Find ESGF files that already exist on local filesystems (e.g. the NCAR glade CMIP mirror).

intake-esgf checks ``<esg_dataroot>/<relative ESGF path>`` before downloading a file, where the
relative path looks like::

    CMIP6/CMIP/NCAR/CESM2/historical/r1i1p1f1/Lmon/lai/gn/v20190308/lai_Lmon_..._185001-201412.nc

Most of ``/glade/campaign/collections/cmip.mirror`` was populated with synda, which stores files
one directory deeper, under the variable name::

    CMIP6/CMIP/.../gn/v20190308/lai/lai_Lmon_..._185001-201412.nc

so intake-esgf misses them and downloads a second copy. ``install_layout_resolver`` patches
intake-esgf's lookup to also try the synda layout. The patch is applied per file, so a
partially mirrored dataset only downloads its missing files.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import intake_esgf.base

from .catalog import first_facet

_ORIGINAL_GET_LOCAL_FILE = intake_esgf.base.get_local_file


def candidate_paths(path: Path, root: Path) -> list[Path]:
    """Return the locations under ``root`` where the ESGF file ``path`` may live.

    The first candidate is the ESGF publisher layout (``.../vYYYYMMDD/<file>``), the second
    the synda layout (``.../vYYYYMMDD/<variable_id>/<file>``).
    """
    path = Path(path)
    root = Path(root).expanduser()
    variable_id = path.name.split("_", 1)[0]
    return [root / path, root / path.parent / variable_id / path.name]


def get_local_file(path: Path, dataroots: list[Path]) -> Path:
    """Drop-in replacement for ``intake_esgf.base.get_local_file`` that knows both layouts."""
    for root in dataroots:
        for local_file in candidate_paths(path, root):
            if local_file.is_file():
                return local_file
    raise FileNotFoundError


def install_layout_resolver() -> None:
    """Make intake-esgf find files stored in the synda layout under any dataroot.

    ``intake_esgf.base`` looks ``get_local_file`` up as a module global at call time, so
    replacing the attribute affects every caller (``partition_infos``, ``parallel_download``
    and ``ESGFCatalog``'s cache reload). Calling this more than once is harmless.
    """
    intake_esgf.base.get_local_file = get_local_file


def uninstall_layout_resolver() -> None:
    """Restore intake-esgf's original lookup."""
    intake_esgf.base.get_local_file = _ORIGINAL_GET_LOCAL_FILE


def cmip6_dataset_dir(row: Mapping[str, Any]) -> Path | None:
    """Return the relative DRS version directory of a CMIP6 intake-esgf dataframe row."""
    if first_facet(row, ["mip_era", "project"]).upper() != "CMIP6":
        return None
    facets = [
        "activity_drs",
        "institution_id",
        "source_id",
        "experiment_id",
        "member_id",
        "table_id",
        "variable_id",
        "grid_label",
    ]
    parts = [first_facet(row, [f]) for f in facets]
    version = first_facet(row, ["version"]).removeprefix("v")
    if not all(parts) or not version:
        return None
    return Path("CMIP6", *parts, f"v{version}")


def locate_dataset(
    row: Mapping[str, Any], roots: Mapping[str, Path]
) -> tuple[str, int, int] | None:
    """Look for a CMIP6 dataset at its exact version under named roots.

    Returns ``(root_name, n_files, n_bytes)`` for the first root with files, counting each
    filename once even when both layouts are present, or ``None`` if no root has it. This is a
    dataset-level probe for dry-run reporting; the actual per-file resolution happens inside
    intake-esgf via ``install_layout_resolver``.
    """
    rel = cmip6_dataset_dir(row)
    if rel is None:
        return None
    variable_id = first_facet(row, ["variable_id"])
    for name, root in roots.items():
        vdir = Path(root).expanduser() / rel
        sizes: dict[str, int] = {}
        for directory in (vdir, vdir / variable_id):
            if directory.is_dir():
                for f in directory.glob("*.nc"):
                    if f.is_file():
                        sizes.setdefault(f.name, f.stat().st_size)
        if sizes:
            return name, len(sizes), sum(sizes.values())
    return None
