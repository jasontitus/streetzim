#!/usr/bin/env bash
# Download the shapefiles the tilemaker config reads; see the script this runs,
# resources/tilemaker/fetch-shapefiles.sh (kept there so installed wheels carry it).
#
#   scripts/fetch-shapefiles.sh            # into the current directory
#   scripts/fetch-shapefiles.sh /some/dir  # into /some/dir
exec bash "$(dirname "$0")/../resources/tilemaker/fetch-shapefiles.sh" "$@"
