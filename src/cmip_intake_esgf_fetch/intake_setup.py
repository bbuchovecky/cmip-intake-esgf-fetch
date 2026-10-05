from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import intake_esgf

from .config import WorkflowConfig
from .local import install_layout_resolver


def _attach_file_log(logfile: Path) -> None:
    """Send intake-esgf's log records (per-file access/download messages) to ``logfile``."""
    logger = logging.getLogger("intake-esgf")
    logger.setLevel(logging.INFO)
    target = str(logfile.resolve())
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == target:
            return
    handler = logging.FileHandler(target)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)


def configure_intake_esgf(cfg: WorkflowConfig) -> None:
    """Configure intake-esgf for reproducible NCAR scratch-cache operation.

    The configured ``esg_dataroot`` list replaces intake-esgf's default list, so the roots
    checked before downloading are exactly those in the YAML file. The layout resolver is
    installed so files stored as ``vYYYYMMDD/<variable_id>/<file>.nc`` are found too.
    """
    paths = cfg.paths
    settings: dict[str, Any] = {
        "local_cache": [str(paths["local_cache"])],
        "esg_dataroot": [str(p) for p in cfg.dataroots.values()],
    }

    iesgf = cfg.intake_esgf
    for key in ["all_indices", "num_threads", "confirm_download", "break_on_error", "additional_df_cols"]:
        if key in iesgf:
            settings[key] = iesgf[key]

    intake_esgf.conf.set(**settings)
    # conf.set() does not accept download_db, but the config object is a plain dict.
    intake_esgf.conf["download_db"] = str(
        paths.get("download_db", paths["logs"] / "intake_esgf_download.db")
    )
    _attach_file_log(paths.get("logfile", paths["logs"] / "intake-esgf.log"))
    install_layout_resolver()
