# cmip-intake-esgf-fetch

A reproducible Python package for discovering, downloading, and inventorying native-grid CMIP5 and CMIP6 historical data using [`intake-esgf`](https://intake-esgf.readthedocs.io/), created by *gpt-5.5* agent.

The package is designed for NCAR systems and stores the intake-esgf local cache in `${SCRATCH}/cmip_intake_esgf_fetch/esgf_cache` by default.

## Why intake-esgf?

`intake-esgf` searches ESGF indices, resolves file locations, downloads files into a configurable local cache, reuses existing cached files, and returns either xarray datasets or local paths. It also harvests cell-measure information when loading datasets.

## Quick start on NCAR

```bash
# 1. Create an environment. intake-esgf currently requires Python >= 3.12.
conda create -n cmip-intake-esgf python=3.12 -y
conda activate cmip-intake-esgf
pip install -e .

# 2. Copy then edit your configuration file.
cp configs/template.yaml configs/my_cmip_request.yaml

# 3. Build a manifest from intake-esgf search results. This is relatively quick.
cmip-intake-fetch build-manifest --config configs/my_cmip_request.yaml

# 4. Download selected datasets into the intake-esgf cache. This will take a while.
cmip-intake-fetch download --config configs/my_cmip_request.yaml

# 5. Build an inventory of local NetCDF files. This should work, but you may want
#    to use `scripts/catalog_esgf_cache.py` instead.
cmip-intake-fetch catalog --config configs/my_cmip_request.yaml --no-open
```

## Output layout

The workflow writes control files to:

```text
${SCRATCH}/cmip_intake_esgf_fetch/
  esgf_cache/                # intake-esgf local cache; downloaded NetCDF files live here
  manifests/
    cmip_intake_manifest_raw.csv
    cmip_intake_manifest_selected.csv
    cmip_intake_manifest_rejected.csv
    cmip_intake_downloaded_paths.csv
    cmip_intake_inventory.csv
  logs/
    intake-esgf.log
```

## Recommended workflow

1. Make your own copy of `config.yml` and make the following edits.
    1. Change the paths to match your directory structure.
    2. Confirm whether you want one ensemble member per model (`first_member`) or all members (`all_members`). I picked `all_members` to include the large ensembles.
    3. Prefer native grids for CMIP6 by keeping `grid_label_preference: ["gn", "gr", "gr1"]`.
2. Build the manifest using using `cmip-intake-fetch build-manifest` and inspect it before downloading.
3. Download using `cmip-intake-fetch download`. This calls `intake-esgf` path retrieval, which downloads missing files and reuses files already present in the cache. This may take a while so I'd recommend running it in a terminal that will persist if you lose SSH connection (e.g., tmux).
4. Rebuild the catalog using `scripts/catalog_esgf_cache.py` to reference later when loading the data using `load_cmip_esgf.py`.

You also may want to edit the path in `scripts/clean_tmp_netcdf.sh` and then run the script to clean up any remaining temporary NetCDF files from interrupted or incomplete downloads.

## Notes and caveats

- Historical CMIP5 usually ends in 2005 and historical CMIP6 usually ends in 2014; this workflow requests files intersecting the configured start year onward, but some ESGF files cover multi-decade ranges and may include years before 1950.
- `intake-esgf` facet support differs somewhat between CMIP5 and CMIP6 because their metadata conventions differ. If a CMIP5 search is sparse, inspect the raw manifest and adjust `project_configs.CMIP5` in the YAML file.
- The package downloads original native files; it does not regrid or rewrite model output.
