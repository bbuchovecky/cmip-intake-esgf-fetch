from __future__ import annotations

from pathlib import Path
from typing import Any

import intake_esgf

from .config import WorkflowConfig


def configure_intake_esgf(cfg: WorkflowConfig) -> None:
    """Configure intake-esgf for reproducible NCAR scratch-cache operation."""
    paths = cfg.paths
    settings: dict[str, Any] = {
        "local_cache": str(paths["local_cache"]),
        # "logfile": str(paths["logs"] / "intake-esgf.log"),
        # "download_db": str(paths["download_db"]),
    }

    iesgf = cfg.intake_esgf
    for key in ["all_indices", "num_threads", "confirm_download", "break_on_error", "additional_df_cols"]:
        if key in iesgf:
            settings[key] = iesgf[key]

    extra_roots = [str(Path(p).expanduser()) for p in iesgf.get("esg_dataroot", [])]
    if extra_roots:
        settings["esg_dataroot"] = extra_roots

    intake_esgf.conf.set(**settings)
