# Moving the build host to `-builder` and `-next`, and testing them there

A runbook for a Claude Code session on the build host (`/storage/streetzim`,
host `ot-hel1`). The host checked the ops split in a scratch clone (stage 1
steps 1–3, [TESTING-STAGE1.md](TESTING-STAGE1.md)). This page gets the
split onto `main` and the host onto it (§1.0), then moves the host on to
the builder changes and this round's work, and runs the tests that only
the host can run. Merging to `main` is the user's action, through PRs.

**The host as of 29 September, 15:00 PDT** (the build-host session's
report):
- The production checkout is on `main` at `21b3ffd`: `origin/main`
  (`37403b8`) plus two torrent commits of the round, **not pushed**:
  `7af110c` (east-coast-us) and `21b3ffd` (indian-subcontinent).
  `git status` shows `M web/drive/build-info.js`, `M web/index.html`
  (deploy output) and `?? sz-tests/` (the test root, below).
- **The round is paused** (the user's instruction, 15:00:33 PDT). The
  round is `cloud/rebuild_old_regions.sh`, not `build-refresh-queue.sh`.
  SIGTERM went to its driver (PID 1934432) only; its child
  `build-region-fast.sh brazil` was left to finish without gates or
  upload. `finish_pending_uploads.sh` is held. The host's notes are in
  `$HOME/sz-round-paused.txt`.

**The user's decisions (29 September, late):**
- the orphaned web server on port 18770: stop it (§0);
- the deploy output: saved and restored to `HEAD` by any move that
  changes it, never committed (§1.4, "Deploy output");
- the stage-1 pull: approved, in the order of §1.0 (the host pushes its
  two commits to `host-torrents-2026-09-29`, the lead merges them and
  the ops PR, then the host pulls, while the round is paused);
- tests: §2d, then the Switzerland head-to-head (§2e), then Alaska (§2f)
  and Massachusetts (§2g); the Luxembourg/D.C. matrix (§2h) is optional.

**No pulls and no pushes on the host until the migration** except the
§1.0 steps. This rule is new: the host merged
`origin/main` (`0792d0d`, PR #19) at 2026-09-29 05:23 CEST while
west-asia was building, and pushed to `origin/main` seven times since
26 September (`fe02ced`, `423f65f`, `2db4fa0`, `273184a`, `abdb891` on
09-26; `0792d0d`, `37403b8` on 09-29), at the user's request. So every
region built after 05:23 CEST on 29 September was built on the
refactored `main`. **The host never pulls `main`:** its one move before
the migration is §1.0's stage-1 step, a fast-forward to the exact commit
`M` the lead names (`merge --ff-only <M>`), not a `git pull`. So
`builder` and `next` may merge into `main` at any time (§3): nothing on
the host follows `origin/main` on its own (no production script pulls or
fetches; only `ops/cloud/build-vm-startup.sh` on a build VM does). Never
run `git pull` in `/storage/streetzim` until the migration step of §3,
and never TESTING-STAGE1.md step 4 on this host (§1.0 replaces it).

**Until the round is over** (`sz-round.sh`, §0, prints `OVER`, which
needs the user's word), production moves only once: the stage-1 pull of
§1.0, which changes no code. The moves to `builder` and `next` (§1.4,
§1.6) and the production smoke build (§2c) wait. Reasons: §1.0 ("Why
production waits for the round").

**Branches** (on the host checkout's `origin`, GitHub; heads as of
29 September, late):

| short name | branch | head | contains |
|---|---|---|---|
| ops | `claude/adoring-dijkstra-i2vge7` | `bd09db9` | `main` (`37403b8`) merged into the ops split (CI green: run 36528140810) |
| builder | `claude/adoring-dijkstra-i2vge7-builder` | `dbfe993` | the builder commits (`3d8e7d7`) with `bd09db9` merged in |
| next | `claude/adoring-dijkstra-i2vge7-next` | `82def4f` | `builder` (`dbfe993`) merged in, this round's work, and the topic branches below |

`main` ⊂ `bd09db9` ⊂ `dbfe993` ⊂ `82def4f` (checked with
`merge-base --is-ancestor`). In the `builder` → `next` merge the three
stamp files (`web/drive/build-info.js`, `web/drive/sw.js`,
`web/drive/viewer/.version`) were resolved to `next`'s side. The host's
two torrent commits are on none of them yet (§1.0).

**On `next`** (merge commits on its first-parent line; §1.2 checks each
with `merge-base --is-ancestor`):

| topic branch | merge commit | what |
|---|---|---|
| `topic-wiki-429` | `e94474c` | a 429 from Wikipedia/Wikidata is retried, not recorded as a miss |
| `topic-host-runbook` | `c98ab07` | this runbook's previous version (the host's image for §2a–2b was built from it) |
| `topic-satellite-optin` | `2a80be9` | satellite imagery off by default, opt-in (`--satellite`) |
| `topic-viewer-polish` | `f66c028` | viewer fixes (units, focus rings, sheet, `--kiwix-poi-pages`) |
| `topic-mbtiles-flag` | `bb5b992` | `streetzim --mbtiles-url` (`http(s)://` is downloaded, `file://` is read in place), planet-size MBTiles cut to the area |
| `topic-full-profile` | `3cd8e8e` | `streetzim --profile {full,basic}`; the command line's default is `full` |
| `topic-terrain-openzim` | `82def4f` | terrain without a world DEM; on in the full profile (`--terrain`/`--no-terrain`) |

## 0. Rules for the session

- **Three helpers, written once** (they only read; nothing here changes
  `/storage/streetzim`). `sz-env.sh` is the one place the test root is
  set; every block below starts by sourcing it, and so do the other two.
  This replaces the earlier `$HOME/sz-busy.sh` (and its hand-edited
  filter). Until the orphaned server below is stopped, `sz-busy.sh`
  lists it:

```bash
cat > "$HOME/sz-env.sh" <<'EOF2'
# StreetZim host tests: the one place the test root is set (TESTING-NEXT.md §0).
# Sourced by every block, by sz-busy.sh and by sz-round.sh.
export SZT=/storage/streetzim/sz-tests      # the test root (the user's choice, 2026-09-29)
export PROD=/storage/streetzim              # the production checkout
export PY=/storage/streetzim/venv-linux/bin/python3
# git run anywhere under $SZT (results/, dcwiki/, golden/src-*) must not find
# production's repository above it: without this, `git status` there reads and
# can rewrite /storage/streetzim/.git/index.
export GIT_CEILING_DIRECTORIES=/storage/streetzim
export TMPDIR=$SZT/tmp
mkdir -p "$TMPDIR" 2>/dev/null
# Never source this file in a shell that then starts production (a round, a
# resume, ship-region.sh): it sets TMPDIR and GIT_CEILING_DIRECTORIES, and git
# run from a subfolder of the checkout then finds no repository.
# Tests of §2d-2h start only while no round driver runs (paused or over).
sz_round_quiet() { bash "$HOME/sz-round.sh" > /dev/null; case $? in 1|3) return 0 ;; *) echo "STOP: the round runs (sz-round.sh); tests wait"; return 1 ;; esac; }
EOF2
cat > "$HOME/sz-busy.sh" <<'EOF2'
#!/usr/bin/env bash
# Prints the production processes that forbid a change to /storage/streetzim
# and exits 0 if there are any; prints IDLE and exits 1 if there are none.
# Any other exit status (sz-env.sh missing, ...) means: treat as busy.
# Only reads the process table.
. "$HOME/sz-env.sh" 2>/dev/null && [ -n "${SZT:-}" ] || { echo "sz-busy: \$HOME/sz-env.sh missing or sets no SZT"; exit 2; }
command -v pgrep >/dev/null || { echo "sz-busy: no pgrep"; exit 2; }
# This script's own shell and its ancestors (the session's shell, whose
# command line may quote these names) are not production.
anc=" "; p=$$
while [ -n "$p" ] && [ "$p" -gt 1 ]; do
  anc="$anc$p "
  p=$(awk '/^PPid:/ {print $2}' "/proc/$p/status" 2>/dev/null)
done
n=0
while read -r pid args; do
  case "$anc" in *" $pid "*) continue ;; esac
  case "$args" in *"$SZT"*) continue ;; esac        # our tests, by path
  # A session's own watcher that polls these helpers is not production.
  case "$args" in *sz-busy.sh*|*sz-round.sh*) continue ;; esac
  # Processes in a container: skip only our test containers (image
  # streetzim:hosttest or ghcr.io/openzim/maps, or names cmp-* / sz-test-*).
  # Production runs containers too (build-world-tiles.sh: tilemaker), and an
  # unknown or unreadable container counts.
  cid=$(grep -oE '[0-9a-f]{64}' "/proc/$pid/cgroup" 2>/dev/null | head -1)
  if [ -n "$cid" ]; then
    ci=$(timeout 10 docker inspect --type container -f '{{.Config.Image}} {{.Name}}' "$cid" 2>/dev/null)
    case "$ci" in
      "streetzim:hosttest "*|"ghcr.io/openzim/maps:"*|"ghcr.io/openzim/maps "*|*" /cmp-"*|*" /sz-test-"*) continue ;;
    esac
  fi
  echo "$pid $args"; n=$((n + 1))
done < <(pgrep -af '[c]reate_osm_zim|[b]uild-region|[s]hip-region|[u]pload_validated|[f]inish_pending_uploads|[f]inish-pending-loop|[d]ownload_overture_data|[o]smium (extract|cat|merge)|[e]xtract-region-pbfs|[c]heck_terrain_coverage|[v]alidate_zim|[r]oute_cli|[c]heck_smoke_pairs|[p]wa_smoke_test|[s]erve-web-local|[k]iwix_viewer_gate|[d]evice-matrix|[o]verlap-check|[m]ap-health|[g]enerate\.py|[f]irebase|[s]ync-drive-viewer|[s]treetzim-pack|[x]apianbuilder|[t]ilemaker|[b]uild-world-|[b]uild-refresh-queue|[r]ebuild_old_regions|[r]un-continent-chain|[r]etrofit-chips|[v]iewer-refresh|[r]ollout_viewer_patch|[r]epackage_zim|[e]urope-safety-net|[r]egate-stranded|[b]uild_torrent|[c]anada_viewer_repack|[/]bin/ia ')
# The round's drivers, as sz-round.sh finds them: every bash/sh script named
# *queue* or *rebuild* (by its parsed script argument, so a `bash -c` text that
# only mentions such a file does not count), and unknown scripts in the checkout.
r=$(bash "$HOME/sz-round.sh" 2>&1); rc=$?
case $rc in
  1|3) ;;
  0) printf '%s\n' "$r" | grep '^RUNNING'; n=$((n + 1)) ;;
  *) echo "sz-busy: sz-round.sh failed (exit $rc): $r"; n=$((n + 1)) ;;
esac
[ "$n" -gt 0 ] && exit 0
echo IDLE; exit 1
EOF2
cat > "$HOME/sz-round.sh" <<'EOF2'
#!/usr/bin/env bash
# Is a production round running, paused, or over?  Only reads.
#   exit 0  RUNNING: a driver (or an unknown script in the checkout) runs.
#   exit 3  NOT OVER: no driver runs, but the user has not said the round is
#           over (it is paused), or something ran after the user said so.
#   exit 1  OVER: no driver runs, $HOME/sz-round-over.txt holds the user's
#           word, and no round file or pidfile changed after it was written.
#   other   error: treat as running.
# Fail-safe: any bash/sh process running a script inside /storage/streetzim
# counts as a driver unless it is a known per-region step (sz-busy.sh counts
# those). A stale pidfile whose PID now belongs to something else is shown
# and counted too: ask.
. "$HOME/sz-env.sh" 2>/dev/null && [ -n "${SZT:-}" ] || { echo "sz-round: \$HOME/sz-env.sh missing"; exit 2; }
command -v pgrep >/dev/null || { echo "sz-round: no pgrep"; exit 2; }
P=/storage/streetzim
PR=$(cd "$P" 2>/dev/null && pwd -P) || { echo "sz-round: cannot read $P"; exit 2; }
# Per-region steps a driver runs; never drivers themselves.
LEAF='build-region-fast.sh|build-region.sh|ship-region.sh|upload_validated.sh|kiwix_viewer_gate.sh|extract-region-pbfs.sh|sync-drive-viewer.sh|check_stage1.sh'
# Passive monitors in the checkout (read logs and /proc, write only their own
# log; verified on the host 2026-09-29): shown, never counted. Exact paths only.
# (The old paths are symlinks into ops/; both spellings are listed.)
MON="$PR/ops/scripts/resource-watch.sh $PR/ops/tmp/upload-recorder.sh $PR/scripts/resource-watch.sh $PR/tmp/upload-recorder.sh"
seen=" "; running=0
files() {  # $1 pid, $2 script or command line
  case "$2" in
    *rebuild_old_regions*)
      echo "    files: $P/rebuild-old.{list,tsv,log,out}, $P/.rebuild-old.pid, $P/.retrofit-upload.lock, $P/tmp/.regions-pbf.lock"
      echo "    world tiles (script line WORLD_MB=): $(sed -n 's/^WORLD_MB=//p' "$P/cloud/rebuild_old_regions.sh" | head -1)" ;;
    *build-refresh-queue*)
      echo "    files: $P/queue-refresh-<date>.log, \$RESULTS or $P/queue-refresh.tsv, its .release, $P/queue-refresh.out, $P/tmp/.regions-pbf.lock"
      tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | grep -E '^(OVERTURE_|WORLD_|PLANET=|TIER_A=|RESULTS=|REGISTRY=)' | sed 's/^/    env: /' ;;
    *)
      echo "    open files under $P (no known layout for this driver):"
      ls -l "/proc/$1/fd" 2>/dev/null | grep -o "$P/[^ ]*" | sort -u | sed 's/^/      /' ;;
  esac
}
# 1. Pidfiles. A live PID whose command line is not a shell script is a
#    stale pidfile with a reused PID: shown, counted (ask the user).
for f in "$P"/.*.pid; do
  [ -s "$f" ] || continue
  pid=$(tr -dc 0-9 < "$f")
  [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ] || continue
  args=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  [ -n "$args" ] || continue
  case "$args" in
    *.sh*) echo "RUNNING  pidfile $(basename "$f") -> $pid $args" ;;
    *)     echo "RUNNING? pidfile $(basename "$f") -> $pid $args  (not a script: a stale pidfile whose PID was reused? ask)" ;;
  esac
  files "$pid" "$args"; seen="$seen$pid "; running=1
done
# 2. Every bash/sh/dash process: find its script argument. Selected by the
#    command line's first word, not the process name: a `#!/bin/bash` script
#    run directly (./x.sh) is named after the script but runs /bin/bash x.sh.
for pid in $(pgrep -f '^([^ ]*/)?(bash|sh|dash)( |$)'); do
  case "$seen" in *" $pid "*) continue ;; esac
  mapfile -d '' -t a 2>/dev/null < "/proc/$pid/cmdline" || continue
  s=""; skip=0
  for x in "${a[@]:1}"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$x" in
      -o|+o|-O|+O|--rcfile|--init-file) skip=1; continue ;;
      --*) continue ;;
      -*c*) s=""; break ;;          # -c, or combined flags such as -ec: no script
      -*|+*) continue ;;
      *) s=$x; break ;;
    esac
  done
  [ -n "$s" ] || continue
  case "$s" in /*) abs=$s ;; *) abs="$(readlink "/proc/$pid/cwd" 2>/dev/null)/$s" ;; esac
  real=$(readlink -f "$abs" 2>/dev/null || echo "$abs")
  case "$abs$real" in *"$SZT"*) continue ;; esac
  base=${s##*/}
  case "$base" in
    *queue*|*rebuild*|run-continent-chain*|retrofit-chips*|europe-safety-net*|finish-pending-loop*|rollout_viewer*|viewer-refresh*|canada_viewer_repack*)
      echo "RUNNING  $pid ${a[*]}  (cwd $(readlink "/proc/$pid/cwd" 2>/dev/null))"
      files "$pid" "$s"; running=1; continue ;;
  esac
  case "$real" in "$P"/*|"$PR"/*) ;; *) continue ;; esac
  printf '%s\n' "$base" | grep -q -x -E "$LEAF" && continue
  case " $MON " in *" $real "*|*" $abs "*) echo "monitor  $pid ${a[*]}  (passive, not a driver)"; continue ;; esac
  echo "RUNNING  $pid ${a[*]}  (an unknown script in the checkout, counted as a driver; ask if it is not one)"
  files "$pid" "$s"; running=1
done
[ "$running" = 1 ] && exit 0
O=$HOME/sz-round-over.txt
if [ -s "$O" ]; then
  newer=$(find "$P" -maxdepth 1 \( -name '.*.pid' -o -name 'rebuild-old.*' -o -name 'queue-refresh*' \) -newer "$O" 2>/dev/null)
  if [ -n "$newer" ]; then
    echo "NOT OVER: these changed after the user's word in $O (a round ran again?); ask:"; echo "$newer" | sed 's/^/    /'
    exit 3
  fi
  echo "OVER (no driver runs; the user's word, $O):"; sed 's/^/    /' "$O"; exit 1
fi
echo "NOT OVER: no driver runs, but the user has not said the round is over (\$HOME/sz-round-over.txt is missing)."
[ -s "$HOME/sz-round-paused.txt" ] && echo "    paused: see $HOME/sz-round-paused.txt"
exit 3
EOF2
bash "$HOME/sz-busy.sh"; echo "sz-busy exit $?"
bash "$HOME/sz-round.sh"; echo "sz-round exit $?"
```

- **The test root** is `/storage/streetzim/sz-tests`, set only in
  `sz-env.sh` (`$SZT`). `/storage` is root-owned (755) and the account
  has no sudo; it can write only `/storage/streetzim`,
  `/storage/experiments` and `/storage/zimru`, and the user chose
  `/storage/streetzim/sz-tests`. Because it sits inside the production
  checkout:
  - production's `git status` shows `?? sz-tests/`. Expected; record it.
    A `git add -A` on the host would stage the test clone as a gitlink:
    nobody runs one (the host's scripts add only
    `web/torrents/<id>.torrent`). `check_stage1.sh` and
    `tools/check_boundary.py` look at tracked files only, and Firebase
    deploys `web/` only, so neither sees it;
  - `GIT_CEILING_DIRECTORIES=/storage/streetzim` (in `sz-env.sh`) keeps
    git run anywhere below it from finding production's repository.
    `git -C /storage/streetzim …` and `git -C $SZT/next …` still work.
    Every test command runs with it: every block sources `sz-env.sh`
    first, and stops (`|| exit`) if it can't. Run the blocks as Claude
    Code's Bash tool does (a non-interactive shell): an `exit` there ends
    only that command;
  - the process checks below skip anything whose command line names
    `$SZT`.
- **Nothing moves while a build, a gate or an upload runs.** `sz-busy.sh`
  prints `IDLE` and exits 1 only when none runs; exit 0 means busy, any
  other exit (a helper missing) counts as busy. Every block that changes
  `/storage/streetzim` runs it **inside the block**, as
  `elif bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then echo "STOP: …"`, so
  there is no gap between the check and the change. It counts the round's
  drivers, builds, gates (validator, routing, the browser and Kiwix
  gates), the PBF extract, the Overture download, uploads and the
  pending-upload finisher. It skips our tests: anything naming `$SZT`,
  anything running in a container (our images' tilemaker, osmium and
  `measure_build.py` show `/work/…` or `/run/…` paths, not `$SZT`), and
  its own calling shells.
- **The orphaned web server** (the user's decision, 29 September: stop
  it). `sz-busy.sh` never prints `IDLE` while
  `scripts/serve-web-local.py 18770 /storage/streetzim/web` (PID 2691498,
  running since 2026-09-16, parent init, idle) runs. No production script
  uses port 18770 (`ship-region.sh` picks 8810–8889 per run,
  `build-refresh-queue.sh` 8801, `retrofit-chips-queue.sh` 8811, each
  stopping its own server). Stop that one process, after checking it is
  still exactly that:

```bash
pid=2691498
a=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null); pp=$(awk '/^PPid:/ {print $2}' "/proc/$pid/status" 2>/dev/null)
st=$(ps -o lstart= -p "$pid" 2>/dev/null)
echo "cmdline: ${a:-<gone>}"; echo "parent: ${pp:-<gone>}"; echo "started: ${st:-<gone>}"
if [ -z "$a" ]; then echo "OK: already gone"
elif [ "$pp" != 1 ]; then echo "STOP: its parent is $pp, not init; ask"
elif [ "$(date -d "$st" +%F 2>/dev/null)" != 2026-09-16 ]; then echo "STOP: it did not start on 2026-09-16 (a reused PID?); ask"
else
  case "$a" in
    *python*" scripts/serve-web-local.py 18770 /storage/streetzim/web ")
      kill -TERM "$pid"; sleep 3
      if [ -e "/proc/$pid" ]; then echo "STOP: $pid still runs after SIGTERM; ask (no SIGKILL without the user)"
      else echo "OK: stopped $pid"; fi ;;
    *) echo "STOP: PID $pid is not that server any more; ask" ;;
  esac
fi
```

  Then `sz-busy.sh` keeps its normal filter: a new `serve-web-local`
  process is production's (a smoke gate) and counts as busy.
- **The round** is whatever driver `sz-round.sh` finds: a live pidfile
  (`.rebuild-old.pid`, `.rollout-viewer.pid`, `.canada-repack.pid`), a
  bash/sh process running a `*queue*`, `*rebuild*` or chain script, or
  **any other script inside `/storage/streetzim`** that isn't a per-region
  step (`build-region-fast.sh`, `ship-region.sh`, `upload_validated.sh`,
  …; `sz-busy.sh` counts those): host scripts such as
  `.carolinas-fast-ship.sh` or `.build-africa-light.sh` drive builds
  under names no list foresees. It prints each driver with its files; a
  pidfile whose PID now runs something that isn't a script is shown as
  `RUNNING?` (a stale pidfile: ask). The two known drivers:

  | driver | its files | world tiles |
  |---|---|---|
  | `cloud/rebuild_old_regions.sh` (this round) | `rebuild-old.list` (read once at start), `rebuild-old.tsv` (one row per region), `rebuild-old.log`, `rebuild-old.out`, `.rebuild-old.pid`, `.retrofit-upload.lock` (upload), `tmp/.regions-pbf.lock` (extract) | `WORLD_MB=` in the script (line 36): `/storage/streetzim/world-data/world-tiles-v3.mbtiles` |
  | `build-refresh-queue.sh` (the Sep 19–21 round, finished; last start 2026-09-10) | `queue-refresh-<date>.log`, `queue-refresh.tsv` (or `$RESULTS`), `queue-refresh.release`, `queue-refresh.out`, `tmp/.regions-pbf.lock` | its `WORLD_MBTILES` environment |

  A round is **over** only when no driver runs **and** the user has said
  so: then, on their word, write it down (their words and the date):
  `printf '%s\n' "<the user's words>" "$(date -Is)" > "$HOME/sz-round-over.txt"`.
  If a pidfile or a round file (`rebuild-old.*`, `queue-refresh*`)
  changes after that file was written, `sz-round.sh` falls back to
  `NOT OVER` (something ran again): ask.
  `sz-round.sh` exits 0 while a driver runs (`RUNNING`), 3 while none
  runs but the round isn't over (`NOT OVER`: paused), and 1 once it is
  `OVER`. §1.4, §1.6 and §2c need 1. The tests of §2d–2h need 1 or 3:
  they run while the round is paused, never while it runs (the §2e
  runner enforces it; for the others, check before starting).
- **Pausing a round** is the user's call. The way that works: SIGTERM to
  the driver's PID only (`kill -TERM <pid>`), leaving a running child to
  finish; the driver starts nothing more. **Never `kill -INT` a driver:**
  bash waits for its foreground child and then carries on into the gates
  and the upload. Never write rows into the round's results file to make
  it stop at a region boundary (that corrupts the record the restart
  reads, §1.0 "Resuming the paused round").
- **Git on `/storage/streetzim`:** only the commands written here. Moves
  are fast-forward only (`pull --ff-only --no-rebase` with
  `merge.autoStash=false` and `rebase.autoStash=false`). Never
  `stash`, `reset`, `checkout -- <path>`, `restore` (except the
  deploy-output restore inside the §1.4 and §1.6 blocks), `rebase`,
  `commit`, `push` (except §1.0's push), `add`, or `clean` there.
  `status` always with `--no-optional-locks`.
- **Record before and after.** Each move writes `$HOME/sz-move-<name>.before`
  (branch and commit) and `.after` (commit), and refuses to run twice.
  Rollback (§1.7) uses them.
- **The round is not ours.** Don't change its settings, environment,
  lists (`rebuild-old.list`, `queue-refresh.*`), results or logs; don't
  stop, restart or resume it unless the user says so. The round passes
  `OVERTURE_RELEASE=2026-08-19.0` to every build (the driver's `REL=`).
  That is not the wrappers' default: `main`'s and `builder`'s
  `build-region-fast.sh` default to `2026-04-15.0` (line 48), and only
  `next`'s refuses to start without it. Never set it to `latest` for
  anything touching the round.
- **Host-edited files stay as they are:** the `*.list` queues,
  `viewer-refresh.tsv`, `cloud/region-variants.tsv` and
  `tmp/live-inventory.out` ([in-place.txt](in-place.txt)). Never edit,
  restore or commit them. `M` on them in `git status` is expected.
- **Deploy output** (`web/index.html`, `web/drive/build-info.js`,
  `web/drive/sw.js`, `web/drive/viewer/.version`) is rewritten by every
  upload's deploy. `M` on them is expected. Never commit them on the host,
  and never `checkout --` them by hand (ops/README.md's "Pulling on the
  build host" suggests it). A move that would change them saves and
  restores them itself (§1.4, "Deploy output").
- **Never publish from a test.** No `cloud/upload_validated.sh`, no
  `web/generate.py --deploy`, no `ia upload`, no Firebase, no torrent.
  Test outputs go under `$SZT`; a test that writes a ZIM into
  `/storage/streetzim` moves it out when it finishes (§2c).
- **Tests run in a separate clone,** `$SZT/next`, and write only under
  `$SZT`. Only §1.0 (its push and pull), §1.4–1.7 and §2c change the
  production checkout.
- **Docker:** its filesystem is the tight one (the docker LV: 57 GB, 85 %
  used, shared with another tenant). Remove only our own images
  (`streetzim:hosttest`, `ghcr.io/openzim/maps:0.2.1`) when done; never
  `docker system prune` or `docker image prune`.
- **Stop and report at any `STOP` or `FAIL`.** Don't work around it.
- Every command below uses the variables of `sz-env.sh` or absolute
  paths. Run each block as one command.

## 1. Moving the host: main → ops split → builder → next

**Which branch, when.** First `main` gets the host's two commits and the
ops split, and the host pulls it (§1.0), in an idle window (the split
changes no code). The production checkout then stays there while the
tests of §2a–b and §2d–2h run in the clone. It moves to `builder` only
after the round is over, §1.2–1.3 and §2a pass, `builder` contains the
new `main` (§1.2), and the user says go; to `next` after §2c passed on `builder` and the
user says go (that move changes the public site: see below); to `main`
after the merges (§3).

### 1.0 The ops split onto `main`, and the host's stage-1 pull

**Now:** the round is paused; the host is at `21b3ffd` on `main`, two
torrent commits (`7af110c`, `21b3ffd`) ahead of `origin/main`, which is
`37403b8`. `origin/main` is an ancestor of `bd09db9`. `main` is not a
protected branch on GitHub (the branches API reports `protected: false`
on 29 September; repository rulesets can't be seen from here).

**The order** (approved by the user on 29 September; each step waits
for the one before, and the host session **stops and waits for the lead**
after steps 1 and 3):
1. **The host pushes its two torrent commits to a new branch,
   `host-torrents-2026-09-29`** (the block below). Only that branch: never
   to `main`, never to any `claude/*` branch. Then report the pushed
   commit and **wait**.
2. **The lead merges that branch** into ops
   (`claude/adoring-dijkstra-i2vge7`), `builder` and `next` (a merge
   commit on each), then opens a PR ops → `main` and, once CI is green,
   merges it with **Create a merge commit**. The result `M` contains
   `bd09db9` and the host's commits. The lead tells the host `M`'s full
   40-character hash.
3. **The host runs the checks** below (read-only), reports them, and
   **waits** for the lead's go in every case.
4. **The host moves to `M`** (the "stage-1 move" block below, then
   TESTING-STAGE1.md step 5), with `sz-busy.sh` inside the block, while
   the round is paused. It is a fast-forward to exactly `M`, not a `git
   pull`: whatever reached `main` after `M` (`builder`, `next`) stays
   off the host. TESTING-STAGE1.md step 4 is **not** used on this host.

The push (step 1):

```bash
. "$HOME/sz-env.sh" || exit
G="git -C /storage/streetzim"
B=refs/heads/host-torrents-2026-09-29
H=$($G rev-parse HEAD)
$G ls-remote --exit-code origin "$B" >/dev/null 2>&1; lr=$?
if [ "$(printf %.7s "$H")" != 21b3ffd ]; then
  echo "STOP: HEAD is not 21b3ffd any more:"; $G log --oneline -3; echo "ask"
elif [ "$($G rev-parse --abbrev-ref '@{u}')" != origin/main ] || [ "$($G rev-parse '@{u}' | cut -c1-7)" != 37403b8 ]; then
  echo "STOP: the upstream is not origin/main at 37403b8; ask"
elif [ "$($G rev-list '@{u}..HEAD' | cut -c1-7 | tr '\n' ' ')" != "21b3ffd 7af110c " ]; then
  echo "STOP: the host's own commits are not exactly 7af110c and 21b3ffd:"; $G log --oneline '@{u}..HEAD'; echo "ask"
elif [ "$lr" = 0 ]; then
  echo "STOP: origin already has host-torrents-2026-09-29; ask"
elif [ "$lr" != 2 ]; then
  echo "STOP: could not list origin (ls-remote exit $lr: network or credentials); ask"
elif pgrep -af '[u]pload_validated|[f]inish_pending|[f]inish-pending-loop' || [ -e /storage/streetzim/.git/index.lock ]; then
  echo "STOP: an upload is running (above) or git holds the index lock; push after it"
else
  $G push --no-follow-tags origin "$H:$B" &&
  [ "$($G ls-remote origin "$B" | cut -f1)" = "$H" ] &&
  echo "OK: pushed $H to host-torrents-2026-09-29 (checked on origin); now wait for the lead" ||
  echo "STOP: the push failed or origin shows another commit; ask"
fi
```

(The push names the commit, not a branch, and a new ref, so it can't
touch `main` or any `claude/*` branch; without `+` or `--force` it can't
overwrite anything, and `--no-follow-tags` keeps a `push.followTags`
setting from sending tags. It changes no file and no local branch; it
may add the remote-tracking ref `origin/host-torrents-2026-09-29`. A
build may run meanwhile: the push only reads the object store; it waits
only for an upload, which commits.)

**Never squash or rebase-merge** ops into `main`: either gives `main`
commits that don't contain `bd09db9` or the host's commits, and the
host's `pull --ff-only` then refuses. **If the PR page offers no "Create a merge commit"** (a
ruleset requiring linear history), stop and ask; don't fall back to
squash or rebase.

**The cost of the merge commit:** `M` is on `main` but not on `builder`
or `next`. After the host pulls `M`, the moves of §1.4 refuse (`HEAD` must
be an ancestor of the target) until the branch owner merges the new
`main` into `builder`, then `builder` into `next`. Those merges bring only
`M` itself (its parents are on both), so they are conflict-free; §1.2's
`origin/HEAD` lines check them.

**Every further upload adds a host commit.** The round is paused and
`finish_pending_uploads.sh` held, so none should come. If one does
(`git -C /storage/streetzim log --oneline '@{u}..HEAD'` lists more than
the two), the pull refuses (safely): stop and ask; the new commits need
the same way to `main`.

**Checks before the host moves** (`origin/main` is fetched into the
host's remote-tracking ref only; nothing else changes). Set `M` to the
full hash the lead sent:

```bash
M=<the 40-character hash of main's merge commit, from the lead>
. "$HOME/sz-env.sh" || exit
G="git -C /storage/streetzim"
$G -c gc.auto=0 -c maintenance.auto=false fetch -q origin +refs/heads/main:refs/remotes/origin/main
$G --version
$G rev-parse --abbrev-ref HEAD '@{u}'
[ ${#M} = 40 ] && $G rev-parse -q --verify "$M^{commit}" >/dev/null && echo "OK: M is a commit here" || echo "STOP: M is not a full hash of a fetched commit"
$G merge-base --is-ancestor "$M" origin/main && echo "OK: M is on main" || echo "STOP: M is not on main"
$G merge-base --is-ancestor HEAD "$M" && echo "OK: M contains the host's commits" || echo "STOP: the host has commits M lacks"
$G merge-base --is-ancestor bd09db94d89dca6b97b36cdbf153a46ea13ab228 "$M" && echo "OK: M contains bd09db9" || echo "STOP: bd09db9 is not in M"
$G diff --stat bd09db94d89dca6b97b36cdbf153a46ea13ab228 "$M"
$G cat-file -e "$M:ops/in-place.txt" && echo "OK: M has the split" || echo "STOP: M lacks the split"
$G log --oneline "$M..origin/main" | head -5; echo "(commits on main after M: they stay off the host)"
$G --no-optional-locks status --short
[ -z "$($G diff --cached --name-only)" ] && echo "OK: nothing staged" || echo "STOP: staged changes in production's index"
c=$($G --no-optional-locks diff --name-only HEAD | sort | comm -12 - <($G diff --name-only HEAD "$M" | sort)); [ -z "$c" ] && echo "OK: the move touches no locally modified file" || { echo "STOP: locally modified and changed by the move:"; echo "$c"; }
u=$($G diff --name-only --diff-filter=A HEAD "$M" | while read -r f; do [ -e "/storage/streetzim/$f" ] || [ -L "/storage/streetzim/$f" ] && echo "$f"; done); [ -z "$u" ] && echo "OK: no untracked file in the way" || { echo "STOP: untracked files where M adds tracked ones:"; echo "$u"; }
```

**Expect:** the branch `main` and upstream `origin/main`, git 2.23 or
later (for `switch` and `restore`), eight `OK` lines, and the diff listing
nothing or only `web/torrents/*.torrent` files (the two host commits:
east-coast-us and indian-subcontinent). Commits on `main` after `M` are
fine. Anything else: STOP. Report all of it and **wait** for the lead.

**The deploy output doesn't block this pull.** `web/index.html` and
`web/drive/build-info.js` are `M` (and after another upload
`web/drive/viewer/.version` and `web/drive/sw.js` can be): the split
changes none of them, and the "touches no locally modified file" line
says so, so they stay as they are. The move to `next` changes them; §1.4 saves and restores them
then. `check_stage1.sh` step 2 reports them (`FAIL local change`);
record them.

**When:** the move needs an idle window: `sz-busy.sh` prints `IDLE`
(after the orphaned server is dealt with, §0). With the round paused,
the window opens once `build-region-fast.sh brazil` has finished. Then
run TESTING-STAGE1.md **step 1** (read-only), this block, and
TESTING-STAGE1.md **step 5**. Its step 2 line "the branch contains the
host's commit" is replaced by the checks above, and its step 4 by this
block. It records `$HOME/sz-before-stage1.txt` and
`$HOME/sz-after-stage1.txt` (the same records step 4 writes), and must
print `OK: moved to the split`:

```bash
M=<the same 40-character hash>
. "$HOME/sz-env.sh" || exit
G="git -C /storage/streetzim"
if [ ${#M} != 40 ] || ! $G rev-parse -q --verify "$M^{commit}" >/dev/null; then
  echo "STOP: M is not a full hash of a fetched commit (run the checks first)"
elif [ ! -s "$HOME/sz-busy.sh" ] || ! command -v pgrep >/dev/null; then
  echo "STOP: \$HOME/sz-busy.sh or pgrep is missing; ask"
elif bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then
  echo "STOP: the processes above are running (or sz-busy.sh failed); move between them"
elif $G cat-file -e HEAD:ops/in-place.txt 2>/dev/null; then
  echo "STOP: already at the split"; cat "$HOME/sz-before-stage1.txt" "$HOME/sz-after-stage1.txt" || echo "records incomplete; ask"
elif [ "$($G rev-parse --abbrev-ref HEAD)" != main ]; then
  echo "STOP: not on main; ask"
elif ! $G merge-base --is-ancestor "$M" refs/remotes/origin/main || ! $G merge-base --is-ancestor HEAD "$M"; then
  echo "STOP: M is not on main, or lacks the host's commits; ask"
elif [ -n "$($G diff --cached --name-only)" ]; then
  echo "STOP: staged changes in production's index; ask"
elif c=$($G --no-optional-locks diff --name-only HEAD | sort | comm -12 - <($G diff --name-only HEAD "$M" | sort)); [ -n "$c" ]; then
  echo "STOP: modified here and changed by the move; ask:"; echo "$c"
else
  rm -f "$HOME/sz-after-stage1.txt" &&
  $G rev-parse HEAD > "$HOME/sz-before-stage1.txt.new" &&
  mv "$HOME/sz-before-stage1.txt.new" "$HOME/sz-before-stage1.txt" &&
  $G -c merge.autoStash=false merge -q --ff-only "$M" &&
  $G rev-parse HEAD > "$HOME/sz-after-stage1.txt" &&
  if [ "$(cat "$HOME/sz-after-stage1.txt")" = "$M" ]; then echo "OK: moved to the split ($M)"
  else echo "STOP: HEAD is not M after the move; don't run this block again; ask"; fi ||
  echo "STOP: the move did not finish (a fast-forward refusal changes nothing); check git log -1; ask"
fi
```

(`merge --ff-only <M>` is the fast-forward `pull --ff-only` would do,
without its fetch: it can only move `main` forward to `M`, and refuses,
changing nothing, if it can't. A local branch's `git pull` would take
whatever `main` holds by then.)

**From here until the migration (§3): never `git pull` on the host.**
`main` may gain `builder` and `next` at any time; the host stays at `M`
(plus its own torrent commits once the round resumes). The one way the
host takes a later `main` is the §1.4 block with `T=main`, after the
round, on the user's word.

**Rollback of this pull** (instead of TESTING-STAGE1.md step 6, which
uses `reset --keep`; this runbook uses no `reset`: the old commit gets a
branch of its own). It needs an idle window like the pull, and stops if
HEAD moved since (a torrent commit: switching back would take that file
off the disk):

```bash
. "$HOME/sz-env.sh" || exit
G="git -C /storage/streetzim"
if [ ! -s "$HOME/sz-before-stage1.txt" ] || [ ! -s "$HOME/sz-after-stage1.txt" ]; then
  echo "STOP: the stage-1 records are missing; ask"
elif [ "$($G rev-parse HEAD)" != "$(cat "$HOME/sz-after-stage1.txt")" ]; then
  echo "STOP: HEAD moved since the pull (a host commit?):"; $G log --oneline -3; echo "ask"
elif bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then
  echo "STOP: the processes above are running; roll back between them"
elif $G rev-parse -q --verify refs/heads/host-rollback-stage1 >/dev/null; then
  echo "STOP: branch host-rollback-stage1 exists already; ask"
else
  $G switch -q -c host-rollback-stage1 "$(cat "$HOME/sz-before-stage1.txt")" &&
  [ "$($G rev-parse HEAD)" = "$(cat "$HOME/sz-before-stage1.txt")" ] &&
  echo "ROLLED BACK to $(cat "$HOME/sz-before-stage1.txt") on host-rollback-stage1" ||
  echo "STOP: the switch refused or landed elsewhere; ask"
fi
```

Then ask. That leaves the checkout on `host-rollback-stage1`, which has
no upstream; later `@{u}` commands fail there until the user decides.

**Why production waits for the round** before moving to `builder` or
`next`:
- **Overture transport.** The paused driver is `main`'s
  `cloud/rebuild_old_regions.sh`; resumed, it is again `main`'s (or the
  split's identical copy), and it calls `download_overture_data.py` by
  relative path without `--transport` (line 171). On `next` that
  downloader defaults to `https` (§2b tests both), so every remaining
  region without a cached parquet would switch transport mid-round.
  (`next`'s own `rebuild_old_regions.sh` passes
  `--transport "${OVERTURE_TRANSPORT:-s3}"`, but only a driver started
  from `next` runs it. `builder`'s downloader is still `main`'s, s3 only.)
- **Mixed code in one round.** The driver calls `build-region-fast.sh`,
  `cloud/validate_zim.py`, the gates and `cloud/upload_validated.sh` by
  path for each region, so every region after a move would be built,
  gated and published with the new code.
- **The public site.** Every upload deploys `web/` from the checkout.
  `next` carries the new `/drive/` viewer and changes `web/index.html`
  and `web/template.html`; `builder` changes `web/drive/viewer/index.html`
  (the licence sections for satellite, terrain and Wikipedia are hidden
  unless the ZIM has them): small, but not "no `web/` change".
- **Host commits.** Each upload adds a torrent commit to whatever branch
  the host is on; §1.4 then refuses until that commit is on the target,
  so a move during the round needs a push and a merge per region.

Once the round is over, none of this applies: the ops scripts on
`builder`/`next` pass `--transport "${OVERTURE_TRANSPORT:-s3}"`
explicitly (ops/tests/test_overture_release_ops.py checks every call), so
no downloader change is needed.

**Resuming the paused round** is the user's decision, and so are its
details; the host's notes (`$HOME/sz-round-paused.txt`) have the
driver's start command and state. What a restart does (from the
script):
- it reads `rebuild-old.list` once, at start, and skips only regions
  whose `rebuild-old.tsv` row says `uploaded` or `upload-pending`. So it
  rebuilds **iceland** and **southeast-asia** (rows `upload-failed`),
  and **brazil**, whose build finished after the pause and has no row:
  on the same day `build-region-fast.sh` finds today's ZIM, exits 0 with
  `ALREADY EXISTS`, and the driver records `no-output` and skips brazil
  (built, never gated or shipped); after a date change it rebuilds brazil
  from scratch (~12 h). What to do with brazil's finished ZIM, and
  whether to trim the list, is the user's call; ask;
- it first waits for `.rollout-viewer.pid`'s process, if any;
- it runs the code of the checkout at that moment: `main`'s, or after
  §1.0 the split's (the same code behind symlinks).

To resume, on the user's word and only with `sz-busy.sh` printing
`IDLE`:
1. Check that no test of ours runs: `pgrep -af "$SZT/run-one.sh"`,
   `docker ps --format '{{.Names}}' | grep -E '^cmp-'`, and any
   `create_osm_zim`/`osmium` naming `$SZT` (`sz-busy.sh` skips all of
   these). If one does, ask the user whether to stop it first (a 16 GiB
   test next to a continent build is how the host OOMs).
2. Remove `$HOME/sz-round-over.txt` if it exists.
3. In a **fresh shell that has not sourced `sz-env.sh`** (its
   `TMPDIR` and `GIT_CEILING_DIRECTORIES` must not reach production),
   run exactly the start command in `$HOME/sz-round-paused.txt` (if it
   has none, ask), and append the time to that file.
4. Release `finish_pending_uploads.sh` only if the user says so too.

### 1.1 Look (read-only)

```bash
. "$HOME/sz-env.sh" || exit
G="git -C /storage/streetzim"
$G rev-parse HEAD
$G rev-parse --abbrev-ref HEAD
$G rev-parse --abbrev-ref '@{u}'
$G --no-optional-locks status -sb
$G rev-list --left-right --count '@{u}...HEAD'
$G log --oneline '@{u}..HEAD'
bash "$HOME/sz-round.sh"; echo "sz-round exit $?"
bash "$HOME/sz-busy.sh"; echo "sz-busy exit $?"
cat "$HOME/sz-round-paused.txt" 2>/dev/null
for f in rebuild-old.tsv queue-refresh.tsv; do [ -e "$PROD/$f" ] && { echo "== $f (modified $(stat -c %y "$PROD/$f"))"; tail -5 "$PROD/$f"; }; done
tail -3 "$PROD/rebuild-old.log" 2>/dev/null
grep -hP '^washington-dc\t' "$PROD/rebuild-old.tsv" "$PROD/queue-refresh.tsv" 2>/dev/null || echo "washington-dc: no row in either results file"
crontab -l
grep -l -e build-region-fast -e create_osm_zim_leaflet -e upgrade_spatial_zim "$PROD"/.*.sh 2>/dev/null
df -h /storage /storage/streetzim/tmp / "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
free -g; swapon --show
$G config --get-all remote.origin.fetch
wc -l "$PROD/pending-uploads.tsv" 2>/dev/null || echo "no pending uploads"
$G cat-file -e HEAD:ops/check_stage1.sh 2>/dev/null && bash "$PROD/ops/check_stage1.sh" --root "$PROD" --python "$PY" || echo "no ops/check_stage1.sh at HEAD: the host is not at the split yet (expected before §1.0)"
```

(Every line only reads. Result files are read with `tail`/`grep`, never
opened in an editor.)

**Record:** the commit and branch (a detached `HEAD`: STOP), the upstream,
the "ahead" count and the host's own commits (torrent commits from
`upload_validated.sh`), every `M` line of `status` (expect the in-place
lists, the deploy output of §0, and `?? sz-tests/`), what `sz-round.sh`
and `sz-busy.sh` print, the paused-round notes, crontab lines, disk
(`/storage`, `/`, the docker filesystem), memory and swap (on 29 September
swap was 16/16 GB full, from another tenant), the fetch refspec (a line
other than `+refs/heads/*:refs/remotes/origin/*`: say so, §1.4 needs
it), pending uploads, the round's last rows and D.C.'s row, and the
checker's output once the host is at the split (expect `ALL CHECKS
PASSED` apart from the deploy output; its step 5 is
`tools/check_boundary.py`, and its step 6 lists our test processes too).

- **Ahead is not 0:** the host has commits the branches lack. The moves
  STOP until they are on `origin` and merged into `builder` and `next`
  (§1.0 steps 1–2).
- **Host scripts that call `build-region-fast.sh`** without
  `OVERTURE_RELEASE`: on `next` they refuse to start; on `main`, the ops
  branch and `builder` they default to `2026-04-15.0` and silently lose
  Overture (no parquet of that release). On 29 September:
  `.build-africa-light.sh`, `.queue-phase2.sh`, `.queue-africa-rest.sh`,
  `.queue-rebuild-buggy.sh`, `.queue-europe-countries.sh`,
  `.queue-asia-russia.sh`, `.carolinas-fast-ship.sh`. List any change
  for the user. `next` deletes `create_osm_zim_leaflet.py` and
  `cloud/upgrade_spatial_zim.py`; any host script or cron line naming
  them: report.

### 1.2 The test clone and the branch facts (no change to production)

```bash
. "$HOME/sz-env.sh" || exit
mkdir -p "$SZT/tmp" "$SZT/results"
[ -d "$SZT/next/.git" ] || git clone -q /storage/streetzim "$SZT/next"
git -C "$SZT/next" fetch -q "$(git -C /storage/streetzim remote get-url origin)" +refs/heads/main:refs/remotes/gh/main +refs/heads/claude/adoring-dijkstra-i2vge7:refs/remotes/gh/ops +refs/heads/claude/adoring-dijkstra-i2vge7-builder:refs/remotes/gh/builder +refs/heads/claude/adoring-dijkstra-i2vge7-next:refs/remotes/gh/next
git -C "$SZT/next" switch -q --detach gh/next
git -C "$SZT/next" log -1 --format='%H %s'
for p in "gh/main gh/ops" "gh/ops gh/builder" "gh/builder gh/next" "origin/HEAD gh/builder" "origin/HEAD gh/next"; do set -- $p; git -C "$SZT/next" merge-base --is-ancestor "$1" "$2" && echo "OK    $1 is in $2" || echo "STOP  $1 is not in $2"; done
for m in e94474cde8205fdee1e7a303eb5a1f274eb07db5:topic-wiki-429 c98ab071e9d034a2473c442817b3e0ae1e359e06:topic-host-runbook 2a80be9839da3f5b6fed4282140a0be42d676170:topic-satellite-optin f66c028df93ce1febbf743fa24d17ef6927ebfa2:topic-viewer-polish bb5b9923327910f030bf1d7290e7f804a8fc1f38:topic-mbtiles-flag 3cd8e8e7ce90cd2f9e86df792bd8ff9daf9cd923:topic-full-profile 82def4f489d810e95c2447ac74d352c2b0cd3a52:topic-terrain-openzim; do git -C "$SZT/next" merge-base --is-ancestor "${m%%:*}" gh/next 2>/dev/null && echo "OK       ${m#*:} (${m:0:7})" || echo "MISSING  ${m#*:} (${m:0:7})"; done
```

(`origin/HEAD` in the clone is the host's commit when the clone was
made.) To refresh the clone later, run
`git -C "$SZT/next" fetch -q origin` (so `origin/HEAD` follows the host),
then the block again (it skips the clone).

**Expect:** `gh/next` is `82def4f…` (or later); the first three ancestry
lines `OK`; all seven topic lines `OK`. The two `origin/HEAD` lines:
- now (the host at `21b3ffd`, two torrent commits beyond `37403b8`):
  `STOP` until the host's branch is merged into `builder` and `next`
  (§1.0 step 2);
- after §1.0 (the host on `main`'s merge commit `M`): `STOP` until the
  branch owner merges the new `main` into `builder`, then `builder` into
  `next`.

Report the lines as they are; a `STOP` here stops only the production
moves (which wait for the end of the round anyway), not §1.3 or the
clone-only tests (§2a, 2b, 2d–2g).

### 1.3 Checks in the clone

```bash
. "$HOME/sz-env.sh" || exit
bash "$SZT/next/ops/check_stage1.sh" --root "$SZT/next" --python "$PY"
"$PY" -I "$SZT/next/tools/check_boundary.py" --root "$SZT/next"
"$PY" -m pytest -q -p no:cacheprovider "$SZT/next/ops/tests" "$SZT/next/tests/test_check_boundary.py"
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
. "$HOME/sz-env.sh" || exit
G="git -C /storage/streetzim"
DEPLOY='web/index\.html|web/drive/build-info\.js|web/drive/sw\.js|web/drive/viewer/\.version'
KEEP='[^/]+\.list|viewer-refresh\.tsv|cloud/region-variants\.tsv|tmp/live-inventory\.out'
if bash "$HOME/sz-round.sh"; [ $? -ne 1 ]; then
  echo "STOP: the round is not over (above); production moves wait for its end (§1.0)"
elif bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then
  echo "STOP: the processes above are running (or sz-busy.sh failed); move between them"
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
elif [ -n "$($G diff --cached --name-only)" ]; then
  echo "STOP: staged changes in production's index; ask:"; $G diff --cached --name-only
elif o=$($G --no-optional-locks diff --name-only HEAD | grep -v -x -E "$DEPLOY|$KEEP"); [ -n "$o" ]; then
  echo "STOP: tracked files modified that are neither deploy output nor host-edited; ask:"; echo "$o"
elif c=$($G --no-optional-locks diff --name-only HEAD | sort | comm -12 - <($G diff --name-only HEAD "refs/remotes/origin/$T" | sort)); echo "$c" | grep -q -v -x -E "$DEPLOY|"; then
  echo "STOP: host-edited files that the move changes; ask:"; echo "$c"
elif u=$($G diff --name-only --diff-filter=A HEAD "refs/remotes/origin/$T" | while read -r f; do [ -e "/storage/streetzim/$f" ] || [ -L "/storage/streetzim/$f" ] && echo "$f"; done); [ -n "$u" ]; then
  echo "STOP: untracked or ignored files where $T adds tracked ones (the pull would refuse, or overwrite ignored ones); ask:"; echo "$u"
elif bash "$HOME/sz-busy.sh" >/dev/null; [ $? -ne 1 ]; then
  echo "STOP: something started meanwhile; run the block again later"
else
  if [ -n "$c" ]; then   # deploy output the move changes: save it, then restore exactly those files
    K=$SZT/results/deploy-output-$(date +%Y%m%d-%H%M%S) &&
    mkdir -p "$K" && (cd /storage/streetzim && cp -p --parents $c "$K"/) &&
    $G restore --source=HEAD --worktree -- $c &&
    echo "saved to $K and restored to HEAD: $c" || { echo "STOP: saving or restoring the deploy output failed; ask"; exit 1; }
  fi
  { $G rev-parse --abbrev-ref HEAD && $G rev-parse HEAD; } > "$R.before.new" && mv "$R.before.new" "$R.before" &&
  $G switch -q -c "$L" &&
  $G branch -q --set-upstream-to="origin/$T" "$L" &&
  $G -c merge.autoStash=false -c rebase.autoStash=false pull -q --ff-only --no-rebase &&
  $G rev-parse HEAD > "$R.after" &&
  if [ "$(cat "$R.after")" = "$($G rev-parse "refs/remotes/origin/$T")" ]; then echo "OK: on $L at $T ($(cat "$R.after"))"
  else echo "STOP: pulled, but not to $T's commit; don't run this block again; ask"; fi ||
  echo "STOP: the move did not finish; run: $G log -1; $G --no-optional-locks status -sb; ask"
fi
```

`switch -c` creates `$L` at the current commit, so nothing on disk changes
until the pull, and the old branch is left untouched for rollback. The
host-edited lists are identical on every branch here, so their edits carry
over. The checks before the switch stop the move whenever a locally
modified file would make the pull refuse, because it would refuse
**after** the switch, leaving the host on `$L` at the old commit with
`$R.before` written, and later torrent commits landing on `$L`.

**Deploy output** (the policy, from the scripts). Four tracked files are
rewritten by every upload's deploy and by nothing else:
`web/generate.py` regenerates `web/index.html` from `web/template.html`
and archive.org's listing, and Firebase's predeploy
(`scripts/sync-drive-viewer.sh`) rewrites `web/drive/build-info.js` (the
build time and stamp), `web/drive/viewer/.version` (the stamp; not even
deployed) and the `SHELL_CACHE` line of `web/drive/sw.js`. Their
committed contents are never what the site serves, and the next deploy
rewrites all four from the checkout it runs in. So before a move that
changes any of them, the block copies exactly those files to
`$SZT/results/deploy-output-<time>/` and restores them to `HEAD`, then
moves; the next upload's deploy regenerates them from the new code. It
stops instead if any other tracked file is modified (apart from the
host-edited lists, which no move changes), if anything is staged, or if
an untracked or ignored file sits where the target adds a tracked one.
`git restore` needs git 2.23 or later (§1.0's checks print the version;
`switch` needs the same). On an older git, stop and ask; don't substitute
`checkout --`.
Never commit them on the host: that makes the `main` → `builder` →
`next` merges conflict in `build-info.js`. (`web/sitemap.xml` is tracked
but nothing writes it, so it is not deploy output.) Taking the four out
of git is the lasting fix, a follow-up for a development machine, not
the host: `tests/viewer_ui_js.test.mjs` and `cloud/deploy_pwa.sh` read
them, and `sw.js` is source apart from its stamp line.

At `82def4f`, `next` changes all four (and `web/template.html`); `builder`
changes none of them (`web/drive/viewer/index.html` only). If the pull
still refuses after the switch (a file the checks couldn't foresee), the
block prints the last STOP line with the checkout on `$L` at the old
commit. §1.7 doesn't apply (there is no `.after`); report
`git -C /storage/streetzim rev-parse HEAD` (it must equal line 2 of
`$R.before`) and ask. The way back, on the user's word, is
`git -C /storage/streetzim switch -q "$(sed -n 1p "$R.before")"`, while
`sz-busy.sh` prints `IDLE`.

If `--set-upstream-to` fails ("not a branch" or similar: the host's fetch
refspec, recorded in §1.1, doesn't map `origin/$T`), the same applies.

### 1.5 After each move (read-only)

```bash
. "$HOME/sz-env.sh" || exit
bash "$PROD/ops/check_stage1.sh" --root "$PROD" --python "$PY"
git -C /storage/streetzim --no-optional-locks status -sb
ps -eo pid,lstart,args | grep -E '/storage/streetzim/.*\.(sh|py|mjs)' | grep -v -e grep -e "$SZT"
"$PY" "$PROD/create_osm_zim.py" --help > /dev/null && echo "OK: builder imports"
"$PY" "$PROD/cloud/serve_zims.py" --help > /dev/null && echo "OK: ops python by old path"
"$PY" -c 'import regex' 2>/dev/null && echo "regex present" || echo "regex missing (only the streetzim command's metadata flags need it; installing requirements-ops.txt is the user's call)"
```

**Pass:** `ALL CHECKS PASSED` (including "no local commits" and the
boundary check; the only acceptable `FAIL local change` lines are deploy
output that §1.1 showed too), the same `M` lines on the host-edited lists
as in §1.1, the §1.1 PIDs still running (check_stage1's step 6 also lists
processes under `$SZT`: ours, leave them out of the comparison), both
`OK` lines. Then run §2c on this branch.

### 1.6 Later commits on the same branch

When `next` gains more topic branches, update `host-next` by pull, between
builds:

```bash
. "$HOME/sz-env.sh" || exit
R=$HOME/sz-pull-next-$(date +%Y%m%d-%H%M)
G="git -C /storage/streetzim"
DEPLOY='web/index\.html|web/drive/build-info\.js|web/drive/sw\.js|web/drive/viewer/\.version'
KEEP='[^/]+\.list|viewer-refresh\.tsv|cloud/region-variants\.tsv|tmp/live-inventory\.out'
if bash "$HOME/sz-round.sh"; [ $? -ne 1 ]; then echo "STOP: the round is not over"
elif bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then echo "STOP: a build, gate or upload is running (or sz-busy.sh failed)"
elif [ "$($G rev-parse --abbrev-ref HEAD)" != host-next ]; then echo "STOP: not on host-next"
elif ! $G fetch -q origin +refs/heads/claude/adoring-dijkstra-i2vge7-next:refs/remotes/origin/claude/adoring-dijkstra-i2vge7-next; then echo "STOP: fetch failed; ask"
elif [ -n "$($G diff --cached --name-only)" ]; then echo "STOP: staged changes in production's index; ask"
elif o=$($G --no-optional-locks diff --name-only HEAD | grep -v -x -E "$DEPLOY|$KEEP"); [ -n "$o" ]; then
  echo "STOP: tracked files modified that are neither deploy output nor host-edited; ask:"; echo "$o"
elif c=$($G --no-optional-locks diff --name-only HEAD | sort | comm -12 - <($G diff --name-only HEAD '@{u}' | sort)); echo "$c" | grep -q -v -x -E "$DEPLOY|"; then
  echo "STOP: host-edited files that the pull changes; ask:"; echo "$c"
elif u=$($G diff --name-only --diff-filter=A HEAD '@{u}' | while read -r f; do [ -e "/storage/streetzim/$f" ] || [ -L "/storage/streetzim/$f" ] && echo "$f"; done); [ -n "$u" ]; then
  echo "STOP: untracked or ignored files where the pull adds tracked ones; ask:"; echo "$u"
else
  if [ -n "$c" ]; then   # deploy output the pull changes: save, then restore exactly those (§1.4)
    K=$SZT/results/deploy-output-$(date +%Y%m%d-%H%M%S) &&
    mkdir -p "$K" && (cd /storage/streetzim && cp -p --parents $c "$K"/) &&
    $G restore --source=HEAD --worktree -- $c &&
    echo "saved to $K and restored to HEAD: $c" || { echo "STOP: saving or restoring the deploy output failed; ask"; exit 1; }
  fi
  $G rev-parse HEAD > "$R.before" &&
  $G -c merge.autoStash=false -c rebase.autoStash=false pull -q --ff-only --no-rebase &&
  $G rev-parse HEAD > "$R.after" && echo "OK: $(cat "$R.before") -> $(cat "$R.after") (records $R.*)" ||
  echo "STOP: the pull refused (host commits, or a local edit in the way); nothing changed; ask"
fi
```

Then §1.5 again. To go back from such a pull, only if HEAD is still
`$R.after` and `sz-busy.sh` prints `IDLE`:
`git -C /storage/streetzim switch -q -c host-rollback-<date> <the .before commit>`,
then ask.

### 1.7 Rollback of a move

Only if HEAD is still the commit the move reached (a host commit since,
such as a torrent, makes the block stop: switching back would take that
file off the disk), and in an idle window. Moves happen only after the
round (§1.4), so their rollbacks do too; mid-round, the only move is the
stage-1 pull, whose rollback is in §1.0.

```bash
R=$HOME/sz-move-builder
. "$HOME/sz-env.sh" || exit
G="git -C /storage/streetzim"
if [ ! -s "$R.before" ] || [ ! -s "$R.after" ]; then
  echo "STOP: the records $R.before/.after are missing; ask"
elif [ "$($G rev-parse HEAD)" != "$(cat "$R.after")" ]; then
  echo "STOP: HEAD moved since the move:"; $G log --oneline -3; echo "ask"
elif bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then
  echo "STOP: the processes above are running (or sz-busy.sh failed); roll back between them"
else
  $G switch -q "$(sed -n 1p "$R.before")" &&
  [ "$($G rev-parse HEAD)" = "$(sed -n 2p "$R.before")" ] &&
  echo "ROLLED BACK to $(sed -n 1p "$R.before") $(sed -n 2p "$R.before")" ||
  echo "STOP: the switch refused or landed elsewhere; ask"
fi
```

`switch` keeps local edits (the host-edited lists) and refuses if one
would be overwritten. The new local branch stays; don't delete it. Then
§1.5, and report.

## 2. Tests on the host

Results go to `$SZT/results/<test>/`. Each test lists what it needs;
§2c changes the production checkout's data directory only by the files
it names. Every block sources `sz-env.sh`, so `TMPDIR` is `$SZT/tmp` and
`GIT_CEILING_DIRECTORIES` is set.

**Order** (the user's, 29 September): §2a and §2b are done (§2a to rerun
at `82def4f`); next **§2d** (D.C. with Wikipedia images), then **§2e**
(Switzerland head-to-head), then §2f (Alaska) and §2g (Massachusetts);
§2h (the Luxembourg/D.C. matrix) is optional after that. §2c waits for
the end of the round. §2d–2h run while the round is paused, never while
it runs.

### 2.0 Preparation (once)

The StreetZim image, from the clone's current `next` (for §2a tilemaker
mode, §2e–2h); record its commit and ID:

```bash
. "$HOME/sz-env.sh" || exit
docker build -q -t streetzim:hosttest "$SZT/next" > "$SZT/results/image-id.txt"
git -C "$SZT/next" rev-parse HEAD >> "$SZT/results/image-id.txt"
docker run --rm streetzim:hosttest tilemaker --help 2>&1 | head -2
df -h "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
```

(The image's tilemaker, built from source, prints `version not set`; that
is expected, and it is tilemaker 3.) Rebuild the image after refreshing
the clone (§1.2), and note the new commit. The host's first image was
built from `c98ab07`; §2e–2h need one from `82def4f` or later
(`--mbtiles-url`, `--profile`).

### 2a. Golden builds, Monaco ([docs/golden-builds.md](../docs/golden-builds.md))

OpenFreeMap tiles (the strict mode). Each run is three Monaco builds,
minutes in all:

```bash
. "$HOME/sz-env.sh" || exit
for pair in "gh/main gh/ops main-ops" "gh/main gh/builder main-builder" "gh/main gh/next main-next" "gh/next gh/next next-next"; do
  set -- $pair
  PYTHON="$PY" bash "$SZT/next/tools/golden_builds.sh" "$1" "$2" "$SZT/golden/$3" > "$SZT/results/golden-$3.txt" 2>&1; echo "$3 exit $?"
done
```

**Expected** (measured in a sandbox on 29 September with Monaco inputs,
on `bd09db9`, `builder` `3d8e7d7` and `next` `d5238cb`, and **confirmed
on the host with `next` at `c98ab07`**: exactly this table):

| pair | exit | `changed` | `only-before` / `only-after` |
|---|---|---|---|
| main → ops | 0 | 0 | 0 / 0 (the ops split changes no build output) |
| main → builder | 1 | `C/index.html`, `M/License` (CC BY-SA 4.0) | 0 / 0 |
| main → next | 1 | `C/index.html`, `C/map-config.json` (adds `description`, `generator`, `rtlTextPlugin`, `title`), `C/places.html`, `C/routing-worker.js`, `M/License`, `M/Counter` | 0 / `C/mapbox-gl-rtl-text.js` |
| next → next | 0 | 0 | 0 / 0 |

`reordered` varies between runs (0–38 seen) and is fine. **FAIL:** any
other `changed`, `only-before` or `only-after` path, or `noise` above 0.
Look at each such entry as the golden-builds page describes, and report
it.

**At `82def4f`** five merges came in after `c98ab07` (the table at the
top), all touching the builder: `create_osm_zim.py` and
`streetzim/zim_writer.py` (each), the viewer (`resources/viewer/`:
satellite-optin, viewer-polish, mbtiles-flag, terrain-openzim),
`streetzim/zim_metadata.py` (satellite-optin). Rerun `main → next`; each
path beyond the table must be explained by one of them (for example the
viewer's `C/index.html`/`C/places.html` differ more, which the table
already lists). Report every new path with the merge it belongs to; one
that belongs to none: FAIL.

(§2a writes only under `$SZT`; it needs no idle window, but it does load
the host: run it when `sz-busy.sh` shows no continent-tier build.)

**tilemaker mode.** The host has no tilemaker binary and no `unzip` (and
no sudo to install either), so both run inside the image. Everything
tilemaker reads must be under `$SZT`, hence `TMPDIR` there (set by
`sz-env.sh`). The shapefiles are fetched inside the image (864 MB of
downloads); alternatively, on the user's word, copy production's
`coastline/` and `landcover/` read-only into the inputs folder instead.

```bash
. "$HOME/sz-env.sh" || exit
W=$SZT/golden/tm-main-next
mkdir -p "$SZT/bin" "$W/inputs" "$SZT/tmp"
printf '%s\n' '#!/bin/sh' ". \"\$HOME/sz-env.sh\"" 'exec docker run --rm --user "$(id -u):$(id -g)" -v "$SZT:$SZT" -w "$PWD" streetzim:hosttest tilemaker "$@"' > "$SZT/bin/tilemaker"
chmod +x "$SZT/bin/tilemaker"
docker run --rm --user "$(id -u):$(id -g)" -e TMPDIR="$SZT/tmp" -v "$SZT:$SZT" -w "$SZT" streetzim:hosttest bash "$SZT/next/scripts/fetch-shapefiles.sh" "$W/inputs"; echo "shapefiles exit $?"
ls "$W/inputs/coastline" "$W/inputs/landcover"
PATH=$SZT/bin:$PATH PYTHON="$PY" bash "$SZT/next/tools/golden_builds.sh" --tilemaker gh/main gh/next "$W" > "$SZT/results/golden-tm-main-next.txt" 2>&1; echo "exit $?"
"$PY" "$SZT/next/tools/golden_diff.py" "$W/before/monaco.zim" "$W/after/monaco.zim" --control "$W/control/monaco.zim" --decode-tiles --coord-tolerance 0.0001 --show 500 > "$SZT/results/golden-tm-main-next-all.txt"; echo "diff exit $?"
```

Then the per-record check of every changed JSON entry (the search records
are paired by all their fields but `s`):

```bash
. "$HOME/sz-env.sh" || exit
W=$SZT/golden/tm-main-next
"$PY" - "$W/before/monaco.zim" "$W/after/monaco.zim" <<'EOF' | tee "$SZT/results/golden-tm-records.txt"
import ast, json, os, sys
from collections import Counter
from libzim.reader import Archive
src = open(os.path.join(os.environ["SZT"], "next/streetzim/search_extract.py")).read()
RAW = next(set(ast.literal_eval(n.value.args[0])) for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "RAW_OSM_KEY_CLASSES")
def jsons(path):
    a, out = Archive(path), {}
    for i in range(a.all_entry_count):
        e = a._get_entry_by_id(i)
        if not e.is_redirect and e.get_item().mimetype.startswith("application/json"):
            out[e.path] = bytes(e.get_item().content)
    return out
def recs(x, acc):
    if isinstance(x, dict) and "n" in x and ("t" in x or "s" in x):
        acc.append(x)
    elif isinstance(x, dict):
        for v in x.values(): recs(v, acc)
    elif isinstance(x, list):
        for v in x: recs(v, acc)
    return acc
key = lambda r: json.dumps({k: v for k, v in r.items() if k != "s"}, sort_keys=True)
before, after = jsons(sys.argv[1]), jsons(sys.argv[2])
per_prefix, pairs, other, nonrec = Counter(), Counter(), [], []
for p in sorted(set(before) & set(after)):
    if before[p] == after[p]:
        continue
    ja, jb = json.loads(before[p]), json.loads(after[p])
    ra, rb = recs(ja, []), recs(jb, [])
    if not ra and not rb:
        nonrec.append(p); continue
    ga, gb = {}, {}
    for r in ra: ga.setdefault(key(r), Counter())[r.get("s")] += 1
    for r in rb: gb.setdefault(key(r), Counter())[r.get("s")] += 1
    subst, bad = {}, False
    for k in ga.keys() | gb.keys():
        gone, new = ga.get(k, Counter()) - gb.get(k, Counter()), gb.get(k, Counter()) - ga.get(k, Counter())
        if not gone and not new:
            continue
        if (sum(gone.values()) == sum(new.values()) and all(s in RAW for s in gone)
                and all(s not in RAW for s in new)):
            for so, sn in zip(sorted(gone.elements()), sorted(new.elements())):
                pairs[(so, sn)] += 1
            if len(gone) == 1 and len(new) == 1:
                subst[(k, next(iter(gone)))] = next(iter(new))
        else:
            bad = True
            other.append(f"{p}: record {k[:200]}: s {dict(gone)} -> {dict(new)}")
    if bad:
        continue
    def fix(x):
        if isinstance(x, dict) and "n" in x and ("t" in x or "s" in x):
            s = subst.get((key(x), x.get("s")))
            return {**x, "s": s} if s is not None else x
        if isinstance(x, dict): return {k: fix(v) for k, v in x.items()}
        if isinstance(x, list): return [fix(v) for v in x]
        return x
    if json.dumps(fix(ja), sort_keys=True) != json.dumps(jb, sort_keys=True):
        other.append(f"{p}: the same records apart from s, but the file differs otherwise (order or other fields): look")
        continue
    per_prefix[p.split("/")[0] if "/" in p else p] += 1
print("changed paths that differ only in s (raw key -> subclass), by prefix:", dict(per_prefix))
print("records, by (s before -> s after):")
for (so, sn), n in pairs.most_common():
    print(f"  {so:10s} -> {sn:20s} {n}")
print("JSON without search records that differs (each must be in the OpenFreeMap run's changed list):", nonrec)
print("OTHER differences:" if other else "OTHER differences: none")
for line in other:
    print("  " + line)
EOF
```

**Expected:** `golden_builds.sh` exits 1 with the OpenFreeMap run's
`changed` and `only-after` set, plus any number of `tiles-equal` and
`moved` (largest shift at most 0.0001°), **plus the search-record
changes of `topic-poi-type`** (`a052a87`, `477afa4`, on `next` only, via
`668de77`): tilemaker's profile writes the raw OSM key as a POI's class
for values without an OpenMapTiles class, so `main` stores `s:"amenity"`
(or `tourism`, `leisure`, …) where `next` stores the subclass
(`restaurant`, …). On the host at `c98ab07`: 161 changed entries, the six
of the table plus 148 `search-data/…` and 7 `category-index/…`, differing
only in `s`. The record check must print those paths under "differ only
in `s`", every record pair as a raw key (`amenity`, `barrier`,
`building`, `highway`, `historic`, `landuse`, `leisure`, `sport`,
`tourism`, `waterway`) → a subclass, the JSON-without-records list within
the table's paths, and `OTHER differences: none`. **FAIL:** a tile in
`changed` (`next` doesn't change the tilemaker profile), or any `OTHER`
line. Report the (before → after) counts: they show what the shipped
tilemaker ZIMs miss today (on the host, 4–5 % of POI records carry a
raw-key `s`, which the Find chips don't show).

### 2b. Overture over `s3://` at the pinned release

The first run of `next`'s downloader with `--transport s3` on the host,
into test files, compared with the round's cached parquets. Uses D.C.;
if `overture_cache/` has no D.C. file for 2026-08-19.0, use the smallest
region that has one (`ls -S -r /storage/streetzim/overture_cache/places-*-2026-08-19.0.parquet | head -3`)
with its bbox from `cloud/regions.tsv`, and say so.

**The pinned release won't last.** Overture keeps only a few releases in
the bucket: on 29 September `2026-08-19.0`, `2026-09-23.0` and
`2026-09-23.1`. `2026-08-19.0` disappears at the next release (about late
October), and then every download below fails in a way that looks like a
transport FAIL. So check the listing first:

```bash
. "$HOME/sz-env.sh" || exit
mkdir -p "$SZT/results/2b"
(cd "$SZT/next" && "$PY" -c 'import download_overture_data as d; r = d.list_releases(); print(r); print("OK: 2026-08-19.0 is listed" if "2026-08-19.0" in r else "SKIP: 2026-08-19.0 is gone from the bucket; not a transport failure; ask")')
cat "$PROD/overture_cache/places-washington-dc-2026-08-19.0.parquet.bbox" 2>/dev/null
for tr in s3 https; do for t in addresses places; do "$PY" "$SZT/next/download_overture_data.py" $t --bbox=-77.12,38.79,-76.91,38.99 --release 2026-08-19.0 --transport $tr --out "$SZT/results/2b/$t-$tr.parquet" > "$SZT/results/2b/$t-$tr.log" 2>&1; echo "$t $tr exit $?"; tail -1 "$SZT/results/2b/$t-$tr.log"; done; done
```

(Run the downloads only after the `OK` line; on `SKIP`, stop and ask
which release to pin instead: the comparison with the round's cache then
no longer applies.)

Compare (ids both ways, rows, columns, the release stamp):

```bash
. "$HOME/sz-env.sh" || exit
(cd "$SZT/next" && "$PY" - <<'EOF') | tee "$SZT/results/2b/compare.txt"
import os
import duckdb
from streetzim.overture import parquet_release
d = os.path.join(os.environ["SZT"], "results/2b")
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

**Pass** (the host passed it on 29 September): all four downloads exit 0,
and each log's last line names `release 2026-08-19.0`; the release stamp
is `2026-08-19.0`; against the https files and the cache, the same row
count and 0 ids only on either side. Columns: `places` from `next`
carries `categories`, `taxonomy` and `basic_category` (2026-08-19.0 has
all three); a cache file written by the old downloader lacks the last
two. That, and nothing else, is expected. **FAIL:** a failed s3 download
while the release is listed (report the log: this is the transport the
wrappers use), or any id difference (first check the cache file's `.bbox`
sidecar printed above: another bbox explains it).

### 2c. A small region end to end, without uploading (production checkout)

Runs after each move (§1.5), on the branch the host is then on, and only
once the round is over. It runs the production wrappers, which
`cd /storage/streetzim` and use its code, caches and `overture_cache/`.

**What keeps a test from publishing:**

| script | uploads or deploys? | test flags |
|---|---|---|
| `build-region-fast.sh` | never; writes `osm-<id>-<date>.zim` in `/storage/streetzim` | on `next` needs `OVERTURE_RELEASE` (refuses without it, and refuses `latest`); elsewhere defaults to `2026-04-15.0`, so always pass it |
| `ship-region.sh <id> --no-upload` | not with `--no-upload` (must be the second argument); it stops after the gates. Without it: `cloud/upload_validated.sh` (archive.org, torrent commit, `web/generate.py --deploy`) | |
| `cloud/rebuild_old_regions.sh` | always uploads; no test mode | never run it as a test |
| `build-refresh-queue.sh` | unless `--no-upload` | `--dry-run` builds nothing but still takes the queue lock and appends to `queue-refresh-<date>.log`. With `--no-upload` a good region is recorded `built-ok`, which `--continue` then skips: never run a test against the round's results (set `RESULTS=$SZT/...`) |

**Only after the round is over** (a production build of its own, in
production's directory: it would compete with the round's builds for CPU,
memory and `/storage`, and a D.C. ZIM dated today would be reused and
*uploaded* by a round that reached D.C. later). The block also saves
D.C.'s previous logs and production's `git status` first:
`washington-dc-build.out` has no date in its name and the test
overwrites it.

Use `ship-region.sh` on D.C. (`--no-upload` exists on every branch here,
must be the second argument, and exits 0 right after `=== all gates
passed`, before `cloud/upload_validated.sh`: no archive.org upload, no
torrent, no commit, no `generate.py --deploy`, no Firebase). Run it as
one command:

```bash
. "$HOME/sz-env.sh" || exit
D=$(date +%F); K=$SZT/results/2c-$(git -C /storage/streetzim rev-parse --short HEAD)
if bash "$HOME/sz-round.sh"; [ $? -ne 1 ]; then echo "STOP: the round is not over; §2c waits for its end"
elif bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then echo "STOP: the processes above are running (or sz-busy.sh failed)"
elif ls /storage/streetzim/osm-washington-dc-"$D".zim 2>/dev/null; then echo "STOP: today's D.C. ZIM exists; the wrapper would reuse it"
elif [ -e "$K" ]; then echo "STOP: $K exists (this commit was tested already)"
else
  mkdir -p "$K/round-logs-before" &&
  git -C /storage/streetzim --no-optional-locks status --short > "$K/status-before" &&
  cp -p /storage/streetzim/washington-dc-build.out /storage/streetzim/washington-dc-rebuild-"$D".log /storage/streetzim/washington-dc-smoke-"$D".log /storage/streetzim/ship-washington-dc-"$D".log "$K/round-logs-before/" 2>/dev/null
  ( cd /storage/streetzim && OVERTURE_RELEASE=2026-08-19.0 OVERTURE_TRANSPORT=s3 STAGE_MBTILES_NVME=0 ./ship-region.sh washington-dc --no-upload ); echo "exit $?"
fi
```

(The subshell keeps this session's working directory out of
`/storage/streetzim`. `ship-region.sh` sets its own
`TMPDIR=/storage/streetzim/tmp`. If the script prints `FATAL: … run
./extract-region-pbfs.sh …` or any other FATAL: that is a FAIL; don't run
what it suggests, it writes production's region files.)

(`STAGE_MBTILES_NVME=0`: the round passes it for every build, so the test
runs as the round does. D.C.'s `world-data/regions/washington-dc.mbtiles`
is a regular 19 MB file, not a symlink to the world tiles.)

**Side effects to expect** (and report): `.bbox` sidecars written next to
`world-data/regions/washington-dc.osm.pbf` and D.C.'s Overture parquets
(on `next`); on `next`, if a D.C. parquet's sidecar names another bbox,
the script deletes that parquet and downloads it again over s3; the
Wikidata/Wikipedia caches gain entries; `web/osm-washington-dc-<date>.zim`
exists as a symlink during the browser smoke (Firebase ignores `*.zim`);
new `??` lines for Chromium profile folders under `tmp/` (the browser
gates run with `TMPDIR=/storage/streetzim/tmp`).

**Pass:** exit 0; `ship-washington-dc-<date>.log` ends with `=== all gates
passed` and `--no-upload: stopping here with osm-washington-dc-<date>.zim`;
`washington-dc-build.out` shows `overture release: 2026-08-19.0
(addr=yes places=yes)` and `wikipedia: bundling articles`; the build used
the Rust packer (`--zim-builder=rust`, a to-do in head-to-head-dc.md);
no `archive.org` line in the log. **FAIL:** anything else; keep the logs.

Then take the test output out of production's way (a later round would
otherwise reuse today's ZIM) and compare production's status:

```bash
. "$HOME/sz-env.sh" || exit
D=$(date +%F); K=$SZT/results/2c-$(git -C /storage/streetzim rev-parse --short HEAD)
if bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then echo "STOP: the processes above are running; move the ZIM out once they end"
elif [ ! -d "$K" ]; then echo "STOP: $K is missing (HEAD moved since the run?); use the run's folder by hand"
else
  mv /storage/streetzim/osm-washington-dc-"$D".zim "$K"/ &&
  cp /storage/streetzim/ship-washington-dc-"$D".log /storage/streetzim/washington-dc-build.out /storage/streetzim/washington-dc-rebuild-"$D".log /storage/streetzim/washington-dc-smoke-"$D".log "$K"/
  git -C /storage/streetzim --no-optional-locks status --short > "$K/status-after"
  diff "$K/status-before" "$K/status-after"
  grep -a '^Temp files kept at: ' /storage/streetzim/washington-dc-rebuild-"$D".log | tail -1
fi
```

The `diff` must show nothing but new `?? tmp/…` Chromium folders.
Delete the directory the last line names, only if it is under
`/storage/streetzim/tmp/osm_zim_` (`rm -rf <that directory>`). If the
build crossed midnight, use the date in the ZIM's name instead of
`$(date +%F)`.

### 2d. D.C. with offline Wikipedia, `main` vs `next`

**`next` contains `topic-wiki-429`** (`e94474c`; §1.2 checks it). The
comparison [head-to-head-dc.md](../docs/head-to-head-dc.md#what-is-still-not-compared)
asks for: images and the offline-ZIM path, from production's Wikipedia
ZIM. Both sides read the same PBF, the same tiles (`--mbtiles`, D.C.'s
19 MB region file) and the same warm caches, copied under `$SZT`.
Approved (29 September); runs while the round is paused.

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
test "$(stat -c%s /storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim)" = 123980647016 && echo "OK: wiki ZIM" || echo "STOP: wiki ZIM missing or wrong size"
mkdir -p "$SZT/dcwiki/src-main" "$SZT/dcwiki/main" "$SZT/dcwiki/next"
git -C "$SZT/next" archive gh/main | tar -x -C "$SZT/dcwiki/src-main"
du -sh /storage/streetzim/wikidata_cache; df -h /storage
```

The Wikidata cache is about 2.2 GB, and builds rewrite its files **in
place**, so a copy taken while a build writes it can hold a torn file.
Copy it only while no build runs, and check every file afterwards:

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
if bash "$HOME/sz-busy.sh"; [ $? -ne 1 ]; then echo "STOP: a build may be writing wikidata_cache; copy later"
elif [ -e "$SZT/dcwiki/wd" ]; then echo "STOP: $SZT/dcwiki/wd exists; remove it first if a copy failed"
else
  cp -a /storage/streetzim/wikidata_cache "$SZT/dcwiki/wd"
  cp /storage/streetzim/wiki_articles_cache/washington-dc_qid_titles.json "$SZT/dcwiki/titles.json" 2>/dev/null || echo "no D.C. title cache; starts cold"
  bash "$HOME/sz-busy.sh" > /dev/null; [ $? -eq 1 ] && echo "OK: still idle after the copy" || echo "STOP: something started during the copy; remove $SZT/dcwiki/wd and copy again"
  "$PY" - "$SZT/dcwiki/wd" "$SZT/dcwiki/titles.json" <<'EOF'
import json, pathlib, sys
files = sorted(pathlib.Path(sys.argv[1]).rglob("*.json")) + [pathlib.Path(p) for p in sys.argv[2:] if pathlib.Path(p).exists()]
bad = []
for f in files:
    try:
        with open(f) as fh: json.load(fh)
    except Exception as e:
        bad.append(f"{f}: {e}")
print(f"{len(files)} JSON files, {len(bad)} unreadable"); print("\n".join(bad[:20]))
EOF
fi
```

**Pass:** `OK: still idle`, and `0 unreadable`. Otherwise remove the copy
and repeat in another idle window. (Both are copies: the tests never write
production's caches.) Warm the caches with `next` (which no longer
records a 429 as a miss). Run this until two runs in a row print the same
`distinct titles` count and the log has no `429`:

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
[ "$(awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo)" -ge 16 ] || { echo "STOP: under 16 GiB MemAvailable (swap is full)"; exit; }
cd "$SZT/dcwiki/next" && STREETZIM_CACHE_DIR="$SZT/dcwiki/cache" "$PY" "$SZT/next/create_osm_zim.py" --pbf /storage/streetzim/world-data/regions/washington-dc.osm.pbf --mbtiles /storage/streetzim/world-data/regions/washington-dc.mbtiles --bbox=-77.12,38.79,-76.91,38.99 --name "Washington, D.C." --wikidata --wikidata-cache "$SZT/dcwiki/wd" --resolve-wikidata-titles --wikidata-title-cache "$SZT/dcwiki/titles.json" --bundle-wiki-articles --wiki-articles-source /storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim --wiki-images all --wiki-image-max-kb 128 -o "$SZT/dcwiki/next/dc-next.zim" > "$SZT/dcwiki/next/build.log" 2>&1; echo "exit $?"; grep -a -e 'distinct titles' -e 'stored' -e 429 "$SZT/dcwiki/next/build.log" | tail -5
```

Then `main`, same flags and caches, from its exported tree:

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
cd "$SZT/dcwiki/main" && STREETZIM_CACHE_DIR="$SZT/dcwiki/cache" "$PY" "$SZT/dcwiki/src-main/create_osm_zim.py" --pbf /storage/streetzim/world-data/regions/washington-dc.osm.pbf --mbtiles /storage/streetzim/world-data/regions/washington-dc.mbtiles --bbox=-77.12,38.79,-76.91,38.99 --name "Washington, D.C." --wikidata --wikidata-cache "$SZT/dcwiki/wd" --resolve-wikidata-titles --wikidata-title-cache "$SZT/dcwiki/titles.json" --bundle-wiki-articles --wiki-articles-source /storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim --wiki-images all --wiki-image-max-kb 128 -o "$SZT/dcwiki/main/dc-main.zim" > "$SZT/dcwiki/main/build.log" 2>&1; echo "exit $?"; grep -a -e 'distinct titles' -e 'stored' -e 429 "$SZT/dcwiki/main/build.log" | tail -5
```

If `main`'s log shows any new fetch or `429`, its inputs differ: run the
`next` build once more, then `main` again, and compare only a pair with no
fetches. Compare:

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
"$PY" "$SZT/next/tools/golden_diff.py" "$SZT/dcwiki/main/dc-main.zim" "$SZT/dcwiki/next/dc-next.zim" --show 200 > "$SZT/results/dcwiki-diff.txt"; echo "exit $?"
ls -l /storage/streetzim/osm-washington-dc-20*.zim
"$PY" - "$SZT/dcwiki/main/dc-main.zim" "$SZT/dcwiki/next/dc-next.zim" $(ls -t /storage/streetzim/osm-washington-dc-20*.zim 2>/dev/null | head -1) <<'EOF' | tee "$SZT/results/dcwiki-counts.txt"
import os, sys
from collections import Counter
from libzim.reader import Archive
for f in sys.argv[1:]:
    a = Archive(f); n = Counter()
    for i in range(a.all_entry_count):
        p = a._get_entry_by_id(i).path
        for pre in ("wiki-article/", "wiki-image/"):
            if pre in p: n[pre] += 1
    print(f, os.path.getsize(f), dict(n), "geo-index" if a.has_entry_by_path("wiki-geo-index.json") else "no geo-index")
EOF
```

**Pass:** no `wiki-article/` or `wiki-image/` path in `changed`,
`only-before` or `only-after` of the diff; the other differences are the
ones §2a lists (plus `streetzim-meta.json` if its counts differ, which
must then be explained). **FAIL:** any article or image that differs;
report the paths.

The third line is the newest D.C. ZIM on the host. On 29 September that
was `osm-washington-dc-2026-09-06.zim` (225 MB); the published 09-26 file
is not on the host, and the reference counts of 1,551 articles and 5,065
images (the same source ZIM) are the 09-26 file's. Report the compared
file's name, date and counts as they are, and compare with 1,551/5,065
only if it is the 09-26 file.

### 2e. Switzerland head-to-head: maps2zim 0.2.1 vs StreetZim on the same tiles

**Approved by the user on 29 September, right after §2d: Switzerland is
what openZIM knows and cares about.** maps2zim 0.2.1 runs openZIM's
production recipe for Switzerland (same poly, 3 CPUs, 16 GiB), against
StreetZim `--profile full` and `--profile basic` on the same OpenFreeMap
planet tiles. With the round paused, no build competes for the spinning
disk, so this runs now. It must not run while the round runs: the
runner refuses to start a run when `sz-round.sh` prints `RUNNING`, and
if the user resumes the round, the remaining runs don't start. Swap is
full (16/16 GB, another tenant), so the memory caps and floor below stay.

The host has the disk and memory the sandbox lacked. The test follows
the D.C. comparison plan (v2): the same inputs for both tools, the same
measurement, n ≥ 2, and a pre-registered reporting rule. It doesn't gate
the merges (§3). Budget: 3 variants × 2 runs = 6 runs, maps2zim over an
hour each, plus the planet download (~103 GB) once. The Luxembourg and
D.C. runs and the tilemaker variant are optional, after §2f and §2g
(§2h).

**Resources, checked before every run** (the runner does it): this test
shares `/storage` and the host's memory with production. The rounds' own
guards differ: `build-refresh-queue.sh` refuses to *start* below 500 GB
free on `/storage`; `cloud/rebuild_old_regions.sh` has no disk check. Their
continent builds keep 20–120 GB of scratch each and have OOMed without
swap. So:
- disk: the planet (about 103 GB) once, plus per maps2zim run up to about
  105 GB in `tmp/` (openZIM's production Switzerland task peaked at
  208.7 GB of disk *with* its own 103 GB planet download), plus the
  StreetZim cuts, extracts and DEM tiles, plus the ZIMs kept. A run
  starts only with at least **500 GB** free on `/storage`, and is
  aborted (and its round voided) below **300 GB**;
- memory: each container is capped at 16 GiB (swap included); a run
  starts only if `MemAvailable` is at least 24 GiB;
- the round: a run starts only while `sz-round.sh` prints `NOT OVER`
  (paused) or `OVER`, never `RUNNING`;
- a run that overlaps a production build, gate or upload anyway is
  marked `contended` (sampled every 5 s with `sz-busy.sh`, which skips
  containers, so our own runs don't mark each other). Start after
  `build-region-fast.sh brazil` has finished (`sz-busy.sh` prints `IDLE`).

**Variants** (both gates are on `next` since `82def4f`: `--mbtiles-url`
in `bb5b992`, `--profile` in `3cd8e8e`; §1.2 checks them):

| id | what |
|---|---|
| `maps2zim` | `ghcr.io/openzim/maps:0.2.1`, unmodified, with its own planet path (`/tmp/dl/planet.mbtiles`, its default download folder) seeded with the shared planet file |
| `sz-basic` | StreetZim `--profile basic`, tiles from the same planet file (`--mbtiles-url file:///planet/planet.mbtiles`: a `file://` URL is read in place, not copied) |
| `sz-full` | as `sz-basic`, with `--profile full` (Wikidata, Wikipedia, Overture, terrain) |
| `sz-tm` | StreetZim `--profile basic`, its own tilemaker tiles from the Geofabrik extract (optional: §2h) |

Refresh the clone and rebuild the image (§1.2, §2.0), and check the flags'
spelling: `docker run --rm streetzim:hosttest streetzim --help | grep -E -A3 'mbtiles-url|--profile'`.
If `--profile` takes its value differently, change the `PROFILE_*` lines
in the runner below, and say so.

**Region: Switzerland** (Geofabrik's `europe/switzerland.poly`, as the
recipe uses; the user's requirement: openZIM is based there; don't
substitute another country). The runner also knows Luxembourg
(`europe/luxembourg.poly`) and D.C.
(`north-america/us/district-of-columbia.poly`) for §2h. maps2zim runs
exactly as openZIM's production recipes do: the runner's maps2zim command
is the recipe's command (same poly, name, title, description, publisher,
output and stats file; same limits: `cpu: 3` → `--cpu-shares 3072`,
`memory: 17179869184` → `--memory 16g`), checked against the Zimfarm API
on 29 September:

| recipe | poly | latest production task |
|---|---|---|
| `maps_en_switzerland` (annual) | `https://download.geofabrik.de/europe/switzerland.poly` | `625d6b2d-bd75-47ed-ae4f-dba447810e88`, worker `duncan`, 2026-06-12: scraper 72.4 min (06:17:17–07:29:41 UTC), task 73.7 min; ZIM `maps_en_switzerland_2026-06.zim`, 684,493,202 bytes, zimcheck 0; container peaks: memory 17.18 GB (at the 16 GiB cap), CPU max 426 % / avg 77 %, disk 208.7 GB |
| `maps_en_luxembourg` | `https://download.geofabrik.de/europe/luxembourg.poly` | `46f2ea98-8200-4984-81b0-ccfddbf3acee`, worker `badger2`, 2026-06-11: scraper 75.0 min, ZIM `maps_en_luxembourg_2026-06.zim`, 176,649,821 bytes; peaks: memory 17.18 GB, CPU max 132 % / avg 69 %, disk 208.0 GB |
| (none for D.C.: `maps_en_district-of-columbia` and similar return 404) | | |

Refresh these before the report
(`curl -s https://api.farm.openzim.org/v2/recipes/maps_en_switzerland`, and
`/v2/tasks/<id>` of its `most_recent_task`) and cite them under the table
as the real-world reference. They are *not* a run of this test: each
production task downloaded the ~103 GB planet itself (inside its 72–75
min), ran on another machine, and its disk peak includes that planet.

**Fairness and measurement** (the plan's §3, §6 and §8):
- one planet file for every tile-reading run, and the same container
  limits for all (`--memory 16g --memory-swap 16g --cpu-shares 3072`, no
  CPU quota: a quota penalises the multi-process tools);
- the same measurement for all: `tools/measure_build.py` inside the
  container around the tool (wall, CPU, peak PSS and RSS of the process
  tree, peak disk of its folders), `docker stats` every 5 s, `docker
  inspect` (start, finish, `OOMKilled`), and the host's load
  (`/proc/loadavg`, `/proc/pressure/cpu`) at start and end;
- network use differs by design and is listed, not equalised: StreetZim
  `sz-full` fetches Overture and Wikidata/Wikipedia over the network, and
  a 429 there changes its work; record each run's fetch and 429 lines;
- every run starts network-cold except for the planet file: a fresh
  download folder, so GeoNames (maps2zim), and the extract, shapefiles and
  DEM (StreetZim) download as they would on Zimfarm;
- the order within each round rotates (round 1: maps2zim, sz-basic,
  sz-full, sz-tm; round 2: the reverse);
- report the median, the [min–max] and every raw run. Say "A uses less X
  than B" only when the ranges don't overlap **and** the ratio of medians
  is at least 1.5×; otherwise "no clear difference". CPU time is the main
  speed figure; wall time is shown with the load;
- a `contended` run stays out of the headline when a clean pair is
  available;
- every table carries the coverage and feature notes: maps2zim keeps the
  tiles meeting the poly; `sz-basic` and `sz-full` cut the planet's tiles
  to the poly's bbox; `sz-tm` builds tiles from the extract; all
  StreetZim variants take search and routing from Geofabrik's extract,
  which is clipped to the poly. The features differ (StreetZim: search
  over every named feature, routing, a full-text index; maps2zim:
  sprites, the Natural Earth raster, GeoNames search);
- abort a run if `/storage` falls below 300 GB free, and void its round
  (the runner does both).

**The planet tiles, once.** First, what the host's world tiles are (the
round's `WORLD_MB=`, §0). They come from `build-world-tiles.sh`, which
runs tilemaker, so they are expected *not* to be an OpenFreeMap build:

```bash
. "$HOME/sz-env.sh" || exit
"$PY" - "$(sed -n 's/^WORLD_MB=//p' "$PROD/cloud/rebuild_old_regions.sh" | head -1)" <<'EOF'
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
(on 29 September `areas/planet/20260927_080001_pt/tiles.mbtiles`,
102,753,779,712 B):

The download is 103 GB on `/storage` and the image about 1–2 GB on the
docker filesystem (57 GB, 85 % used, shared with another tenant), so the
block checks both first: at least **650 GB** free on `/storage` (the
500 GB run floor after the planet, plus margin) and **10 GB** on the
docker filesystem, and no round running:

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
DR=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
fs=$(df -BG --output=avail "$SZT" | tail -1 | tr -dc 0-9); fd=$(df -BG --output=avail "$DR" | tail -1 | tr -dc 0-9)
echo "free: $fs GB on the test root, $fd GB on docker ($DR)"
[ -e "$SZT/planet/planet.mbtiles" ] || [ "${fs:-0}" -ge 650 ] || { echo "STOP: under 650 GB free for the planet; ask"; exit; }
docker image inspect ghcr.io/openzim/maps:0.2.1 >/dev/null 2>&1 || [ "${fd:-0}" -ge 10 ] || { echo "STOP: under 10 GB free on the docker filesystem; ask"; exit; }
mkdir -p "$SZT/planet"
curl -fsS https://btrfs.openfreemap.com/files.txt | grep -E '^areas/planet/[0-9]+_[^/]+/tiles\.mbtiles$' | sort | tail -1 | tee "$SZT/planet/SOURCE.txt"
curl -fL -C - --retry 5 -o "$SZT/planet/planet.mbtiles" "https://btrfs.openfreemap.com/$(head -1 "$SZT/planet/SOURCE.txt")"
ls -l "$SZT/planet/planet.mbtiles" >> "$SZT/planet/SOURCE.txt"
sha256sum "$SZT/planet/planet.mbtiles" >> "$SZT/planet/SOURCE.txt"
docker pull ghcr.io/openzim/maps:0.2.1
docker image inspect --format '{{json .RepoDigests}}' ghcr.io/openzim/maps:0.2.1 >> "$SZT/planet/SOURCE.txt"
cat "$SZT/results/image-id.txt" >> "$SZT/planet/SOURCE.txt"
```

(If the download stops early, run the block again: `-C -` resumes, and the
floor check is skipped once the file exists; `df` then still shows what
is left. The same docker floor applies to every image rebuild of §2.0:
record the old image ID first and remove that ID after the rebuild, since
the old image stays on the docker filesystem untagged.)

The build id is the `<timestamp>_<suffix>` on the first line of
`SOURCE.txt`. If the download restarted (`-C -`), check the final size
against a fresh `files.txt` listing; if the build rotated out meanwhile,
start over. If you reuse the world tiles instead, write their path, build
id and size into `SOURCE.txt`, and link them:
`ln -s <their path> "$SZT/planet/planet.mbtiles"`.

**The runner,** written once:

```bash
. "$HOME/sz-env.sh" || exit
cat > "$SZT/run-one.sh" <<'EOF'
#!/usr/bin/env bash
# run-one.sh maps2zim|sz-basic|sz-full|sz-tm luxembourg|dc|switzerland N
# Exit: 0 the run finished (container exit 0, not void); 4 it ran but failed
# or was voided; 3 not started (resources, or the round runs); 2 usage.
set -u
. "$HOME/sz-env.sh" || exit 2
tool=$1 region=$2 n=$3
PROFILE_BASIC=(--profile basic)   # the profile flags, as they landed on next (3cd8e8e)
PROFILE_FULL=(--profile full)
case $region in
  luxembourg)  poly=https://download.geofabrik.de/europe/luxembourg.poly; title=Luxembourg ;;
  dc)          poly=https://download.geofabrik.de/north-america/us/district-of-columbia.poly; title="District of Columbia" ;;
  switzerland) poly=https://download.geofabrik.de/europe/switzerland.poly; title=Switzerland ;;  # = recipe maps_en_switzerland
  *) echo "unknown region"; exit 2 ;;
esac
d=$SZT/results/planet/${region}__${tool}__$n
name=cmp-$region-$tool-$n
[ -e "$d" ] && { echo "exists: $d"; exit 2; }
bash "$HOME/sz-round.sh" > /dev/null; rr=$?
[ "$rr" -eq 1 ] || [ "$rr" -eq 3 ] || { echo "not started: the round is running (sz-round.sh exit $rr)"; exit 3; }
free_gb() { df -BG --output=avail "$SZT" | tail -1 | tr -dc 0-9; }   # the test root's filesystem (/storage)
avail_gib() { awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo; }
[ "$(free_gb)" -ge 500 ] || { echo "not started: $(free_gb) GB free on /storage (want >= 500)"; exit 3; }
[ "$(avail_gib)" -ge 24 ] || { echo "not started: $(avail_gib) GiB MemAvailable (want >= 24)"; exit 3; }
docker ps --format '{{.Names}}' | grep -q -E '^(cmp-|sz-test-)' && { echo "not started: another test container runs:"; docker ps --format '{{.Names}}' | grep -E '^(cmp-|sz-test-)'; exit 3; }
mkdir -p "$d/out" "$d/tmp" "$d/dl"
host() { date -Is; cat /proc/loadavg /proc/pressure/cpu 2>/dev/null; free -g | sed -n 2p; bash "$HOME/sz-busy.sh" || true; df -h /storage | tail -1; }
host > "$d/host-start.txt"
# As Zimfarm's worker: cpu 3 -> cpu-shares 3072, memory 16 GiB, swappiness 0
# (it sets no memory-swap; here swap is full, so 16g/16g). --user: without it
# the files would be root's, and this account can't delete them (no sudo).
lim=(-d --name "$name" --user "$(id -u):$(id -g)" -e HOME=/tmp --memory 16g --memory-swap 16g --memory-swappiness 0 --cpu-shares 3072)
desc="Full map, including roads and landmarks"
case $tool in
  maps2zim)
    # The production recipe's command, unchanged. The planet is seeded where
    # maps2zim 0.2.1 looks for it (MAPS_TMP=/tmp in the image, dl=/tmp/dl,
    # area "planet" -> /tmp/dl/planet.mbtiles): it logs "using mbtiles file
    # already available" and skips files.txt and the download.
    docker run "${lim[@]}" -v "$d/out:/output" -v "$d/tmp:/tmp" -v "$d/dl:/tmp/dl" \
      -v "$SZT/planet/planet.mbtiles:/tmp/dl/planet.mbtiles:ro" \
      -v "$SZT/next/tools/measure_build.py:/measure_build.py:ro" \
      ghcr.io/openzim/maps:0.2.1 python3 /measure_build.py --json /output/measure.json \
      --watch /tmp --log /output/build.log -- \
      maps2zim --name "maps_en_$region" --title "$title" --description "$desc" \
      --publisher openZIM --include-poly "$poly" --output /output \
      --stats-filename /output/task_progress.json ;;
  sz-basic|sz-full|sz-tm)
    case $tool in
      sz-basic) extra=("${PROFILE_BASIC[@]}" --mbtiles-url file:///planet/planet.mbtiles) ;;
      sz-full)  extra=("${PROFILE_FULL[@]}" --mbtiles-url file:///planet/planet.mbtiles) ;;
      sz-tm)    extra=("${PROFILE_BASIC[@]}") ;;
    esac
    docker run "${lim[@]}" -v "$d:/work" -e STREETZIM_CACHE_DIR=/work/tmp/cache \
      -v "$SZT/planet/planet.mbtiles:/planet/planet.mbtiles:ro" \
      streetzim:hosttest python /app/tools/measure_build.py --json /work/measure.json \
      --watch /work/tmp --watch /work/dl --watch /work/out --log /work/build.log -- \
      streetzim --name "streetzim_en_$region" --title "$title" --description "$desc" \
      --include-poly "$poly" "${extra[@]}" --output /work/out --tmp /work/tmp --dl /work/dl \
      --stats-filename /work/out/task_progress.json ;;
  *) echo "unknown tool"; exit 2 ;;
esac > "$d/container.id" || { echo "$d: docker run failed"; exit 4; }
# `docker wait` in the background is the end of the run; the loop samples
# until it returns, so one failed `docker stats` or `docker inspect` can't
# end the sampling (or the disk guard) early.
( for i in 1 2 3 4 5 6; do docker wait "$name" > "$d/exit.txt" 2>> "$d/wait.err" && exit 0; sleep 10; done; exit 1 ) &
wp=$!
while kill -0 "$wp" 2>/dev/null; do
  docker stats --no-stream --format '{{.CPUPerc}}\t{{.MemUsage}}\t{{.BlockIO}}\t{{.NetIO}}' "$name" 2>/dev/null | sed "s/^/$(date +%s)\t/"
  bash "$HOME/sz-busy.sh" > /dev/null && date +%s >> "$d/contended.txt"
  if [ "$(free_gb)" -lt 300 ]; then
    echo "$(date -Is) /storage below 300 GB free: killing $name; this round is void" >> "$d/VOID.txt"
    docker kill "$name" > /dev/null 2>&1
  fi
  sleep 5
done > "$d/docker-stats.tsv"
wait "$wp"
docker inspect "$name" > "$d/inspect.json"
docker logs --timestamps "$name" > "$d/docker.log" 2>&1
docker rm "$name" > /dev/null
host > "$d/host-end.txt"
stat -L -c%s "$SZT/planet/planet.mbtiles" > "$d/planet-bytes.txt"
rm -rf "${d:?}/tmp" "${d:?}/dl"
[ -e "$d/VOID.txt" ] && cat "$d/VOID.txt"
rc=$(tr -dc 0-9 < "$d/exit.txt")
echo "$d: exit ${rc:-unknown}"
[ "${rc:-1}" = 0 ] && [ ! -e "$d/VOID.txt" ] && exit 0
exit 4
EOF
bash -n "$SZT/run-one.sh" && echo "OK: runner written"
```

**Deviations from the Zimfarm task, all deliberate:** the planet is
seeded read-only instead of downloaded; `/tmp` is a folder on `/storage`,
not the container's layer (the docker filesystem can't hold 100 GB); the
tool runs under `measure_build.py` and as this account (`--user`, with
`HOME=/tmp`), not root; `--memory-swap 16g` (Zimfarm leaves swap to the
default with swappiness 0). The maps2zim arguments themselves are the
recipe's, unchanged. List these under the table.

maps2zim runs with its image's own paths (`--tmp /tmp`, `--dl /tmp/dl`,
`--output /output`), all mounted from the run folder; its measurement
watches `/tmp`, which holds its download folder. StreetZim's watches its
temp, download and output folders (mounted at `/work`, not over the
container's `/run`). **`measure_build.py` sums every regular file under a
watched folder and doesn't stop at mount points, so maps2zim's
`peak_disk_gb` includes the 103 GB planet bind-mounted at
`/tmp/dl/planet.mbtiles`**; the table below subtracts it
(`planet-bytes.txt`), so both tools count their downloads and working
files but not the shared planet. StreetZim's cut of it lands in its temp
folder and is counted.

**Runs,** one at a time, two rounds with the order reversed, detached so
that they survive the session:

```bash
. "$HOME/sz-env.sh" || exit
mkdir -p "$SZT/results/planet"
setsid nohup bash -c '
for t in maps2zim sz-full sz-basic; do bash "$SZT/run-one.sh" $t switzerland 1; rc=$?; echo "$(date -Is) $t switzerland 1: rc $rc"; [ $rc -eq 3 ] && exit 3; done
for t in sz-basic sz-full maps2zim; do bash "$SZT/run-one.sh" $t switzerland 2; rc=$?; echo "$(date -Is) $t switzerland 2: rc $rc"; [ $rc -eq 3 ] && exit 3; done
' > "$SZT/results/planet/switzerland.out" 2>&1 < /dev/null &
echo "started; follow $SZT/results/planet/switzerland.out"
```

(Exit 3: the run didn't start for want of disk or memory, or because the
round runs again; the loop stops so that the order isn't silently
changed. Exit 4: the run failed or was voided; the loop goes on. Report
both and ask.)

**Per-run checks** (a run failing one is a FAIL for that run; rerun it
once, within its round):
- exit 0 (`run-one.sh` rc 0), and a ZIM in `out/`;
- maps2zim's `build.log` shows `using mbtiles file already available at
  /tmp/dl/planet.mbtiles` (it used the shared planet, not a download). If
  it can't open the read-only file, stop and ask (don't drop `:ro`: the
  same file feeds every run, and a write to it would void them all);
- no `VOID.txt` in the run folder;
- the `sz-basic` and `sz-full` logs show `Cut to the area: N tiles`;
- the `sz-full` log shows the terrain being built; the `sz-tm` log shows
  tilemaker;
- `inspect.json` has `"OOMKilled": false`.

**The table:**

```bash
. "$HOME/sz-env.sh" || exit
"$PY" - <<'EOF' | tee "$SZT/results/planet/TABLE.md"
import glob, json, os, statistics
from collections import defaultdict
runs = defaultdict(list)
for d in sorted(glob.glob(os.path.join(os.environ["SZT"], "results/planet/*__*__*"))):
    region, tool, n = os.path.basename(d).split("__")
    m = {}
    for f in (f"{d}/measure.json", f"{d}/out/measure.json"):
        if os.path.exists(f):
            m = json.load(open(f))
    zims = glob.glob(f"{d}/out/*.zim")
    ends = [f"{d}/host-start.txt", f"{d}/host-end.txt"]
    busy = os.path.exists(f"{d}/contended.txt") or not all(
        os.path.exists(f) and "IDLE" in open(f).read() for f in ends)
    if os.path.exists(f"{d}/VOID.txt"):
        tool += " (VOID)"
    planet_gb = int(open(f"{d}/planet-bytes.txt").read()) / 1e9 if os.path.exists(f"{d}/planet-bytes.txt") else 0
    if tool.startswith("maps2zim") and m.get("peak_disk_gb") is not None:
        m["peak_disk_gb"] = round(m["peak_disk_gb"] - planet_gb, 2)   # the shared planet is under its watched /tmp
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

Under the table, write for Switzerland (and each §2h region) and metric either "A uses less X
than B (ratio, ranges)" or "no clear difference", by the 1.5× rule; then
the coverage and feature notes, the planet build id, both image digests
and the StreetZim commit, and each run's profile. Add the entry count of each ZIM, and zimcheck
where the host has it (`command -v zimcheck && zimcheck -A <zim>`). Add
one reference row per region with a production recipe, marked
**production (not this test)**: `maps_en_switzerland` (task
`625d6b2d…`, 72.4 min scraper, 684.5 MB ZIM, peaks 17.18 GB memory / 208.7
GB disk incl. its planet download) and `maps_en_luxembourg` (task
`46f2ea98…`, 75.0 min, 176.6 MB), refreshed from the API, with the note
that production times include the ~103 GB planet download and ran on
other hardware. Switzerland's row is the one openZIM will read first.

Report Switzerland as soon as its six runs and the table are done; then
§2f and §2g; then, if the user wants them, §2h. Pack the results at the
end (below, after §2h).

### 2f. Alaska across the antimeridian (no upload, no production files)

Row `alaska 172.0,51.0,-130.0,72.0` (on `next`; `main`'s row is
`-180.0,51.0,-130.0,72.0`). The host's `world-data/regions/alaska.*`
files were cut for the old bbox; this test only reads them (the real
rebuild follows [docs/alaska-antimeridian-runbook.md](docs/alaska-antimeridian-runbook.md),
with the round's owner). Approved, after §2e.

**The tiles:** the round's world tiles, the driver's `WORLD_MB=` (§0's
table). **The PBF:** cut from the two region extracts the host has,
`world-data/regions/russia.osm.pbf` (7.0 GB, bbox `27,41,180,82`, which
holds the western Aleutians, 172E–180) and `alaska.osm.pbf` (413 MB, bbox
`-180,51,-130,72`), and merged. That reads about 7.4 GB instead of the
94,612,383,571-byte planet, which shares one spinning disk with the
builds. Check both headers first:

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
mkdir -p "$SZT/alaska"
W=$(sed -n 's/^WORLD_MB=//p' "$PROD/cloud/rebuild_old_regions.sh" | head -1)
test -s "$W" && echo "$W" > "$SZT/alaska/WORLD_MBTILES.txt" && echo "world tiles: $W ($(stat -c%s "$W") B)" || echo "STOP: no world tiles at '$W'; ask the user for the path and write it to $SZT/alaska/WORLD_MBTILES.txt"
for r in russia alaska; do echo "== $r"; osmium fileinfo "$PROD/world-data/regions/$r.osm.pbf" | grep -A1 -i 'bounding box'; done
```

**Expect:** russia's box reaches `180` east and covers 51–72 N; alaska's
covers `-180` to `-130`. If either doesn't, stop and ask: the fallback
is a poly extract of the planet (`python -m streetzim.area poly` in the
clone, then `osmium extract -p`), which reads all of it and costs hours
of disk contention with the builds.

The build runs on the host, not in a container, so it has no memory cap
while swap is full: it starts only with 24 GiB `MemAvailable` and no §2e
run in progress, and runs under a 16 GiB limit when `systemd-run --user`
works (otherwise say so, and watch `free -g`):

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
A=$SZT/alaska
[ "$(awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo)" -ge 24 ] || { echo "STOP: under 24 GiB MemAvailable"; exit; }
if pgrep -f "$SZT/run-one.sh" >/dev/null || docker ps --format '{{.Names}}' | grep -q '^cmp-'; then echo "STOP: a §2e/§2h run is in progress; wait for it"; exit; fi
CAP=(systemd-run --user --scope -q -p MemoryMax=16G -p MemorySwapMax=0); "${CAP[@]}" true 2>/dev/null || CAP=()
echo "memory cap: ${CAP[*]:-none (systemd-run --user unavailable)}"
osmium extract -b 172.0,51.0,180.0,72.0 "$PROD/world-data/regions/russia.osm.pbf" -o "$A/west.osm.pbf" --overwrite &&
osmium extract -b -180.0,51.0,-130.0,72.0 "$PROD/world-data/regions/alaska.osm.pbf" -o "$A/east.osm.pbf" --overwrite &&
osmium merge "$A/west.osm.pbf" "$A/east.osm.pbf" -o "$A/alaska.osm.pbf" --overwrite; echo "extract exit $?"
osmium fileinfo -e "$A/alaska.osm.pbf" | grep -A1 -i 'bounding box'
cd "$A" && STREETZIM_CACHE_DIR="$A/cache" "${CAP[@]}" "$PY" "$SZT/next/create_osm_zim.py" --pbf "$A/alaska.osm.pbf" --mbtiles "$(cat "$A/WORLD_MBTILES.txt")" --bbox=172.0,51.0,-130.0,72.0 --name Alaska --routing --spatial-chunk-scale 10 --split-find-chips -o "$A/alaska.zim" > "$A/build.log" 2>&1; echo "exit $?"
grep -a -e 'antimeridian' -e 'opening centre' "$A/build.log" | head
```

(Our `osmium` lines name `$SZT`, so `sz-busy.sh` doesn't count them; run
them when `sz-busy.sh` shows no build, to spare the disk.)

Check the western Aleutians (172E–180: Attu, Shemya, Kiska), against the
last published Alaska ZIM if the host has one:

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
"$PY" - "$SZT/alaska/alaska.zim" $(ls -t /storage/streetzim/osm-alaska-20*.zim 2>/dev/null | head -1) <<'EOF' | tee "$SZT/results/alaska.txt"
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

**Pass:** exit 0; the PBF's data box runs from about -180 to about 180;
the log has `Joined N road(s) split at the antimeridian`; the new ZIM has
z10 tiles east of 172E and search hits for Attu, Shemya and Kiska (and
Anchorage); the old published ZIM, if present, has none east of 172E.
**FAIL:** no tiles or no hits there.

### 2g. Massachusetts, measured ([docs/zimfarm.md](../docs/zimfarm.md#what-a-build-costs))

The sandbox run stopped at 2 GB free disk. Needs about 30 GB free on
`/storage`; approved, after §2f. Inside the image, as the Luxembourg
Docker row was measured, with the same limits as §2e. **`--profile
basic`:** on `next` since `3cd8e8e` the command line's default is
`--profile full` (Wikidata, Wikipedia, Overture, terrain); the Luxembourg
row was measured with what is now `basic`.

```bash
. "$HOME/sz-env.sh" && sz_round_quiet || exit
[ "$(awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo)" -ge 24 ] || { echo "STOP: under 24 GiB MemAvailable"; exit; }
[ "$(df -BG --output=avail "$SZT" | tail -1 | tr -dc 0-9)" -ge 100 ] || { echo "STOP: under 100 GB free on /storage"; exit; }
if pgrep -f "$SZT/run-one.sh" >/dev/null || docker ps --format '{{.Names}}' | grep -q -E '^(cmp-|sz-test-)'; then echo "STOP: another test run is in progress; wait for it"; exit; fi
mkdir -p "$SZT/ma/in"
curl -fL --retry 3 -o "$SZT/ma/in/massachusetts-latest.osm.pbf" https://download.geofabrik.de/north-america/us/massachusetts-latest.osm.pbf
stat -c%s "$SZT/ma/in/massachusetts-latest.osm.pbf"
docker run --rm --name sz-test-ma --memory 16g --memory-swap 16g --user "$(id -u):$(id -g)" -e HOME=/tmp -e STREETZIM_CACHE_DIR=/work/cache -v "$SZT/ma:/work" streetzim:hosttest python /app/tools/measure_build.py --json /work/measure.json --watch /work/tmp --watch /work/out --watch /work/dl --log /work/build.log -- streetzim --profile basic --name osm_en_massachusetts --title Massachusetts --description "Offline map of Massachusetts with search and routing" --include-poly https://download.geofabrik.de/north-america/us/massachusetts.poly --pbf-url file:///work/in/massachusetts-latest.osm.pbf --output /work/out --tmp /work/tmp --dl /work/dl --stats-filename /work/out/task_progress.json; echo "exit $?"
cat /proc/loadavg; head -12 "$SZT/ma/measure.json"
```

(The run folder is mounted at `/work`, not over the container's `/run`.
The docker filesystem only holds the image; the run writes to `$SZT`.)

**Pass:** exit 0, a ZIM in `$SZT/ma/out/`, and `measure.json` with
`wall_s`, `cpu_s`, `peak_pss_gb`, `peak_disk_gb`. Report them with the
extract size (the host's copy on 29 September: 310,579,402 B; a later
download differs), the ZIM size, `nproc`, the profile, and whether a
production build overlapped (loadavg; then the wall time is an upper
bound). Copy `measure.json` and `build.log` to `$SZT/results/ma/`.

### 2h. The full matrix (optional, after §2f and §2g)

On the user's word: Luxembourg and D.C. with all four variants, and the
tilemaker variant for Switzerland, with the same runner, rules and
table as §2e (2 runs each, order reversed in the second round):

```bash
. "$HOME/sz-env.sh" || exit
setsid nohup bash -c '
for r in luxembourg dc; do
  for t in maps2zim sz-basic sz-full sz-tm; do bash "$SZT/run-one.sh" $t $r 1; rc=$?; echo "$(date -Is) $t $r 1: rc $rc"; [ $rc -eq 3 ] && exit 3; done
  for t in sz-tm sz-full sz-basic maps2zim; do bash "$SZT/run-one.sh" $t $r 2; rc=$?; echo "$(date -Is) $t $r 2: rc $rc"; [ $rc -eq 3 ] && exit 3; done
done
for n in 1 2; do bash "$SZT/run-one.sh" sz-tm switzerland $n; rc=$?; echo "$(date -Is) sz-tm switzerland $n: rc $rc"; [ $rc -eq 3 ] && exit 3; done
' > "$SZT/results/planet/matrix.out" 2>&1 < /dev/null &
echo "started; follow $SZT/results/planet/matrix.out"
```

Then the §2e per-run checks and table again (the table covers every run
folder), with the Luxembourg production reference row.

### 2i. Packing the results

```bash
. "$HOME/sz-env.sh" || exit
cp "$SZT/planet/SOURCE.txt" "$SZT/results/planet/"
find "$SZT/results" -name '*.zim' -printf '%s\t%p\n' | sort -k2 > "$SZT/results/ZIMS.tsv"
tar -C "$SZT" --exclude='*.zim' -czf "$SZT/results-$(date +%F).tgz" results
ls -l "$SZT/results-$(date +%F).tgz"
```

(ZIMs are already compressed and can be tens of GB, so the tarball
leaves them out and `ZIMS.tsv` lists them; the user fetches the ones
they want. The tarball stays under `$SZT`; don't upload it anywhere.)
When the tests are done, remove our images only
(`docker image rm streetzim:hosttest ghcr.io/openzim/maps:0.2.1`), on the
user's word; never a `prune`.

## 3. Merging to `main` (the user), and the host after each merge

Order: ops branch (§1.0) → `builder` → `next`. For each, the user:
1. opens a PR from the branch to `main`, after that branch contains the
   previous merge (`builder` must contain the ops branch's merge, `next`
   must contain `builder`'s; the branch owner merges `main` in first);
2. waits for CI to be green;
3. merges with **a merge commit** (not squash or rebase), so the commits
   the host is on stay ancestors of `main` and the host can fast-forward.

**The `builder` and `next` PRs don't wait for the host.** The host moves
to `main`'s merge commit `M` by an exact fast-forward (§1.0), never by a
`pull`, and nothing on the host fetches or pulls by itself: the round's
scripts only commit torrents locally (`cloud/upload_validated.sh`), and
Firebase deploys `web/` from the checkout as it is. So `builder` and
`next` can be on `main` today; the host keeps running `M`'s code until
§1.4 (`T=main`) after the round. The one guard: **never `git pull` in
`/storage/streetzim`, and never TESTING-STAGE1.md step 4 there, until
that step** (§0, §1.0). Before merging them, the user checks that no
build VM runs or can be relaunched, because VMs build from `main` and
upload to archive.org: `ops/cloud/build-vm-startup.sh` clones (or, after
a spot restart, `pull --ff-only`s) `main`, and `vm-health-cron.sh` and
`spot-to-ondemand-watcher.sh` relaunch VMs on their own
(`gcloud compute instances list --project streetzim --filter='name~^streetzim-build-'`
empty, and neither script in any crontab). The host's later torrent
commits reach `main` as the first two did (a pushed branch, merged).

After each merge, on the host:
- the ops branch: the host moves to `M` as §1.0 says (a fast-forward to
  that commit, not a pull), in an idle window;
- after the round: if the host is on `main`, the §1.4 block with
  `T=main`, `L=host-main` moves it (it then pulls the merged `main`); if
  it is on `host-builder` or `host-next`, the same block after the
  `next` PR merges (the host's torrent commits must be on `main` first);
- §1.5 (check_stage1, boundary);
- a smoke build: §2c (`ship-region.sh washington-dc --no-upload`), then
  move its ZIM out as §2c says.

The merges don't wait for §2d–2h. Report §2d–2h as they finish.

## 4. Report back (fill in)

- [ ] §0: the three helpers written; what `sz-busy.sh` and `sz-round.sh`
      printed each time a block used them; the orphaned server on port
      18770: its command line and parent before, and the `OK: stopped`
      line; `$HOME/sz-round-over.txt` if written.
- [ ] §1.0: the push of the host's commits (branch, commit), the lead's
      merges, how `main` got the split (PR number), the check lines,
      stage-1 step 4's last line and step 5's result, the two
      `sz-*-stage1.txt` records; any rollback; whether the round was
      resumed (when, with which command, the user's word).
- [ ] §1.1: host commit, branch, upstream, ahead count and host commits,
      `status -sb` lines, round state and drivers, crontab lines, host
      scripts calling `build-region-fast.sh`, disk (`/storage`, `/`,
      docker), memory and swap, fetch refspec, pending uploads, the
      round's last rows and D.C.'s row, checker result (after §1.0).
- [ ] §1.2: the clone's `gh/next` commit (expect `82def4f` or later); the
      five ancestry lines; the seven topic lines.
- [ ] §1.3: checker, boundary, pytest results.
- [ ] §1.4/1.5 per move: the deploy output saved and restored (the
      `saved to …` line), `.before`/`.after` contents, `OK`/`STOP` line, check_stage1 result, PIDs
      still running; any rollback.
- [ ] §2a: exit code and class counts per pair, at which `next` commit;
      every path beyond the table with its merge; tilemaker mode: the
      record check's output (paths by prefix, the before → after counts,
      `OTHER`).
- [ ] §2b: the release listing, exits, release stamps, rows and id
      differences vs https and cache, columns.
- [ ] §2c per branch: exit, gate lines, Overture and Wikipedia lines, Rust
      packer, the status diff, where the ZIM went, scratch removed.
- [ ] §2d: the cache copy check, warm-up runs, the diff classes,
      article/image counts for main, next and the host's newest D.C. ZIM
      (with its date).
- [ ] §2e (Switzerland): world-tiles metadata and whether it was reused;
      planet build id, size, sha256 and image digests; per-run check
      failures, rc 4 runs and any `VOID.txt`; TABLE.md with the 1.5×
      verdicts (maps2zim's disk with the planet subtracted); contended
      runs; the production reference row for Switzerland, refreshed.
- [ ] §2f: the two region headers, PBF box, antimeridian log lines, tile
      and search counts (new and published).
- [ ] §2g: `measure.json` figures, extract and ZIM size, profile, load.
- [ ] §2h (if run): the same as §2e for Luxembourg, D.C. and `sz-tm`,
      with Luxembourg's reference row.
- [ ] §2i: `ZIMS.tsv`; the tarball path and size.
- [ ] Anything that printed `STOP` or `FAIL`, with its output.
- [ ] Leftovers: `$SZT` size (`du -sh`), our docker images, and the
      records in `$HOME/sz-move-*` and `$HOME/sz-pull-*`.
