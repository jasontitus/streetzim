# Moving the build host to `-builder` and `-next`, and testing them there

A runbook for a Claude Code session on the build host (`/storage/streetzim`).
The host checked the ops split in a scratch clone (stage 1 steps 1–3,
[TESTING-STAGE1.md](TESTING-STAGE1.md)); its checkout is still on `main`
(`37403b8` plus any torrent commits of its own). This page gets the split
onto `main` and the host onto it (§1.0), then moves the host on to the
builder changes and this round's work, and runs the tests that only the
host can run. Merging to `main` is the user's action, through PRs;
nobody pushes `main`.

**Branches** (on the host checkout's `origin`, GitHub):

| short name | branch | contains |
|---|---|---|
| ops | `claude/adoring-dijkstra-i2vge7` | `main` (`37403b8`) merged into the ops split (`bd09db9`, CI green: run 36528140810) |
| builder | `claude/adoring-dijkstra-i2vge7-builder` | the builder commits on the ops split (`3d8e7d7`) |
| next | `claude/adoring-dijkstra-i2vge7-next` | builder plus this round's work (`d5238cb` when this was written) |

Still to land on `next` (each step that needs one says so, with its check):

| topic branch | what | check (in the test clone, step 1.2) |
|---|---|---|
| `topic-viewer-polish` | viewer fixes | `git -C /storage/sz-tests/next log --oneline --first-parent --grep=topic-viewer-polish gh/next` prints a merge |
| `topic-wiki-429` | Wikipedia/Wikidata fetch: a 429 is never cached as a miss; `Retry-After` honoured | `--grep=topic-wiki-429` prints a merge, and `git -C /storage/sz-tests/next grep -qi retry-after gh/next -- cloud/wiki_articles.py && echo yes` prints `yes` |
| `topic-mbtiles-flag` | `streetzim --mbtiles-url`, and planet-size MBTiles cut to the area | `--grep=topic-mbtiles-flag` prints a merge, and `git -C /storage/sz-tests/next grep -q -- --mbtiles-url gh/next -- streetzim/cli.py && echo yes` prints `yes` |
| (the profile branch) | `streetzim --profile` (full profile: terrain etc.) | `git -C /storage/sz-tests/next grep -q -- "--profile" gh/next -- streetzim/cli.py && echo yes` prints `yes`; `git -C /storage/sz-tests/next log --oneline --first-parent -i --grep=profile gh/next` names the merge |

## 0. Rules for the session

- **Nothing moves while a build runs.** Before any step that changes
  `/storage/streetzim`, `pgrep -f '[c]reate_osm_zim|[u]pload_validated'`
  must print nothing. A spawned build worker re-imports code from disk,
  and an upload deploys `web/` from the checkout. The blocks below check
  this themselves. If the round's queue starts the next region at once,
  there is no gap: ask the user how to pause it.
- **Git on `/storage/streetzim`:** only the commands written here. Moves
  are fast-forward only (`pull --ff-only --no-rebase` with
  `merge.autoStash=false` and `rebase.autoStash=false`). Never
  `stash`, `reset`, `checkout -- <path>`, `restore`, `rebase`, `commit`,
  `push`, or `clean` there.
- **Record before and after.** Each move writes `$HOME/sz-move-<name>.before`
  (branch and commit) and `.after` (commit), and refuses to run twice.
  Rollback (§1.7) uses them.
- **The in-flight rebuild round is not ours.** Don't change its settings,
  environment, `queue-refresh.tsv`, `queue-refresh.release`, lists or
  logs; don't stop, restart or resume it unless the user says so.
  `OVERTURE_RELEASE` stays `2026-08-19.0` (the wrappers' default); never
  set it to `latest` for anything touching the round.
- **Host-edited files stay as they are:** the `*.list` queues,
  `viewer-refresh.tsv`, `cloud/region-variants.tsv` and
  `tmp/live-inventory.out` ([in-place.txt](in-place.txt)). Never edit,
  restore or commit them. `M` on them in `git status` is expected.
- **Never publish from a test.** No `cloud/upload_validated.sh`, no
  `web/generate.py --deploy`, no `ia upload`, no Firebase, no torrent.
  Test outputs go under `/storage/sz-tests/`; a test that writes a ZIM into
  `/storage/streetzim` moves it out when it finishes (§2c).
- **Tests run in a separate clone,** `/storage/sz-tests/next`, and write
  only under `/storage/sz-tests`. Only §1.0 (its pull), §1.4–1.7 and §2c change the
  production checkout.
- **Stop and report at any `STOP` or `FAIL`.** Don't work around it.
- Every command below is written with absolute paths. Where a block sets
  shell variables, run it as one command.

## 1. Moving the host: main → ops split → builder → next

**Which branch, when.** First `main` gets the ops split and the host pulls
it (§1.0). The production checkout then stays there while the tests of
§2a–b run in the clone. It moves to `builder` when §1.2–1.3 and §2a pass
and the user says go; to `next` after §2c passed on `builder` and the user
says go; to `main` after the merges (§3).

### 1.0 The ops split onto `main`, and the host's stage-1 pull

`main` (`37403b8`) is an ancestor of `bd09db9`, so either way below keeps
`bd09db9` verifiable and lets the host fast-forward:

| way | result on `main` | CI | verdict |
|---|---|---|---|
| PR from `claude/adoring-dijkstra-i2vge7`, merged with **Create a merge commit** | a new merge commit whose tree is exactly `bd09db9`'s (plus any host commit merged first); `bd09db9` is its second parent | runs on the PR | **recommended**: follows the PRs-only rule and works with branch protection |
| the user fast-forwards `main` to `bd09db9` (`git push origin bd09db9:main`, by the user or with their explicit approval) | `main` = `bd09db9` exactly | already green on `bd09db9` (run 36528140810) | acceptable only if `main` is still `37403b8` and branch protection allows it; it is a direct push to `main` |

Never squash or rebase-merge: either gives `main` commits that don't
contain `bd09db9` or the host's commits, and the host's `pull --ff-only`
then refuses.

**The host's own commits come first.** The round is running
(east-coast-us built at 05:41 PDT; its torrent commit is pending).
`upload_validated.sh` commits torrents on the host after each upload. Any
such commit must be on `main` before the host can pull, or the pull
refuses (and changes nothing). So:
1. On the host, after the pending torrent commit exists, list what `main`
   lacks: `git -C /storage/streetzim log --oneline '@{u}..HEAD'`.
2. If that lists commits: the user gets them to GitHub (for example
   `git -C /storage/streetzim push origin HEAD:refs/heads/host-commits-2026-09-29`,
   run only on the user's explicit word) and merges that branch into
   `main` with a merge commit, **before** or **together with** the ops
   split PR.
3. The user merges the ops split PR.

**Checks before the host pulls** (read-only; `origin/main` is fetched into
the host's remote-tracking ref only):

```bash
git -C /storage/streetzim fetch -q origin +refs/heads/main:refs/remotes/origin/main
git -C /storage/streetzim rev-parse --abbrev-ref '@{u}'
git -C /storage/streetzim merge-base --is-ancestor HEAD origin/main && echo "OK: main contains the host's commit" || echo "STOP: the host has commits main lacks"
git -C /storage/streetzim merge-base --is-ancestor bd09db94d89dca6b97b36cdbf153a46ea13ab228 origin/main && echo "OK: main contains bd09db9" || echo "STOP: bd09db9 is not on main"
git -C /storage/streetzim diff --stat bd09db94d89dca6b97b36cdbf153a46ea13ab228 origin/main
git -C /storage/streetzim cat-file -e origin/main:ops/in-place.txt && echo "OK: main has the split"
```

**Expect:** the upstream is `origin/main`, three `OK` lines, and the diff
lists nothing, or only files of the host's own commits
(`web/torrents/*.torrent`, `web/index.html`, `web/drive/*` stamps).
Anything else: STOP.

**When:** the pull needs an idle window: no `create_osm_zim` and no
`upload_validated.sh` running, and the region's torrent commit already
made. The best window is right after an upload finished (its torrent
commit is on `main` by then) and before the queue's next build starts; if
the queue starts the next build at once, ask the user to pause it. Then
run TESTING-STAGE1.md **step 1 and step 4** exactly as written there
(its step 4 checks for a running build itself, records
`$HOME/sz-before-stage1.txt` and `$HOME/sz-after-stage1.txt`, and must
print `OK: pulled to the split`), then its **step 5**. Its step 2 line
"the branch contains the host's commit" is replaced by the checks above.

**Rollback of this pull,** only if HEAD is still the commit in
`$HOME/sz-after-stage1.txt` and no build runs (this runbook uses no
`reset`; the old commit gets a branch of its own):
`git -C /storage/streetzim switch -q -c host-rollback-stage1 "$(cat "$HOME/sz-before-stage1.txt")"`, then ask.

**Moving to `next` changes the public site at the next deploy.** `next`
carries a new `/drive/` viewer (`web/drive/viewer/`), and every upload
runs `web/generate.py --deploy` from the checkout. `builder` changes no
`web/` file beyond the host's own commits. The user decides when `next`'s
viewer may go live.

**Moving to `next` while the round's queue runs:** a running
`build-refresh-queue.sh` keeps executing its old copy, which calls
`download_overture_data.py` without `--transport`. On `next` that
downloader defaults to `https` (untested on the host; §2b tests both).
The old copy does pass `OVERTURE_RELEASE`, so `build-region-fast.sh`'s new
refusal doesn't trip. Tell the user; restarting the queue is their call.

### 1.1 Look (read-only)

```bash
git -C /storage/streetzim rev-parse HEAD
git -C /storage/streetzim rev-parse --abbrev-ref HEAD
git -C /storage/streetzim rev-parse --abbrev-ref '@{u}'
git -C /storage/streetzim --no-optional-locks status -sb
git -C /storage/streetzim rev-list --left-right --count '@{u}...HEAD'
git -C /storage/streetzim log --oneline '@{u}..HEAD'
pgrep -af 'build-refresh-queue|ship-region|extract-region-pbfs|rebuild_old_regions|run-continent-chain|create_osm_zim|upload_validated'
for p in $(pgrep -f '[b]uild-refresh-queue'); do tr '\0' '\n' < /proc/$p/environ | grep -E '^(OVERTURE_|WORLD_|PLANET=|TIER_A=|RESULTS=|REGISTRY=)'; done
cat /storage/streetzim/queue-refresh.release 2>/dev/null || echo "no queue-refresh.release (expected before next)"
grep -h 'refresh queue start' /storage/streetzim/queue-refresh-*.log | tail -2
crontab -l
grep -l -e build-region-fast -e create_osm_zim_leaflet -e upgrade_spatial_zim /storage/streetzim/.*.sh 2>/dev/null
df -h /storage /storage/streetzim/tmp /tmp
bash /storage/streetzim/ops/check_stage1.sh --root /storage/streetzim --python /storage/streetzim/venv-linux/bin/python3
```

**Record:** the commit and branch (a detached `HEAD`: STOP), the upstream,
the "ahead" count and the host's own commits (torrents and site stamps
from `upload_validated.sh`), running scripts and the queue's environment,
crontab lines, and the checker's output (expect `ALL CHECKS PASSED`; its
step 5 is `tools/check_boundary.py`).

- **Ahead is not 0:** the host has commits the branches lack. The move
  will STOP until the user has them on `origin` and merged into `builder`
  and `next`. Don't push them yourself.
- **Host scripts that call `build-region-fast.sh`** without
  `OVERTURE_RELEASE` will now refuse to start (before, they defaulted to the
  deleted 2026-04-15.0 and silently lost Overture). List them for the user.
  `next` deletes `create_osm_zim_leaflet.py` and
  `cloud/upgrade_spatial_zim.py`; any host script or cron line naming them:
  report.

### 1.2 The test clone and the branch facts (no change to production)

```bash
mkdir -p /storage/sz-tests/tmp /storage/sz-tests/results
git clone -q /storage/streetzim /storage/sz-tests/next
git -C /storage/sz-tests/next fetch -q "$(git -C /storage/streetzim remote get-url origin)" +refs/heads/main:refs/remotes/gh/main +refs/heads/claude/adoring-dijkstra-i2vge7:refs/remotes/gh/ops +refs/heads/claude/adoring-dijkstra-i2vge7-builder:refs/remotes/gh/builder +refs/heads/claude/adoring-dijkstra-i2vge7-next:refs/remotes/gh/next
git -C /storage/sz-tests/next switch -q --detach gh/next
git -C /storage/sz-tests/next log -1 --format='%H %s'
for p in "gh/main gh/ops" "gh/ops gh/builder" "gh/builder gh/next" "origin/HEAD gh/builder" "origin/HEAD gh/next"; do set -- $p; git -C /storage/sz-tests/next merge-base --is-ancestor "$1" "$2" && echo "OK    $1 is in $2" || echo "STOP  $1 is not in $2"; done
```

(`origin/HEAD` in the clone is the host's current commit.) To refresh the
clone later, run the `fetch` and `switch` lines again.

**Expect** all five `OK`. **Known on 29 September:** `builder` and `next`
were cut before `bd09db9` (their base is `5eb8a4f`), so `gh/ops
gh/builder` prints `STOP`, and after §1.0 so do both `origin/HEAD` lines.
The branch owner must merge the new `main` into `builder`, then `builder`
into `next`. The conflicts are the three
generated stamp files (`web/drive/build-info.js`, `web/drive/sw.js`,
`web/drive/viewer/.version`): regenerate them with
`scripts/sync-drive-viewer.sh` on the merged tree. Until then, run §1.3
and the clone-only tests (§2a, 2b, 2d–2g), and don't move production.

### 1.3 Checks in the clone

```bash
bash /storage/sz-tests/next/ops/check_stage1.sh --root /storage/sz-tests/next --python /storage/streetzim/venv-linux/bin/python3
/storage/streetzim/venv-linux/bin/python3 -I /storage/sz-tests/next/tools/check_boundary.py --root /storage/sz-tests/next
/storage/streetzim/venv-linux/bin/python3 -m pytest -q -p no:cacheprovider /storage/sz-tests/next/ops/tests /storage/sz-tests/next/tests/test_check_boundary.py
```

**Pass:** `ALL CHECKS PASSED` (a warning that the detached clone has no
upstream is fine), the boundary check's last line reports no violation,
pytest passes (if pytest is missing from the host venv, say so and skip
that line).

### 1.4 The move (one block per move; the only steps that change the host)

Set the three lines at the top for the move, and run the block as one
command:

| move | `T` (remote branch) | `L` (new local branch) | `R` (records) |
|---|---|---|---|
| to builder | `claude/adoring-dijkstra-i2vge7-builder` | `host-builder` | `$HOME/sz-move-builder` |
| to next | `claude/adoring-dijkstra-i2vge7-next` | `host-next` | `$HOME/sz-move-next` |
| to main (§3) | `main` | `host-main` | `$HOME/sz-move-main` |

```bash
T=claude/adoring-dijkstra-i2vge7-builder
L=host-builder
R=$HOME/sz-move-builder
G="git -C /storage/streetzim"
if ! command -v pgrep >/dev/null; then
  echo "STOP: pgrep is missing, so a running build can't be detected; ask"
elif pgrep -f '[c]reate_osm_zim|[u]pload_validated' >/dev/null; then
  echo "STOP: a build or upload is running; move between builds"
elif [ -e "$R.before" ]; then
  echo "STOP: this move was started before:"; cat "$R.before" "$R.after" 2>/dev/null
elif [ "$($G rev-parse --abbrev-ref HEAD)" = HEAD ]; then
  echo "STOP: detached HEAD; ask"
elif $G rev-parse -q --verify "refs/heads/$L" >/dev/null; then
  echo "STOP: local branch $L already exists; ask"
elif ! $G fetch -q origin "+refs/heads/$T:refs/remotes/origin/$T"; then
  echo "STOP: fetch failed; ask"
elif ! $G merge-base --is-ancestor HEAD "refs/remotes/origin/$T"; then
  echo "STOP: $T lacks these host commits; ask:"; $G log --oneline "refs/remotes/origin/$T..HEAD" | head -20
else
  { $G rev-parse --abbrev-ref HEAD && $G rev-parse HEAD; } > "$R.before.new" && mv "$R.before.new" "$R.before" &&
  $G switch -q -c "$L" &&
  $G branch -q --set-upstream-to="origin/$T" "$L" &&
  $G -c merge.autoStash=false -c rebase.autoStash=false pull -q --ff-only --no-rebase &&
  $G rev-parse HEAD > "$R.after" &&
  if [ "$(cat "$R.after")" = "$($G rev-parse "refs/remotes/origin/$T")" ]; then echo "OK: on $L at $T ($(cat "$R.after"))"
  else echo "STOP: pulled, but not to $T's commit; don't run this block again; ask"; fi ||
  echo "STOP: the move did not finish; run: $G log -1; $G status -sb; ask"
fi
```

`switch -c` creates `$L` at the current commit, so nothing on disk changes
until the pull, and the old branch is left untouched for rollback. The pull
refuses if it isn't a fast-forward or would overwrite a local edit; the
host-edited lists are identical on every branch here, so their edits carry
over.

### 1.5 After each move (read-only)

```bash
bash /storage/streetzim/ops/check_stage1.sh --root /storage/streetzim --python /storage/streetzim/venv-linux/bin/python3
git -C /storage/streetzim --no-optional-locks status -sb
ps -eo pid,lstart,args | grep -E '/storage/streetzim/.*\.(sh|py|mjs)' | grep -v grep
/storage/streetzim/venv-linux/bin/python3 /storage/streetzim/create_osm_zim.py --help > /dev/null && echo "OK: builder imports"
/storage/streetzim/venv-linux/bin/python3 /storage/streetzim/cloud/serve_zims.py --help > /dev/null && echo "OK: ops python by old path"
/storage/streetzim/venv-linux/bin/python3 -c 'import regex' 2>/dev/null && echo "regex present" || echo "regex missing (only the streetzim command's metadata flags need it; installing requirements-ops.txt is the user's call)"
```

**Pass:** `ALL CHECKS PASSED` (including "no local commits" and the
boundary check), the same `M` lines on the host-edited lists as in §1.1,
the §1.1 PIDs still running, both `OK` lines. Then run §2c on this branch.

### 1.6 Later commits on the same branch

When `next` gains the topic branches, update `host-next` by pull, between
builds:

```bash
R=$HOME/sz-pull-next-$(date +%Y%m%d-%H%M)
G="git -C /storage/streetzim"
if pgrep -f '[c]reate_osm_zim|[u]pload_validated' >/dev/null; then echo "STOP: a build or upload is running"
elif [ "$($G rev-parse --abbrev-ref HEAD)" != host-next ]; then echo "STOP: not on host-next"
else
  $G rev-parse HEAD > "$R.before" &&
  $G -c merge.autoStash=false -c rebase.autoStash=false pull -q --ff-only --no-rebase &&
  $G rev-parse HEAD > "$R.after" && echo "OK: $(cat "$R.before") -> $(cat "$R.after") (records $R.*)" ||
  echo "STOP: the pull refused (host commits, or a local edit in the way); nothing changed; ask"
fi
```

Then §1.5 again. To go back from such a pull, only if HEAD is still
`$R.after`: `git -C /storage/streetzim switch -q -c host-rollback-<date> <the .before commit>`,
then ask.

### 1.7 Rollback of a move

Only if HEAD is still the commit the move reached (a host commit since,
such as a torrent, makes the block stop: switching back would take that
file off the disk).

```bash
R=$HOME/sz-move-builder
G="git -C /storage/streetzim"
if [ -s "$R.before" ] && [ -s "$R.after" ] &&
   [ "$($G rev-parse HEAD)" = "$(cat "$R.after")" ] &&
   ! pgrep -f '[c]reate_osm_zim|[u]pload_validated' >/dev/null; then
  $G switch -q "$(sed -n 1p "$R.before")" &&
  [ "$($G rev-parse HEAD)" = "$(sed -n 2p "$R.before")" ] &&
  echo "ROLLED BACK to $(sed -n 1p "$R.before") $(sed -n 2p "$R.before")" ||
  echo "STOP: the switch refused or landed elsewhere; ask"
else
  echo "STOP: HEAD moved since the move, a build is running, or the records are missing; ask"
fi
```

`switch` keeps local edits (the host-edited lists) and refuses if one
would be overwritten. The new local branch stays; don't delete it. Then
§1.5, and report.

## 2. Tests on the host

Results go to `/storage/sz-tests/results/<test>/`. Each test lists what
it needs; §2c changes the production checkout's data directory only by
the files it names.

### 2.0 Preparation (once)

The StreetZim image, from the clone's current `next` (for §2a tilemaker
mode, §2f, §2g); record its commit and ID:

```bash
docker build -q -t streetzim:hosttest /storage/sz-tests/next > /storage/sz-tests/results/image-id.txt
git -C /storage/sz-tests/next rev-parse HEAD >> /storage/sz-tests/results/image-id.txt
docker run --rm streetzim:hosttest tilemaker --help 2>&1 | head -2
```

Rebuild it after refreshing the clone (§1.2), and note the new commit.

### 2a. Golden builds, Monaco ([docs/golden-builds.md](../docs/golden-builds.md))

OpenFreeMap tiles (the strict mode). Each run is three Monaco builds,
minutes in all:

```bash
TMPDIR=/storage/sz-tests/tmp PYTHON=/storage/streetzim/venv-linux/bin/python3 bash /storage/sz-tests/next/tools/golden_builds.sh gh/main gh/ops /storage/sz-tests/golden/main-ops > /storage/sz-tests/results/golden-main-ops.txt 2>&1; echo "exit $?"
TMPDIR=/storage/sz-tests/tmp PYTHON=/storage/streetzim/venv-linux/bin/python3 bash /storage/sz-tests/next/tools/golden_builds.sh gh/main gh/builder /storage/sz-tests/golden/main-builder > /storage/sz-tests/results/golden-main-builder.txt 2>&1; echo "exit $?"
TMPDIR=/storage/sz-tests/tmp PYTHON=/storage/streetzim/venv-linux/bin/python3 bash /storage/sz-tests/next/tools/golden_builds.sh gh/main gh/next /storage/sz-tests/golden/main-next > /storage/sz-tests/results/golden-main-next.txt 2>&1; echo "exit $?"
TMPDIR=/storage/sz-tests/tmp PYTHON=/storage/streetzim/venv-linux/bin/python3 bash /storage/sz-tests/next/tools/golden_builds.sh gh/next gh/next /storage/sz-tests/golden/next-next > /storage/sz-tests/results/golden-next-next.txt 2>&1; echo "exit $?"
```

**Expected** (measured in a sandbox on 29 September with the same
commits and Monaco inputs):

| pair | exit | `changed` | `only-before` / `only-after` |
|---|---|---|---|
| main → ops | 0 | 0 | 0 / 0 (the ops split changes no build output) |
| main → builder | 1 | `C/index.html`, `M/License` (CC BY-SA 4.0) | 0 / 0 |
| main → next | 1 | `C/index.html`, `C/map-config.json` (adds `description`, `generator`, `rtlTextPlugin`, `title`), `C/places.html`, `C/routing-worker.js`, `M/License`, `M/Counter` | 0 / `C/mapbox-gl-rtl-text.js` |
| next → next | 0 | 0 | 0 / 0 |

`reordered` varies between runs (0–38 seen) and is fine. **FAIL:** any
other `changed`, `only-before` or `only-after` path, or `noise` above 0.
Look at each such entry as the golden-builds page describes, and report
it. After a topic branch lands on `next`, rerun `main → next`: a new
difference must belong to that branch (the viewer polish touches
`C/index.html` and `C/places.html` only).

**tilemaker mode.** The host has no tilemaker binary, so use the image's
(v3.0.0) through a wrapper. Everything tilemaker reads must be under
`/storage/sz-tests`, hence `TMPDIR` there:

```bash
mkdir -p /storage/sz-tests/bin /storage/sz-tests/golden/tm-main-next/inputs
printf '%s\n' '#!/bin/sh' 'exec docker run --rm --user "$(id -u):$(id -g)" -v /storage/sz-tests:/storage/sz-tests -w "$PWD" streetzim:hosttest tilemaker "$@"' > /storage/sz-tests/bin/tilemaker
chmod +x /storage/sz-tests/bin/tilemaker
bash /storage/sz-tests/next/scripts/fetch-shapefiles.sh /storage/sz-tests/golden/tm-main-next/inputs
PATH=/storage/sz-tests/bin:$PATH TMPDIR=/storage/sz-tests/tmp PYTHON=/storage/streetzim/venv-linux/bin/python3 bash /storage/sz-tests/next/tools/golden_builds.sh --tilemaker gh/main gh/next /storage/sz-tests/golden/tm-main-next > /storage/sz-tests/results/golden-tm-main-next.txt 2>&1; echo "exit $?"
```

**Expected:** exit 1 with the same `changed` and `only-after` set as the
OpenFreeMap run, plus any number of `tiles-equal` and `moved` (largest
shift at most 0.0001°). `next` doesn't change the tilemaker profile, so a
tile in `changed`: FAIL. (tilemaker mode could not run in the sandbox,
whose tilemaker is 2.x.)

### 2b. Overture over `s3://` at the pinned release

The first run of `next`'s downloader with `--transport s3` on the host,
into test files, compared with the round's cached parquets. Uses D.C.;
if `overture_cache/` has no D.C. file for 2026-08-19.0, use the smallest
region that has one (`ls -S -r /storage/streetzim/overture_cache/places-*-2026-08-19.0.parquet | head -3`)
with its bbox from `cloud/regions.tsv`, and say so.

```bash
mkdir -p /storage/sz-tests/results/2b
cat /storage/streetzim/overture_cache/places-washington-dc-2026-08-19.0.parquet.bbox 2>/dev/null
for t in addresses places; do /storage/streetzim/venv-linux/bin/python3 /storage/sz-tests/next/download_overture_data.py $t --bbox=-77.12,38.79,-76.91,38.99 --release 2026-08-19.0 --transport s3 --out /storage/sz-tests/results/2b/$t-s3.parquet > /storage/sz-tests/results/2b/$t-s3.log 2>&1; echo "$t exit $?"; tail -1 /storage/sz-tests/results/2b/$t-s3.log; done
for t in addresses places; do /storage/streetzim/venv-linux/bin/python3 /storage/sz-tests/next/download_overture_data.py $t --bbox=-77.12,38.79,-76.91,38.99 --release 2026-08-19.0 --transport https --out /storage/sz-tests/results/2b/$t-https.parquet > /storage/sz-tests/results/2b/$t-https.log 2>&1; echo "$t exit $?"; tail -1 /storage/sz-tests/results/2b/$t-https.log; done
```

Compare (ids both ways, rows, columns, the release stamp):

```bash
/storage/streetzim/venv-linux/bin/python3 - <<'EOF' | tee /storage/sz-tests/results/2b/compare.txt
import os, sys
sys.path.insert(0, "/storage/sz-tests/next")
import duckdb
from streetzim.overture import parquet_release
d = "/storage/sz-tests/results/2b"
c = duckdb.connect()
def one(q): return c.execute(q).fetchone()[0]
def cols(p): return {r[0] for r in c.execute(f"DESCRIBE SELECT * FROM '{p}'").fetchall()}
for t in ("addresses", "places"):
    s3, https = f"{d}/{t}-s3.parquet", f"{d}/{t}-https.parquet"
    old = f"/storage/streetzim/overture_cache/{t}-washington-dc-2026-08-19.0.parquet"
    print(t, "release stamp:", parquet_release(s3), "| columns:", sorted(cols(s3)))
    for name, other in (("https", https), ("cache", old)):
        if not os.path.exists(other):
            print(f"  vs {name}: missing"); continue
        print(f"  vs {name}: rows {one(f'SELECT count(*) FROM {chr(39)}{s3}{chr(39)}')} / {one(f'SELECT count(*) FROM {chr(39)}{other}{chr(39)}')},",
              f"ids only in s3 {one(f'SELECT count(*) FROM (SELECT id FROM {chr(39)}{s3}{chr(39)} EXCEPT SELECT id FROM {chr(39)}{other}{chr(39)})')},",
              f"only in {name} {one(f'SELECT count(*) FROM (SELECT id FROM {chr(39)}{other}{chr(39)} EXCEPT SELECT id FROM {chr(39)}{s3}{chr(39)})')},",
              f"columns only in s3 {sorted(cols(s3) - cols(other))}, only in {name} {sorted(cols(other) - cols(s3))}")
EOF
```

**Pass:** all four downloads exit 0, and each log's last line names
`release 2026-08-19.0`; the release stamp is `2026-08-19.0`; against the
https files and the cache, the same row count and 0 ids only on either
side. Columns: `places` from `next` carries `categories`, `taxonomy` and
`basic_category` (2026-08-19.0 has all three); a cache file written by the
old downloader lacks the last two. That, and nothing else, is expected.
**FAIL:** a failed s3 download (report the log: this is the transport the
wrappers use), or any id difference (first check the cache file's `.bbox`
sidecar printed above: another bbox explains it).

### 2c. A small region end to end, without uploading (production checkout)

Runs after each move (§1.5), on the branch the host is then on. It runs
the production wrappers, which `cd /storage/streetzim` and use its code,
caches and `overture_cache/`.

**What keeps a test from publishing:**

| script | uploads or deploys? | test flags |
|---|---|---|
| `build-region-fast.sh` | never; writes `osm-<id>-<date>.zim` in `/storage/streetzim` | needs `OVERTURE_RELEASE` (refuses without it, and refuses `latest`) |
| `ship-region.sh <id> --no-upload` | not with `--no-upload` (must be the second argument); it stops after the gates. Without it: `cloud/upload_validated.sh` (archive.org, torrent commit, `web/generate.py --deploy`) | |
| `build-refresh-queue.sh` | unless `--no-upload` | `--dry-run` builds nothing but still takes the queue lock and appends to `queue-refresh-<date>.log`. With `--no-upload` a good region is recorded `built-ok`, which `--continue` then skips: never run a test against the round's `queue-refresh.tsv` (set `RESULTS=/storage/sz-tests/...`), and not while the round runs |

Use `ship-region.sh` on D.C.:

```bash
ls -l /storage/streetzim/osm-washington-dc-$(date +%F).zim 2>/dev/null && echo "STOP: today's D.C. ZIM exists; the wrapper would reuse it" || echo "OK: no D.C. ZIM today"
pgrep -af '[c]reate_osm_zim|[s]hip-region' || echo "OK: idle"
cd /storage/streetzim && OVERTURE_RELEASE=2026-08-19.0 STAGE_MBTILES_NVME=0 ./ship-region.sh washington-dc --no-upload; echo "exit $?"
```

(`STAGE_MBTILES_NVME=0`: D.C.'s `.mbtiles` is a symlink to the world tile
file, which the wrapper would otherwise copy to the shared NVMe.)

**Pass:** exit 0; `ship-washington-dc-<date>.log` ends with `=== all gates
passed` and `--no-upload: stopping here with osm-washington-dc-<date>.zim`;
`washington-dc-build.out` shows `overture release: 2026-08-19.0
(addr=yes places=yes)` and `wikipedia: bundling articles`; the build used
the Rust packer (`--zim-builder=rust`, a to-do in head-to-head-dc.md);
no `archive.org` line in the log. **FAIL:** anything else; keep the logs.

Then take the test output out of production's way (a later queue run
would otherwise reuse today's ZIM) and free its scratch:

```bash
mkdir -p /storage/sz-tests/results/2c-$(git -C /storage/streetzim rev-parse --short HEAD)
mv /storage/streetzim/osm-washington-dc-$(date +%F).zim /storage/sz-tests/results/2c-$(git -C /storage/streetzim rev-parse --short HEAD)/
cp /storage/streetzim/ship-washington-dc-$(date +%F).log /storage/streetzim/washington-dc-build.out /storage/streetzim/washington-dc-rebuild-$(date +%F).log /storage/streetzim/washington-dc-smoke-$(date +%F).log /storage/sz-tests/results/2c-$(git -C /storage/streetzim rev-parse --short HEAD)/
grep -a '^Temp files kept at: ' /storage/streetzim/washington-dc-rebuild-$(date +%F).log | tail -1
```

Delete the directory that last line names, only if it is under
`/storage/streetzim/tmp/osm_zim_` (`rm -rf <that directory>`). If the
build crossed midnight, use the date in the ZIM's name instead of
`$(date +%F)`.

### 2d. D.C. with offline Wikipedia, `main` vs `next`

**After `next` contains `topic-wiki-429`** (check in the table at the top).
The comparison [head-to-head-dc.md](../docs/head-to-head-dc.md#what-is-still-not-compared)
asks for: images and the offline-ZIM path, from production's Wikipedia
ZIM. Both sides read the same PBF, the same tiles (the host has no
tilemaker, so `--mbtiles`) and the same warm caches, copied under
`/storage/sz-tests`.

```bash
test "$(stat -c%s /storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim)" = 123980647016 && echo "OK: wiki ZIM" || echo "STOP: wiki ZIM missing or wrong size"
mkdir -p /storage/sz-tests/dcwiki/src-main /storage/sz-tests/dcwiki/main /storage/sz-tests/dcwiki/next
git -C /storage/sz-tests/next archive gh/main | tar -x -C /storage/sz-tests/dcwiki/src-main
du -sh /storage/streetzim/wikidata_cache
cp -a /storage/streetzim/wikidata_cache /storage/sz-tests/dcwiki/wd
cp /storage/streetzim/wiki_articles_cache/washington-dc_qid_titles.json /storage/sz-tests/dcwiki/titles.json 2>/dev/null || echo "no D.C. title cache; starts cold"
```

(If the Wikidata cache is too large to copy, stop and ask.) Warm the
caches with `next` (which no longer records a 429 as a miss). Run this
until two runs in a row print the same `distinct titles` count and the
log has no `429`:

```bash
cd /storage/sz-tests/dcwiki/next && TMPDIR=/storage/sz-tests/tmp STREETZIM_CACHE_DIR=/storage/sz-tests/dcwiki/cache /storage/streetzim/venv-linux/bin/python3 /storage/sz-tests/next/create_osm_zim.py --pbf /storage/streetzim/world-data/regions/washington-dc.osm.pbf --mbtiles /storage/streetzim/world-data/regions/washington-dc.mbtiles --bbox=-77.12,38.79,-76.91,38.99 --name "Washington, D.C." --wikidata --wikidata-cache /storage/sz-tests/dcwiki/wd --resolve-wikidata-titles --wikidata-title-cache /storage/sz-tests/dcwiki/titles.json --bundle-wiki-articles --wiki-articles-source /storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim --wiki-images all --wiki-image-max-kb 128 -o /storage/sz-tests/dcwiki/next/dc-next.zim > /storage/sz-tests/dcwiki/next/build.log 2>&1; echo "exit $?"; grep -a -e 'distinct titles' -e 'stored' -e 429 /storage/sz-tests/dcwiki/next/build.log | tail -5
```

Then `main`, same flags and caches, from its exported tree:

```bash
cd /storage/sz-tests/dcwiki/main && TMPDIR=/storage/sz-tests/tmp STREETZIM_CACHE_DIR=/storage/sz-tests/dcwiki/cache /storage/streetzim/venv-linux/bin/python3 /storage/sz-tests/dcwiki/src-main/create_osm_zim.py --pbf /storage/streetzim/world-data/regions/washington-dc.osm.pbf --mbtiles /storage/streetzim/world-data/regions/washington-dc.mbtiles --bbox=-77.12,38.79,-76.91,38.99 --name "Washington, D.C." --wikidata --wikidata-cache /storage/sz-tests/dcwiki/wd --resolve-wikidata-titles --wikidata-title-cache /storage/sz-tests/dcwiki/titles.json --bundle-wiki-articles --wiki-articles-source /storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim --wiki-images all --wiki-image-max-kb 128 -o /storage/sz-tests/dcwiki/main/dc-main.zim > /storage/sz-tests/dcwiki/main/build.log 2>&1; echo "exit $?"; grep -a -e 'distinct titles' -e 'stored' -e 429 /storage/sz-tests/dcwiki/main/build.log | tail -5
```

If `main`'s log shows any new fetch or `429`, its inputs differ: run the
`next` build once more, then `main` again, and compare only a pair with no
fetches. Compare:

```bash
/storage/streetzim/venv-linux/bin/python3 /storage/sz-tests/next/tools/golden_diff.py /storage/sz-tests/dcwiki/main/dc-main.zim /storage/sz-tests/dcwiki/next/dc-next.zim --show 200 > /storage/sz-tests/results/dcwiki-diff.txt; echo "exit $?"
/storage/streetzim/venv-linux/bin/python3 - /storage/sz-tests/dcwiki/main/dc-main.zim /storage/sz-tests/dcwiki/next/dc-next.zim $(ls -t /storage/streetzim/osm-washington-dc-20*.zim 2>/dev/null | head -1) <<'EOF' | tee /storage/sz-tests/results/dcwiki-counts.txt
import sys
from collections import Counter
from libzim.reader import Archive
for f in sys.argv[1:]:
    a = Archive(f); n = Counter()
    for i in range(a.all_entry_count):
        p = a._get_entry_by_id(i).path
        for pre in ("wiki-article/", "wiki-image/"):
            if pre in p: n[pre] += 1
    print(f, dict(n), "geo-index" if a.has_entry_by_path("wiki-geo-index.json") else "no geo-index")
EOF
```

**Pass:** no `wiki-article/` or `wiki-image/` path in `changed`,
`only-before` or `only-after` of the diff; the other differences are the
ones §2a lists (plus `streetzim-meta.json` if its counts differ, which
must then be explained). Counts close to the published ZIM's 1,551
articles and 5,065 images (the same source ZIM); the published file is
the third line, if the host has it. **FAIL:** any article or image that
differs; report the paths.

### 2e. Alaska across the antimeridian (no upload, no production files)

Row `alaska 172.0,51.0,-130.0,72.0`. The host's `world-data/regions/alaska.*`
files were cut for the old bbox; this test doesn't touch them (the real
rebuild follows [docs/alaska-antimeridian-runbook.md](docs/alaska-antimeridian-runbook.md),
with the round's owner). It extracts its own PBF from the planet and reads
the queue's world tiles. Write that path once (the queue's
`WORLD_MBTILES`, as §1.1 printed it):

```bash
mkdir -p /storage/sz-tests/alaska
for p in $(pgrep -f '[b]uild-refresh-queue'); do tr '\0' '\n' < /proc/$p/environ | sed -n 's/^WORLD_MBTILES=//p'; done | head -1 > /storage/sz-tests/alaska/WORLD_MBTILES.txt
test -s "$(cat /storage/sz-tests/alaska/WORLD_MBTILES.txt)" && cat /storage/sz-tests/alaska/WORLD_MBTILES.txt || echo "STOP: no running queue to read WORLD_MBTILES from; ask the user for the path and write it to that file"
cd /storage/sz-tests/next && /storage/streetzim/venv-linux/bin/python3 -m streetzim.area poly 172.0,51.0,-130.0,72.0 /storage/sz-tests/alaska/alaska.poly
osmium extract -p /storage/sz-tests/alaska/alaska.poly /storage/streetzim/world-data/planet-2026-08-31.osm.pbf -o /storage/sz-tests/alaska/alaska.osm.pbf --overwrite
osmium fileinfo -e /storage/sz-tests/alaska/alaska.osm.pbf | grep -A1 'Bounding box'
cd /storage/sz-tests/alaska && TMPDIR=/storage/sz-tests/tmp STREETZIM_CACHE_DIR=/storage/sz-tests/alaska/cache /storage/streetzim/venv-linux/bin/python3 /storage/sz-tests/next/create_osm_zim.py --pbf /storage/sz-tests/alaska/alaska.osm.pbf --mbtiles "$(cat /storage/sz-tests/alaska/WORLD_MBTILES.txt)" --bbox=172.0,51.0,-130.0,72.0 --name Alaska --routing --spatial-chunk-scale 10 --split-find-chips -o /storage/sz-tests/alaska/alaska.zim > /storage/sz-tests/alaska/build.log 2>&1; echo "exit $?"
grep -a -e 'antimeridian' -e 'opening centre' /storage/sz-tests/alaska/build.log | head
```

Check the western Aleutians (172E–180: Attu, Shemya, Kiska), against the
last published Alaska ZIM if the host has one:

```bash
/storage/streetzim/venv-linux/bin/python3 - /storage/sz-tests/alaska/alaska.zim $(ls -t /storage/streetzim/osm-alaska-20*.zim 2>/dev/null | head -1) <<'EOF' | tee /storage/sz-tests/results/alaska.txt
import sys
from libzim.reader import Archive
from libzim.search import Query, Searcher
for f in sys.argv[1:]:
    a = Archive(f)
    n = sum(a.has_entry_by_path(f"tiles/10/{x}/{y}.pbf") for x in range(1001, 1024) for y in range(320, 350))
    hits = {q: Searcher(a).search(Query().set_query(q)).getEstimatedMatches() for q in ("Attu", "Shemya", "Kiska", "Anchorage")}
    print(f, "z10 tiles 172E-180:", n, hits)
EOF
```

**Pass:** exit 0; the PBF's box runs from -180 to 180; the log has
`Joined N road(s) split at the antimeridian`; the new ZIM has z10 tiles
east of 172E and search hits for Attu, Shemya and Kiska (and Anchorage);
the old published ZIM, if present, has none east of 172E. **FAIL:**
no tiles or no hits there.

### 2f. Massachusetts, measured ([docs/zimfarm.md](../docs/zimfarm.md#what-a-build-costs))

The sandbox run stopped at 2 GB free disk. Needs about 30 GB free on
`/storage` (`df -h /storage`); default profile, inside the image as the
Luxembourg Docker row was measured:

```bash
mkdir -p /storage/sz-tests/ma/in
curl -fL --retry 3 -o /storage/sz-tests/ma/in/massachusetts-latest.osm.pbf https://download.geofabrik.de/north-america/us/massachusetts-latest.osm.pbf
docker run --rm --user "$(id -u):$(id -g)" -e STREETZIM_CACHE_DIR=/run/cache -v /storage/sz-tests/ma:/run streetzim:hosttest python /app/tools/measure_build.py --json /run/measure.json --watch /run/tmp --watch /run/out --log /run/build.log -- streetzim --name osm_en_massachusetts --title Massachusetts --description "Offline map of Massachusetts with search and routing" --include-poly https://download.geofabrik.de/north-america/us/massachusetts.poly --pbf-url file:///run/in/massachusetts-latest.osm.pbf --output /run/out --tmp /run/tmp --dl /run/dl --stats-filename /run/out/task_progress.json; echo "exit $?"
cat /proc/loadavg; cat /storage/sz-tests/ma/measure.json | head -12
```

**Pass:** exit 0, a ZIM in `/storage/sz-tests/ma/out/`, and
`measure.json` with `wall_s`, `cpu_s`, `peak_pss_gb`, `peak_disk_gb`.
Report them with the extract size, the ZIM size, `nproc`, and whether a
production build overlapped (loadavg; then the wall time is an upper
bound). Copy `measure.json` and `build.log` to `/storage/sz-tests/results/ma/`.

### 2g. Planet-scale head-to-head: maps2zim 0.2.1 vs StreetZim on the same tiles

A first-class test: the host has the disk and memory the sandbox lacked.
It follows the D.C. comparison plan (v2): the same inputs for both tools,
the same measurement, n ≥ 2, and a pre-registered reporting rule. It
doesn't gate the merges (§3). Budget: 3 regions × 4 variants × 2 runs =
24 runs, maps2zim an hour or more each; a day or more in all. Run region by
region and report as each finishes.

**Variants:**

| id | what | needs |
|---|---|---|
| `maps2zim` | `ghcr.io/openzim/maps:0.2.1`, unmodified, with its own planet path (`/tmp/dl/planet.mbtiles`, its default download folder) seeded with the shared planet file | — |
| `sz-basic` | StreetZim, default profile, tiles from the same planet file (`--mbtiles-url file://…`) | `next` contains `topic-mbtiles-flag` |
| `sz-full` | as `sz-basic`, with the full profile (terrain etc.) | `next` contains the `--profile` branch |
| `sz-tm` | StreetZim, default profile, its own tilemaker tiles from the Geofabrik extract | — |

Check the two gates in the table at the top of this page, then refresh
the clone and rebuild the image (§1.2, §2.0), and check the flags' final
spelling: `docker run --rm streetzim:hosttest streetzim --help | grep -E -A3 'mbtiles|profile'`.
If `--profile` takes its value differently, change the one line marked
`PROFILE` in the runner below, and say so.

**Regions** (Geofabrik polys, as openZIM's recipes use): Luxembourg
(`europe/luxembourg.poly`), D.C. (`north-america/us/district-of-columbia.poly`)
and Switzerland (`europe/switzerland.poly`: the mid-size country, with a
production maps2zim task of 72.4 min).

**Fairness and measurement** (the plan's §3, §6 and §8):
- one planet file for every tile-reading run, and the same container
  limits for all (`--memory 16g --memory-swap 16g --cpu-shares 3072`, no
  CPU quota: a quota penalises the multi-process tools);
- the same measurement for all: `tools/measure_build.py` inside the
  container around the tool (wall, CPU, peak PSS and RSS of the process
  tree, peak disk of its folders), `docker stats` every 5 s, `docker
  inspect` (start, finish, `OOMKilled`), and the host's load
  (`/proc/loadavg`, `/proc/pressure/cpu`) at start and end;
- every run starts network-cold except for the planet file: a fresh
  download folder, so GeoNames (maps2zim), and the extract, shapefiles and
  DEM (StreetZim) download as they would on Zimfarm;
- the order within each round rotates (round 1: maps2zim, sz-basic,
  sz-full, sz-tm; round 2: the reverse);
- report the median, the [min–max] and every raw run. Say "A uses less X
  than B" only when the ranges don't overlap **and** the ratio of medians
  is at least 1.5×; otherwise "no clear difference". CPU time is the main
  speed figure; wall time is shown with the load;
- a run that overlapped a production build is marked `contended`; when a
  clean pair is available, it stays out of the headline;
- every table carries the coverage and feature notes: maps2zim keeps the
  tiles meeting the poly; `sz-basic` and `sz-full` cut the planet's tiles
  to the poly's bbox; `sz-tm` builds tiles from the extract; all
  StreetZim variants take search and routing from Geofabrik's extract,
  which is clipped to the poly. The features differ (StreetZim: search
  over every named feature, routing, a full-text index; maps2zim:
  sprites, the Natural Earth raster, GeoNames search);
- abort a run if `/storage` falls below 50 GB free, and void its round.

**The planet tiles, once.** First, what the host's world tiles are (the
queue's `WORLD_MBTILES`, written to a file in §2e). They come from
`build-world-tiles.sh`, which runs tilemaker, so they are expected *not*
to be an OpenFreeMap build:

```bash
/storage/streetzim/venv-linux/bin/python3 - "$(cat /storage/sz-tests/alaska/WORLD_MBTILES.txt)" <<'EOF'
import os, sqlite3, sys
p = sys.argv[1]
print(p, os.path.getsize(p), os.path.getmtime(p))
for k, v in sqlite3.connect(f"file:{p}?mode=ro", uri=True).execute("SELECT name, value FROM metadata WHERE name != 'json'"):
    print(" ", k, "=", v[:120])
EOF
```

Record its name, generator, version and date. Reuse it only if it is an
OpenFreeMap build (planetiler metadata, and the byte size of an entry in
OpenFreeMap's `files.txt`); then record that entry's build id. Otherwise
download OpenFreeMap's newest planet, the file maps2zim itself would fetch
(about 103 GB):

```bash
mkdir -p /storage/sz-tests/planet
curl -fsS https://btrfs.openfreemap.com/files.txt | grep -E '^areas/planet/[0-9]+_[^/]+/tiles\.mbtiles$' | sort | tail -1 | tee /storage/sz-tests/planet/SOURCE.txt
curl -fL -C - --retry 5 -o /storage/sz-tests/planet/planet.mbtiles "https://btrfs.openfreemap.com/$(head -1 /storage/sz-tests/planet/SOURCE.txt)"
ls -l /storage/sz-tests/planet/planet.mbtiles >> /storage/sz-tests/planet/SOURCE.txt
sha256sum /storage/sz-tests/planet/planet.mbtiles >> /storage/sz-tests/planet/SOURCE.txt
docker pull ghcr.io/openzim/maps:0.2.1
docker image inspect --format '{{json .RepoDigests}}' ghcr.io/openzim/maps:0.2.1 >> /storage/sz-tests/planet/SOURCE.txt
cat /storage/sz-tests/results/image-id.txt >> /storage/sz-tests/planet/SOURCE.txt
```

The build id is the `<timestamp>_<suffix>` on the first line of
`SOURCE.txt`. If the download restarted (`-C -`), check the final size
against a fresh `files.txt` listing; if the build rotated out meanwhile,
start over. If you reuse the world tiles instead, write their path, build
id and size into `SOURCE.txt`, and link them:
`ln -s <their path> /storage/sz-tests/planet/planet.mbtiles`.

**The runner,** written once:

```bash
cat > /storage/sz-tests/run-one.sh <<'EOF'
#!/usr/bin/env bash
# run-one.sh maps2zim|sz-basic|sz-full|sz-tm luxembourg|dc|switzerland N
set -u
tool=$1 region=$2 n=$3
PROFILE=(--profile full)   # the full profile's flag, as it landed on next
case $region in
  luxembourg)  poly=https://download.geofabrik.de/europe/luxembourg.poly; title=Luxembourg ;;
  dc)          poly=https://download.geofabrik.de/north-america/us/district-of-columbia.poly; title="District of Columbia" ;;
  switzerland) poly=https://download.geofabrik.de/europe/switzerland.poly; title=Switzerland ;;
  *) echo "unknown region"; exit 2 ;;
esac
d=/storage/sz-tests/results/planet/${region}__${tool}__$n
name=cmp-$region-$tool-$n
[ -e "$d" ] && { echo "exists: $d"; exit 2; }
mkdir -p "$d/out" "$d/tmp" "$d/dl"
host() { date -Is; cat /proc/loadavg /proc/pressure/cpu 2>/dev/null; pgrep -af '[c]reate_osm_zim' || echo "no production build"; df -h /storage | tail -1; }
host > "$d/host-start.txt"
lim=(-d --name "$name" --user "$(id -u):$(id -g)" --memory 16g --memory-swap 16g --cpu-shares 3072)
desc="Full map, including roads and landmarks"
case $tool in
  maps2zim)
    docker run "${lim[@]}" -v "$d/out:/output" -v "$d/tmp:/tmp" -v "$d/dl:/tmp/dl" \
      -v /storage/sz-tests/planet/planet.mbtiles:/tmp/dl/planet.mbtiles:ro \
      -v /storage/sz-tests/next/tools/measure_build.py:/measure_build.py:ro \
      ghcr.io/openzim/maps:0.2.1 python3 /measure_build.py --json /output/measure.json \
      --watch /tmp --log /output/build.log -- \
      maps2zim --name "maps_en_$region" --title "$title" --description "$desc" \
      --publisher openZIM --include-poly "$poly" --output /output \
      --stats-filename /output/task_progress.json ;;
  sz-basic|sz-full|sz-tm)
    extra=(--mbtiles-url file:///planet/planet.mbtiles)
    [ "$tool" = sz-full ] && extra+=("${PROFILE[@]}")
    [ "$tool" = sz-tm ] && extra=()
    docker run "${lim[@]}" -v "$d:/run" -e STREETZIM_CACHE_DIR=/run/tmp/cache \
      -v /storage/sz-tests/planet/planet.mbtiles:/planet/planet.mbtiles:ro \
      streetzim:hosttest python /app/tools/measure_build.py --json /run/measure.json \
      --watch /run/tmp --watch /run/dl --watch /run/out --log /run/build.log -- \
      streetzim --name "streetzim_en_$region" --title "$title" --description "$desc" \
      --include-poly "$poly" ${extra[@]+"${extra[@]}"} --output /run/out --tmp /run/tmp --dl /run/dl \
      --stats-filename /run/out/task_progress.json ;;
  *) echo "unknown tool"; exit 2 ;;
esac > "$d/container.id" || exit 1
while [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ]; do
  docker stats --no-stream --format '{{.CPUPerc}}\t{{.MemUsage}}\t{{.BlockIO}}\t{{.NetIO}}' "$name" | sed "s/^/$(date +%s)\t/"
  sleep 5
done > "$d/docker-stats.tsv"
docker wait "$name" > "$d/exit.txt"
docker inspect "$name" > "$d/inspect.json"
docker logs --timestamps "$name" > "$d/docker.log" 2>&1
docker rm "$name" > /dev/null
host > "$d/host-end.txt"
rm -rf "${d:?}/tmp" "${d:?}/dl"
echo "$d: exit $(cat "$d/exit.txt")"
EOF
```

maps2zim runs with its image's own paths (`--tmp /tmp`, `--dl /tmp/dl`,
`--output /output`), all mounted from the run folder; its measurement
watches `/tmp`, which holds its download folder. StreetZim's watches its
temp, download and output folders. Both count their downloads but not the
planet file (maps2zim reads it in place; StreetZim's cut of it lands in
its temp folder and is counted).

**Runs,** one at a time, per region (Luxembourg first, then `dc`, then
`switzerland`):

```bash
for t in maps2zim sz-basic sz-full sz-tm; do bash /storage/sz-tests/run-one.sh $t luxembourg 1; done
for t in sz-tm sz-full sz-basic maps2zim; do bash /storage/sz-tests/run-one.sh $t luxembourg 2; done
```

Until the `--profile` branch lands, leave `sz-full` out of the loops and
run it later in its own two rounds.

**Per-run checks** (a run failing one is a FAIL for that run; rerun it
once, within its round):
- exit 0, and a ZIM in `out/`;
- maps2zim's `build.log` shows `using mbtiles file already available at
  /tmp/dl/planet.mbtiles` (it used the shared planet, not a download). If
  it can't open the read-only file, drop `:ro` from that mount, say so,
  and rerun;
- the `sz-basic` and `sz-full` logs show `Cut to the area: N tiles`;
- the `sz-full` log shows the terrain being built; the `sz-tm` log shows
  tilemaker;
- `inspect.json` has `"OOMKilled": false`.

**The table:**

```bash
/storage/streetzim/venv-linux/bin/python3 - <<'EOF' | tee /storage/sz-tests/results/planet/TABLE.md
import glob, json, os, statistics
from collections import defaultdict
runs = defaultdict(list)
for d in sorted(glob.glob("/storage/sz-tests/results/planet/*__*__*")):
    region, tool, n = os.path.basename(d).split("__")
    m = {}
    for f in (f"{d}/measure.json", f"{d}/out/measure.json"):
        if os.path.exists(f):
            m = json.load(open(f))
    zims = glob.glob(f"{d}/out/*.zim")
    ends = [f"{d}/host-start.txt", f"{d}/host-end.txt"]
    busy = not all(os.path.exists(f) and "no production build" in open(f).read() for f in ends)
    runs[(region, tool)].append(dict(n=n, exit=m.get("exit_code"), wall=m.get("wall_s", 0) / 60,
        cpu=m.get("cpu_s", 0) / 60, pss=m.get("peak_pss_gb"), disk=m.get("peak_disk_gb"),
        zim=sum(os.path.getsize(z) for z in zims) / 1e6, busy=busy))
def cell(rs, k):
    v = [r[k] for r in rs if r[k] is not None]
    return f"{statistics.median(v):.1f} [{min(v):.1f}–{max(v):.1f}]" if v else "–"
print("| region | variant | runs (exit) | wall min | CPU min | peak PSS GB | peak disk GB | ZIM MB | contended |")
print("|---|---|---|---|---|---|---|---|---|")
for (region, tool), rs in sorted(runs.items()):
    print(f"| {region} | {tool} | {len(rs)} ({','.join(str(r['exit']) for r in rs)}) | {cell(rs, 'wall')} | "
          f"{cell(rs, 'cpu')} | {cell(rs, 'pss')} | {cell(rs, 'disk')} | {cell(rs, 'zim')} | {sum(r['busy'] for r in rs)} |")
print("\nRaw runs:")
for k, rs in sorted(runs.items()):
    for r in rs:
        print(k, r)
EOF
```

Under the table, write for each region and metric either "A uses less X
than B (ratio, ranges)" or "no clear difference", by the 1.5× rule; then
the coverage and feature notes, the planet build id, both image digests
and the StreetZim commit. Add the entry count of each ZIM, and zimcheck
where the host has it (`command -v zimcheck && zimcheck -A <zim>`).

**Pack for the user:**

```bash
cp /storage/sz-tests/planet/SOURCE.txt /storage/sz-tests/results/planet/
tar -C /storage/sz-tests -czf /storage/sz-tests/results-$(date +%F).tgz results
ls -l /storage/sz-tests/results-$(date +%F).tgz
```

(The ZIMs are in the tarball; if it is too large, add
`--exclude='*.zim'` and list the ZIM sizes instead.)

## 3. Merging to `main` (the user), and the host after each merge

Order: ops branch (§1.0) → `builder` → `next`. For each, the user:
1. opens a PR from the branch to `main`, after that branch contains the
   previous merge (`builder` must contain the ops branch's merge, `next`
   must contain `builder`'s; the branch owner merges `main` in first);
2. waits for CI to be green;
3. merges with **a merge commit** (not squash or rebase), so the commits
   the host is on stay ancestors of `main` and the host can fast-forward.

After each merge, on the host, between builds:
- the ops branch: the host pulls `main` as §1.0 says (its upstream is
  already `origin/main`);
- if it is on `host-builder` or `host-next`: nothing needs to move until
  the last merge, since `main` then equals what the host runs. After the
  `next` PR merges, §1.4 with `T=main` moves it to `host-main`;
- §1.5 (check_stage1, boundary);
- a smoke build: §2c (`ship-region.sh washington-dc --no-upload`), then
  move its ZIM out as §2c says.

The promise to openZIM is everything on `main` by the end of 29 September
(Pacific). The merges don't wait for §2d–2g; report those as they finish.

## 4. Report back (fill in)

- [ ] §1.0: how `main` got the split (PR number or fast-forward), the
      host commits merged first, the check lines, stage-1 step 4's last
      line and step 5's result, the two `sz-*-stage1.txt` records.
- [ ] §1.1: host commit, branch, upstream, ahead count and host commits,
      `status -sb` lines, running scripts and queue environment, crontab
      lines, host scripts calling `build-region-fast.sh`, disk, checker
      result.
- [ ] §1.2: the clone's `gh/next` commit; the five ancestry lines.
- [ ] §1.3: checker, boundary, pytest results.
- [ ] §1.4/1.5 per move: `.before`/`.after` contents, `OK`/`STOP` line,
      check_stage1 result, PIDs still running; any rollback.
- [ ] §2a: exit code and class counts per pair; every unexpected path.
- [ ] §2b: exits, release stamps, rows and id differences vs https and
      cache, columns.
- [ ] §2c per branch: exit, gate lines, Overture and Wikipedia lines, Rust
      packer, where the ZIM went, scratch removed.
- [ ] §2d: (after `topic-wiki-429`) warm-up runs, the diff classes,
      article/image counts for main, next and published.
- [ ] §2e: PBF box, antimeridian log lines, tile and search counts (new
      and published).
- [ ] §2f: `measure.json` figures, extract and ZIM size, load.
- [ ] §2g: world-tiles metadata and whether it was reused; planet build
      id, size, sha256 and image digests; which variants ran (`sz-basic`
      after `topic-mbtiles-flag`, `sz-full` after `--profile`); per-run
      check failures; TABLE.md with the 1.5× verdicts; contended runs; the
      tarball path and size.
- [ ] Anything that printed `STOP` or `FAIL`, with its output.
- [ ] Leftovers: `/storage/sz-tests` size (`du -sh`), and the records in
      `$HOME/sz-move-*` and `$HOME/sz-pull-*`.
