#!/bin/bash

CACHE="${SCRATCH}/cmip_intake_esgf_fetch/esgf_cache"

find "${CACHE}" -type f -name "*.nc*" ! -name "*.nc" -print
find "${CACHE}" -type f -name "*.nc*" ! -name "*.nc" -delete
