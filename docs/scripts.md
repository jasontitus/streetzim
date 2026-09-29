# Shell scripts: inventory and consolidation plan

Status 2026-09-28: 73 tracked `*.sh` files. Phase 1 moved 31 dead ones to
`attic/`, leaving **42** in use; about 23 are actually needed. The rest are
one-offs or wrappers superseded by `build-region-fast.sh`. This file lists every script with a verdict and gives
the order for cutting the count down **without breaking the production host**,
which runs these scripts from `/storage/streetzim` (often by absolute path,
sometimes while you edit them).

Verdicts:
- **KEEP**: live and canonical.
- **RETIRE**: move to `attic/` with `git mv`, which keeps the history. Nothing
  tracked executes it.
- **RETIRE after check**: needs a look at the prod host first (it may be
  running, or an untracked prod script may call it).
- **SHIM**: keep the filename, and replace the body with an `exec` of the
  canonical script.
- **MERGE**: fold its behaviour into another script as a flag.

## Rules for changing scripts on the build host

These come from how the scripts reference each other today:

1. **Step 0, before retiring anything.** On `/storage/streetzim`:
   - run `ps -eo pid,args | grep -E '\.sh'` and `crontab -l`;
   - grep the **untracked** dot-scripts (`.queue-*.sh`, `.allzims-*.sh`, …)
     for the names you are about to move:
     ```
     grep -lE 'build-region\.sh|build_region\.sh|retrofit-chips-queue\.sh|viewer-refresh-queue|regate-stranded|europe-safety-net|ship-one|light-swap|canada_viewer_repack|finish-pending-loop|build-world-z12' /storage/streetzim/.*.sh /storage/streetzim/*.sh
     ```
   - Any hit blocks that script.
2. **Never edit a running script in place.** bash reads a script while it
   runs. Write `X.new`, then `mv -f X.new X`, as commit 273184a did. A
   `git pull` also replaces files by rename, so it is safe for running
   scripts. Moving or deleting a file is safe for a process already running
   it; only *future* invocations by that path break.
3. **Keep these names.** Other scripts match them with `pgrep` or `ps`:
   `build-refresh-queue.sh`, `ship-region.sh`, `build-world-tiles.sh`,
   `extract-region-pbfs.sh`, `nvme-guard.sh`, `retrofit-chips-queue.sh`.
   Also keep every lock (`.retrofit-upload.lock`, `tmp/.regions-pbf.lock`,
   `tmp/.world-tiles.lock`, `.finish-pending.lock`), pidfile
   (`.rollout-viewer.pid`, `.rebuild-old.pid`), log name and port.
4. **`tmp/overlap-check.mjs`, `tmp/map-health.mjs` and `tmp/device-matrix.mjs`
   are live dependencies** of the rollout gates, despite the directory name.
   Leave them where they are until their callers are updated in the same
   commit.
5. **Before any `git rm` sweep**, tag `pre-script-consolidation`, so any file
   can be brought back with `git show <tag>:<path>`.

## Phase 1: retire (nothing tracked calls these) — DONE

Moved to `attic/` with `git mv`. Before moving, the tree was searched for
every moved filename: the only remaining mentions are comments, doc text,
`cloud/regions.tsv` notes and `firebase.json` ignore entries.

These use `/Users/jasontitus/…`, `venv312`, AWS, or April-era sources, or
they declare themselves deprecated. Commit dates of 2026-09-03/05 on several
of them come from repo-wide sweeps, not real use.

