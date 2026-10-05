# cmip-intake-esgf-fetch

A reproducible Python package for discovering, downloading, and cataloging native-grid CMIP5 and CMIP6 data using [`intake-esgf`](https://intake-esgf.readthedocs.io/), created by *gpt-5.5* agent.

It is designed for NCAR systems. Files that already exist on glade (the CMIP mirror at `/glade/campaign/collections/cmip.mirror` and NCAR's CDG publication at `/glade/campaign/collections/cdg/data`) are used in place. Only the rest is downloaded, into `${SCRATCH}/cmip_intake_esgf_fetch/esgf_cache` by default. The result is one [intake-esm](https://intake-esm.readthedocs.io/) catalog whose `path` column points at whichever copy is used.

## Quick start on NCAR

```bash
# 1. Create an environment. intake-esgf currently requires Python >= 3.12.
conda create -n cmip-intake-esgf python=3.12 -y
conda activate cmip-intake-esgf
pip install -e ".[dev]"

# 2. Copy then edit a configuration file.
cp configs/template.yml configs/my_cmip_request.yml

# 3. See what would come from glade and what would be downloaded. Downloads nothing.
cmip-intake-fetch fetch --config configs/my_cmip_request.yml --dry-run

# 4. Download what is missing and write the catalog. Run this in tmux for large requests.
cmip-intake-fetch fetch --config configs/my_cmip_request.yml
```

Then open the catalog with intake-esm or with `load_cmip_esgf.CMIPESGFLoader`:

```python
import intake
cat = intake.open_esm_datastore("/glade/derecho/scratch/$USER/cmip_intake_esgf_fetch/catalogs/cmip.json")
```

## Commands

| Command | What it does |
| --- | --- |
| `fetch --config C [--dry-run] [--progress]` | Search ESGF, select datasets, reuse local copies, download the rest, write the catalog. |
| `search --config C` | Search and write the manifests only. |
| `scan --dir D --output O [--pattern P] [--regex] [--no-open] [--workers N] [--esm-json]` | Catalog the NetCDF files under any directory, optionally opening each one for grid and time metadata. |
| `clean --config C [--delete]` | List, or delete, partial files left by interrupted downloads. |

## How glade copies are found

intake-esgf checks `<esg_dataroot>/<ESGF relative path>` before downloading each file. Most of the glade mirror was written by synda, which adds a directory for the variable:

```text
ESGF layout:  CMIP6/CMIP/NCAR/CESM2/historical/r1i1p1f1/Lmon/lai/gn/v20190308/lai_....nc
synda layout: CMIP6/CMIP/NCAR/CESM2/historical/r1i1p1f1/Lmon/lai/gn/v20190308/lai/lai_....nc
```

On its own, intake-esgf misses the synda layout and downloads a second copy. `local.install_layout_resolver()` patches intake-esgf's lookup to try both layouts. The lookup is per file, so a partially mirrored dataset downloads only its missing files.

Only the dataset version selected from ESGF (normally the latest) counts. If the mirror holds an older version, the newer one is downloaded.

## Output layout

```text
${SCRATCH}/cmip_intake_esgf_fetch/
  esgf_cache/                  # downloaded NetCDF files
  catalogs/
    <name>.csv                 # intake-esm catalog (paths.catalog in the config)
    <name>.json                # intake-esm descriptor; keep it next to the CSV
    <name>.csv.partial         # catalog of the groups finished so far, while fetch runs
  manifests/
    cmip_intake_manifest_raw.csv
    cmip_intake_manifest_selected.csv
    cmip_intake_manifest_rejected.csv
    fetch_plan.csv             # per dataset: mirror | cdg | cache | download
  logs/
    intake-esgf.log            # one line per file accessed or downloaded
    intake_esgf_download.db
```

The catalog has columns `activity_id, institution_id, source_id, experiment_id, member_id, table_id, variable_id, grid_label, version, time_range, location, path`. `location` names the root each file came from: `mirror`, `cdg`, `cache`, or `other`.

## Configuration notes

- `intake_esgf.esg_dataroot` maps a location name to a read-only root. The roots are checked in order before the cache. These replace intake-esgf's built-in list.
- `request.member_policy`: `first_member` or `all_members` (`all_members` includes the large ensembles).
- `request.grid_label_preference`: keep `["gn", "gr", "gr1"]` to prefer native grids.
- Configs share `paths.manifests`, so the manifests and `fetch_plan.csv` describe the most recent run.

## Notes and caveats

- Historical CMIP5 usually ends in 2005 and historical CMIP6 in 2014. Only files overlapping `start_year`..`end_year` are fetched, but some files span several decades and include earlier years.
- The `--dry-run` probe only classifies CMIP6 datasets. CMIP5 datasets always show as `download` in the plan, even if intake-esgf finds them locally during the real run.
- `intake-esgf` facet support differs between CMIP5 and CMIP6. If a CMIP5 search is sparse, inspect the raw manifest and adjust `project_configs.CMIP5` in the YAML file.
- The package uses original native-grid files. It does not regrid or rewrite model output.
- The layout resolver patches a private intake-esgf function, so `intake-esgf` is pinned below 2027. `tests/test_local.py` fails if that function's signature changes.
