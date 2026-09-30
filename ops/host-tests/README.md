# Build-host test scripts

Scripts used on the build host (ot-hel1) to test main before making it the
production default. They assume that host's layout (`/storage/streetzim`,
`$HOME/sz-env.sh`, the `streetzim:hosttest` image) and are kept here for
reference, not as general tools.

- `run-openzim-recipe.sh dc|switzerland`: builds a map the way openZIM's
  Zimfarm would (the `streetzim` command in the Docker image, Geofabrik poly,
  default profile, 16 GiB memory limit). The only difference from a Zimfarm
  task is that tmp/dl live on local disk; it does not change the output.
- `run-driver-gates.sh <zim> <search term>`: the round driver's extra gates
  (markers, device matrix, map render, Kiwix in-ZIM viewer), copied from
  `ops/cloud/rebuild_old_regions.sh`.
- `layout-matrix.sh <zim> <port>`: `overlap-check.mjs` at 7 portrait and
  4 landscape phone sizes.
- `run-viewer-compat.sh`: the old and new `/drive/` viewers against published
  ZIMs.