| script | why |
|---|---|
| `build-small-region.sh` | calls the Mac wrapper; its smoke test uses `http.server`, which 404s on the Find page |
| `build-region-and-upload.sh` | calls the Mac wrapper; superseded by `ship-region.sh` |
| `build-region-no-routing.sh` | no callers; `build-region.sh` minus `--routing` |
| `build-ukraine.sh`, `build-california-2026-05-10.sh` | dated one-offs |
| `build-europe-salvage.sh`, `build-us-salvage.sh` | one-time salvage of pinned JSONL |
| `build-coasts.sh`, `build-ausnz.sh`, `build-and-upload-queue.sh`, `rebuild-queue.sh`, `drive-rollout.sh`, `overture-rollout-redo.sh` | Mac paths; `build-ausnz.sh` uploads without validation |
| `rebuild-all-final.sh`, `upload-to-archive.sh` | self-declared DEPRECATED (exit 64) |
| `build_world_and_us.sh` | March local pipeline |
| `cloud_rsync_loop.sh`, `cloud_terrain_{collect,launch,monitor}.sh` (+ `cloud_terrain_gen.py`) | AWS terrain run, finished |
| `cloud/rollout_viewer_swap.sh`, `cloud/upload_shipped.sh` | April repackage rollout; superseded by `rollout_viewer_patch.sh` |
| `cloud/reroll_viewer.sh`, `cloud/reroll-sv-iran.sh`, `cloud/chip-retrofit-d.sh`, `cloud/rebuild-all.sh` | April one-offs, superseded by `swap_viewer_rust.py --reshard-*` |
| `cloud/rebuild_overture_regions.sh`, `cloud/restart-build.sh` | April; restart-build skips validation |
| `web/watch-and-deploy.sh` (and the tracked `web/watch-and-deploy.log`) | Mac poller |
| `tests/_regen_headers.sh` | self-described one-shot |
| `tmp/build-swiss-light.sh` | replaced by `cloud/region-variants.tsv` |

Also candidates, though they aren't shell scripts: root `fix_*.py` (April
terrain repairs), root `test_*_compression.py` and `test_zim_perf.py`
(research scripts cited by `docs/*-compression-analysis.md`; update those
links; `pytest.ini` keeps bare `pytest` from collecting them),
`.smoke_viewer_playwright.py`, and the unreferenced `tmp/chip-test*.mjs`,
`tmp/search-probe.mjs`, `tmp/shim-test.js`, `tmp/try-css.mjs`.

After the move, update the few docs that mention them:
`docs/remote-rebuild.md` and `docs/zim-packaging-gotchas.md` (mentions of
`build-region-and-upload.sh`, which should now point to `ship-region.sh`),
and the compression-analysis docs.

## Phase 2: retire after the step-0 check

| script | check first |
|---|---|
| `.after-round-rebuild-ca-dc.sh` | may still be sleeping on `pgrep build-refresh-queue` |
| `europe-safety-net.sh` | hard-codes one ZIM and one PID; STATUS says it is stopped |
| `regate-stranded.sh` | `regate-stranded.tsv` complete |
| `viewer-refresh-queue.sh` | `viewer-refresh.tsv` complete (`rollout_viewer_patch.sh` replaces it) |
| `cloud/canada_viewer_repack.sh` | Canada shipped; not waiting on `.rollout-viewer.pid` |
| `ship-switzerland-light.sh`, `tmp/light-swap.sh`, `tmp/ship-one.sh`, `tmp/ship-layout-fixed.sh` | one-offs from 2026-09-19/21, done |
| `cloud/build_region.sh` | the Mac is no longer a build host (all its remaining callers are in phase 1) |
| `cloud/launch-build-vm.sh`, `cloud/build-vm-startup.sh`, `cloud/spot-to-ondemand-watcher.sh`, `cloud/vm-health-cron.sh`, `cloud/wait-and-launch.sh`, `cloud/upload-caches.sh` | GCP builds are retired. Note that a VM build today would ship a feature-regressed ZIM through a raw `ia upload`. Check the Mac's `crontab -l` for `vm-health-cron.sh` |

## Phase 3: shims and merges

- **`build-region.sh` → SHIM.** It takes the same `<id> <bbox> <name>` and
  writes the same output and log names as `build-region-fast.sh`, and the
  docs already say not to use it. New body:
  ```bash
  #!/usr/bin/env bash
  echo "build-region.sh is deprecated; running build-region-fast.sh" >&2
  exec env STAGE_MBTILES_NVME="${STAGE_MBTILES_NVME:-0}" bash /storage/streetzim/build-region-fast.sh "$@"
  ```
  Callers get the production flag set (Wikipedia, rust packer, variants).
  That is an upgrade, but it is a behaviour change. It no longer writes the
  raw `osm-<id>.zim` intermediate, which nothing waits for.
