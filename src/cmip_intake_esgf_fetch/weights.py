from __future__ import annotations

import xarray as xr


def land_area_weights(ds: xr.Dataset, area_name: str = "areacella", landfrac_name: str = "sftlf") -> xr.DataArray:
    """Return land-area weights from area and land-fraction variables.

    The returned weights have units of square meters when `areacella` is in square meters and `sftlf`
    is a percent land fraction.
    """
    if area_name not in ds:
        raise KeyError(f"Missing area variable {area_name!r}")
    if landfrac_name not in ds:
        raise KeyError(f"Missing land-fraction variable {landfrac_name!r}")
    weights = ds[area_name] * ds[landfrac_name] / 100.0
    weights.name = "land_area_weight"
    weights.attrs.update(
        {
            "long_name": "grid-cell land area weight",
            "description": f"Computed as {area_name} * {landfrac_name} / 100",
        }
    )
    return weights
