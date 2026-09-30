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
- `gate-zim.sh <zim> <region id>`: `ops/ship-region.sh`'s five gates (terrain
  coverage, validator, routing, search and Find, browser smoke) on a ZIM built
  elsewhere, run in a test clone with its own `web/`; no build, no upload.
  `GATE_BBOX`, `GATE_SRC`, `GATE_DST` and `GATE_SEARCH` stand in for a region
  that is not in `cloud/regions.tsv`.
- `run-measured.sh <tag> <image> <poly> <name> <title> [args]`: the openZIM
  recipe in a container limited like a Zimfarm task (`--memory 16g
  --cpu-shares 3072`), with `memprofile.sh` attached.
- `memanon.sh <container> <out.tsv>`: the container's anonymous memory every
  second from its cgroup (what a memory limit kills for; page cache is
  reclaimed first), with the running peak.
- `memprofile.sh <container> <build.log> <out.tsv>`: every 5 s, the
  container's PSS, its three largest processes and the build log's last
  line, to tie memory to a build step. Short spikes fall between samples.
- `run-cpu-count-ab2.sh <tag> <count|default>`: Luxembourg `basic` with
  Python's CPU count forced (`PYTHON_CPU_COUNT`), for worker-count A/B runs.
- `recdiff.py A.zim B.zim`: for the search JSON lists, how many files are
  identical, reordered, or hold different records.
- `validate-full.sh <out dir> <name> <title> <image>`: everything a
  full-profile openZIM build must pass before upload, nothing skipped:
  `tools/check_openzim_output.py` (metadata, illustration, progress, routing,
  terrain), `tools/check_full_profile.py` (Overture, Wikidata and Wikipedia
  content, credited), `gate-zim.sh` and `run-driver-gates.sh`.
- `run-nl-full.sh <tag> <image>`: the Netherlands, full profile, the recipe's
  Wikipedia ZIM option (the host's copy mounted read-only where the download
  cache expects it), in a Zimfarm-sized container, memory sampled.

`run-driver-gates.sh` now fails when its marker check fails (it used to log
the failure and still report success).
