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
`git pull`. Files moved from `docs/` and `tests/` have no symlink
(nothing runs them by path).

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

**Edit and replace files at their `ops/` path.** For an atomic replace of a
script that may be running, write `ops/X.new`, then `mv -f ops/X.new ops/X`.
Never replace a file at its old path: `mv`, `sed -i`, `perl -pi`, `install`,
`rsync` (without `--inplace`) and Python's `os.replace` all swap the symlink
for a regular file. Then:
- the host silently keeps running that copy;
- `git status` shows `T`;
- later upstream changes to `ops/X` are pulled but never run.

Editors that write in place, and `>` redirections, write through the link,
which is fine. Change scripts on a development machine and commit them
there; the host only pulls.

**Pulling on the build host (stage 1).**
1. `git -C /storage/streetzim status --short` first. On a symlinked old
   path, or under `ops/`, any `T`, `M` or `D` must be resolved before
   pulling.
2. To keep a host change to a script:
   - `cp X ops/X`, then `git checkout -- X` (that restores the symlink);
   - commit `ops/X` on a development machine, not on the host.
3. To drop a change: `git checkout -- <path>`.
4. Never `git checkout --` the in-place data files (`*.list`,
   `viewer-refresh.tsv`, `cloud/region-variants.tsv`). The host edits those
   by design.
5. Pull with `git pull --ff-only`. A pull that stops on local changes has
   changed nothing: resolve and pull again.
6. The build VMs (`ops/cloud/build-vm-startup.sh`) also `git pull --ff-only`.
   On failure they warn and build with the old checkout.

[`TESTING-STAGE1.md`](TESTING-STAGE1.md) is the step-by-step check to run
before, and just after, the build host takes this change.
[`TESTING-NEXT.md`](TESTING-NEXT.md) got the split, the builder and the
`-next` work onto `main` (PRs #20, #21, #22) and lists the tests to run on
the host. Both were carried out on 29–30 September: the host checkout is
now on branch `host-main`, which tracks `origin/main`, and takes changes
with `git pull --ff-only` as above.

**Caveats:**
- The guard does not keep bash options: `bash -x ops/X.sh` re-runs without
  `-x`. Trace with `bash -x <old path>`.
- Every Python ops file that looks up the checkout, and
  `ops/tests/test_render_gate_anchors.py`, honours `$STREETZIM_ROOT`
  (`derive-region-mbtiles.py` and `derive-region-search.py` hard-code
  `/storage/streetzim`). Leave it unset for normal runs.
- The root is found with `realpath`. If `/storage/streetzim` is itself a
  symlink, printed paths show the resolved directory.

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

**Python packages for ops: `requirements-ops.txt`.** `requirements.txt`
lists only what the builder imports. The Internet Archive client
(`internetarchive`, which provides the `ia` command the upload, rollout
and cleanup scripts run) is in `requirements-ops.txt`, which includes
`requirements.txt`. On the build host, install from it:

```bash
/storage/streetzim/venv-linux/bin/pip install -r requirements-ops.txt
/storage/streetzim/venv-linux/bin/ia --version     # must print a version
```

A venv that already has `ia` keeps it: `pip install -r requirements.txt`
never uninstalls anything. Use `requirements-ops.txt` whenever a host
venv is created or rebuilt, or `ia` will be missing and uploads fail.
The build VMs (`ops/cloud/build-vm-startup.sh`) install `internetarchive`
by name. Tests and lint tools (pytest, ruff, pyright) are in
`requirements-dev.txt`.

## Stage 2: a separate repository

On the build host, in this order:

1. **Before anything, run:**
   - `git -C /storage/streetzim status`: local edits block pulls;
   - `crontab -l`, and `ps -eo pid,args | grep -E '\.sh'`;
   - a grep of the untracked `/storage/streetzim/.*.sh` scripts for
     every path in `ops/` and in `in-place.txt`.
2. **Create `streetzim-ops`** from this repository's history:
   `git filter-repo`, keeping `ops/` **and every old path** (so the
   history from before the move survives), then renaming the old paths
   into place, plus the in-place paths. The ops repository then holds today's `ops/` at its root: the
   same relative layout, with `docs/` and `tests/` in it.
3. **Clone it next to the builder,** for example at `/storage/streetzim-ops`.
   Export `STREETZIM_ROOT=/storage/streetzim` for the ops jobs.
4. **Switch the callers:** crontab, the untracked host scripts, and any
   absolute `/storage/streetzim/<ops script>` inside the ops scripts.
   Also:
   - the eight shell scripts that compute the checkout from their own
     path must read `$STREETZIM_ROOT` instead. They are in `ops/cloud/`:
     `build_region.sh`, `launch-build-vm.sh`,
     `spot-to-ondemand-watcher.sh`, `upload-caches.sh`,
     `upload_url_cache.sh`, `upload_validated.sh`, `vm-health-cron.sh`,
     `wait-and-launch.sh`;
   - the scripts that `cd /storage/streetzim` and call ops scripts
     relatively need the ops checkout's path. Examples:
     `QUEUE=./build-refresh-queue.sh`, and the `sed` extractions from
     `retrofit-chips-queue.sh`.

   Then stop the long-running queues and restart them by their new paths.
5. **Remove from this repository:**
   - `ops/`;
   - the old-path symlinks;
   - the in-place files, which have moved to the ops repository;
   - `ruff.toml`'s symlink excludes, and the ops steps in CI.

   The builder then contains no operations code.