- **`finish-pending-loop.sh` → MERGE into cron.** The loop only re-runs
  `cloud/finish_pending_uploads.sh`, which already holds a lock, is
  idempotent, and exits when there is nothing to do:
  `*/30 * * * * cd /storage/streetzim && bash cloud/finish_pending_uploads.sh >> finish-pending.log 2>&1`.
- **`build-world-z12.sh` → MERGE into `build-world-z13.sh` as `PROFILE=z12`.**
  Keep `--max-zoom 12`, no terrain, `world-no-streets.jsonl`, its log name and
  its output name.
- **Optional:** add `NO_ROUTING=1` and `MAP_CENTER`/`MAP_ZOOM` pass-through to
  `build-region-fast.sh`. These are the only features `cloud/build_region.sh`
  has that the canonical wrapper lacks. Deploy by rename.

## Phase 4: one shared library instead of copy-paste

The gate and upload logic is repeated across the queue and rollout scripts,
and six scripts extract functions from `retrofit-chips-queue.sh` at runtime:

```bash
sed -n '/^smoke_find()/,/^}/p;/^smoke_search()/,/^}/p;/^browser_smoke()/,/^}/p' retrofit-chips-queue.sh > $TMP; . $TMP
```

That fails closed: reformatting `retrofit-chips-queue.sh` makes every
extractor's gate report FAIL. Proposed layout:

- `scripts/lib/gates.sh`:
  - `smoke_find` (the superset of the retrofit and build-refresh variants);
  - `smoke_search`;
  - `equivalence`;
  - `browser_smoke` (port from `$PORT`);
  - `terrain_gate`;
  - `route_gate`;
  - `kiwix_ui_gate` (the kiwix-serve + overlap + device-matrix + render block
    that `rebuild_old_regions.sh` and `rollout_viewer_patch.sh` duplicate);
  - `markers <set>`.
- `scripts/lib/common.sh`:
  - the environment preamble (`PY`, `NODE`, `CHROME_PATH`, `TMPDIR`, …);
  - `registry_row <id>`;
  - `upload_locked <id> <zim>`, which always takes `.retrofit-upload.lock`.
    That fixes the lock bypass in `build-refresh-queue.sh` and
    `ship-region.sh`, and it is a timing change: uploads serialise behind
    rollouts;
  - `ensure_overture`;
  - `reclaim_scratch`;
  - `wait_pidfiles`.

Order:
1. Add the library files. They are new, so nothing changes.
2. Switch or retire each `sed` consumer.
3. Switch `retrofit-chips-queue.sh` itself **last**.
4. Switch `build-refresh-queue.sh` and `ship-region.sh` in a separate commit,
   because their gate semantics change.

Library code must be `set -u` clean and use absolute paths.

## After all phases

About 23 live scripts plus two library files, down from 73. If the Mac and GCP
tooling stays, 30.

## The live set (KEEP)

| family | scripts |
|---|---|
| build | `build-region-fast.sh` (canonical), `ship-region.sh` (build + gates + upload for one region), `cloud/rebuild_old_regions.sh` |
| queues | `build-refresh-queue.sh`, `retrofit-chips-queue.sh`, `run-continent-chain.sh`, `cloud/finish_pending_uploads.sh` |
| world data | `download-planet.sh`, `extract-region-pbfs.sh`, `build-world-tiles.sh`, `build-world-z13.sh` |
| viewer rollout | `cloud/rollout_viewer_patch.sh`, `cloud/kiwix_viewer_gate.sh` |
| upload / web | `cloud/upload_validated.sh` (the only upload path), `cloud/upload_url_cache.sh`, `cloud/deploy_pwa.sh`, `scripts/sync-drive-viewer.sh` |
| host hygiene | `scripts/nvme-guard.sh`, `scripts/resource-watch.sh`, `scripts/ssd-reap.sh`, `tmp/upload-recorder.sh` |
| setup / tests | `scripts/fetch-shapefiles.sh`, `tests/run_identity_suite.sh` |
