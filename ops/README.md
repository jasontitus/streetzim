# ops/: StreetZim's own operations

Everything here runs StreetZim's own distribution, not the builder. That
covers:
- **the build host** (`/storage/streetzim`): queues, build wrappers, world
  data, host hygiene;
- **publishing**: the rollout gates and viewer rollouts, uploads to
  archive.org, and torrents;
- **infrastructure and caches**: the VM scripts, terrain-cache maintenance,
  and the world search-cache builders.

The builder ("core": `create_osm_zim.py`, `streetzim/`, `resources/`, the
`cloud/` modules it imports, and the ZIM tools CI runs) does not depend on
anything here. `tools/check_boundary.py` enforces that in CI.

A Zimfarm or openZIM build needs none of this.

## Stage 1 (now): same repository, old paths kept

**Files live at `ops/<old path>`,** for example `ops/build-region-fast.sh`,
`ops/cloud/upload_validated.sh` and `ops/tmp/map-health.mjs`.

**A symlink at each old path** that the build host runs or reads points
into `ops/`. So `/storage/streetzim/build-region-fast.sh`, cron entries,
untracked host scripts and `pgrep` patterns keep working after a
`git pull`. Moved docs and tests have no symlink.

**Run ops scripts by their old path.** They were written for it:
- **Shell scripts** compute the checkout from `dirname "$0"`, and `pgrep`
  matches their old names. A moved shell script started directly (as
  `ops/...`, or from inside `ops/`) re-runs itself through its old-path
  symlink; see the guard at the top of each.
- **Python ops files** find the checkout with `_streetzim_root()`:
  `$STREETZIM_ROOT`, else the nearest directory above the file holding
  `create_osm_zim.py`. So they work by either path.
  `ops/tests/test_streetzim_root.py` checks every copy.
- **`ops/tmp/map-health.mjs`** does the same in JavaScript.

**Edit the file under `ops/`, never through the old path with
`sed -i`.** `sed -i` replaces the symlink with a regular file: the edit
never reaches `ops/`, and the next pull conflicts. Editors and `>`
redirections write through the link, which is fine. On the host, the
older rule still applies: write `X.new`, then `mv -f X.new X`.

**Some operations files are still at their old paths,** listed in
[`in-place.txt`](in-place.txt):
- **`web/`, `preview-proxy/`** and the tests and scripts tied to them by
  relative paths;
- **the files the host edits in place:** the `*.list` queues,
  `viewer-refresh.tsv`, `cloud/region-variants.tsv` and
  `tmp/live-inventory.out`. A symlink there would be replaced by the next
  `sed -i`, and moving them would make `git pull` fail over a local edit.

**`cloud/regions.tsv` is core.** The builder reads it for regions'
opening views.

**New ops code goes under `ops/`,** and finds the checkout with
`$STREETZIM_ROOT` (or the `_streetzim_root()` pattern). It never
computes the checkout from its own path.

Checks (in CI): `python tools/check_boundary.py` and
`python -m pytest ops/tests -q`.

## Stage 2: a separate repository

On the build host, in this order:

1. **Before anything, run:**
   - `git -C /storage/streetzim status`: local edits block pulls;
   - `crontab -l`, and `ps -eo pid,args | grep -E '\.sh'`;
   - a grep of the untracked `/storage/streetzim/.*.sh` scripts for
     every path in `ops/` and in `in-place.txt`.
2. **Create `streetzim-ops`** from this repository's history:
   `git filter-repo --path ops/ --path-rename ops/:`, plus the in-place
   paths. The ops repository then holds today's `ops/` at its root: the
   same relative layout, with `docs/` and `tests/` in it.
3. **Clone it next to the builder,** for example at `/storage/streetzim-ops`.
   Export `STREETZIM_ROOT=/storage/streetzim` for the ops jobs.
4. **Switch the callers:** crontab, the untracked host scripts, and any
   absolute `/storage/streetzim/<ops script>` inside the ops scripts. Stop
   and restart the long-running queues by their new paths.
5. **Remove from this repository:**
   - `ops/`;
   - the old-path symlinks;
   - the in-place files, which have moved to the ops repository;
   - `ruff.toml`'s symlink excludes, and the ops steps in CI.

   The builder then contains no operations code.
