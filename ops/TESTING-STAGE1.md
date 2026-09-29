# Testing the ops split (stage 1) on the build host

This is a runbook for a person or a Claude Code session on the build host.
It checks that the ops split keeps `/storage/streetzim` working before and
just after the host takes the change. Background: [README.md](README.md).

**The change:**
- Operations files moved to `ops/<same path>`, with a symlink at each old
  path.
- Moved shell scripts start with a guard; moved Python files find the
  checkout with `_streetzim_root()`.
- Nothing under `web/` moved, and neither did the files the host edits in
  place ([in-place.txt](in-place.txt)).

**Commits:** `3d0a2c4` (moves) and `835226f` (symlinks, guard, root
finder), plus the review fixes after them, on branch
`claude/adoring-dijkstra-i2vge7`. The commit before the split is `8334eb6`.
Merge the branch to the branch the host pulls (usually via a PR) before
step 4.

**Rules for the session:**
- **Run no production script,** except where a step names one, and never
  one that builds, uploads or deletes. Many of them `cd /storage/streetzim`
  and start work before they read their arguments; even `--help` can run
  setup.
- **Change nothing in `/storage/streetzim` before step 4.** Steps 1–3
  are read-only there, and step 2 works in a scratch clone.
- **Stop and report at any `FAIL`.** Don't work around it.

## 1. Before: look at the host (read-only)

```bash
cd /storage/streetzim
git rev-parse --short HEAD; git rev-parse --abbrev-ref HEAD
git --no-optional-locks status -sb        # anything but ?? lines: see below
git rev-list --left-right --count '@{u}...HEAD'   # "behind ahead": ahead must be 0
crontab -l                                # note entries that call scripts here
ps -eo pid,lstart,args | grep -E '/storage/streetzim/.*\.(sh|py|mjs)' | grep -v grep
ls -l /proc/*/cwd 2>/dev/null | grep ' /storage/streetzim$'   # scripts started relatively
ls -1 .*.sh 2>/dev/null                   # untracked host scripts
```

(`--no-optional-locks` keeps `git status` from rewriting the index while
host scripts may be running git.)

**What to record:**
- the current commit, and whether the host has **local commits**. Some
  ops scripts commit on the host; the "ahead" count of `rev-list` is the
  number of those local commits. If it isn't 0, `git pull --ff-only` will
  refuse, and those commits must be dealt with first (pushed or merged).
  Ask;
- the PIDs of the running scripts (step 5 checks they survived);
- every `M`, `T` or `D` in `git status`.

**How to judge the `git status` lines:**
- On the `*.list` files, `viewer-refresh.tsv` or
  `cloud/region-variants.tsv`, they are expected: the host edits those.
  Leave them.
- On anything else, they block the pull. Resolve them first: see
  "Pulling on the build host" in [README.md](README.md). Don't discard
  work you don't understand; ask.

## 2. The change, in a scratch clone (read-only for the host)

Run it as one block. The subshell stops at the first error, and every git
command names the scratch clone with `-C`. So nothing can land in
`/storage/streetzim`, even if the clone fails (a full `/tmp`, say).

```bash
(
  set -eu
  UPSTREAM="$(git -C /storage/streetzim remote get-url origin)"   # GitHub
  S=/tmp/sz-stage1
  rm -rf "$S"
  git clone -q /storage/streetzim "$S"
  git -C "$S" fetch -q "$UPSTREAM" claude/adoring-dijkstra-i2vge7   # or the merged branch
  git -C "$S" checkout -q FETCH_HEAD
  PY=/storage/streetzim/venv-linux/bin/python3                     # the host's venv
  bash "$S/ops/check_stage1.sh" --root "$S" --python "$PY"
  "$PY" -m pytest -q --rootdir "$S" "$S/ops/tests" "$S/tests/test_check_boundary.py" \
    || echo "(pytest failed or is missing in the host venv; the checker above is the main test)"
)
```

**Expect:**
- `ALL CHECKS PASSED`, with no `FAIL` lines;
- the tests pass.

The clone's own `origin` is the host checkout, which does not have the
branch; `$UPSTREAM` is the host checkout's remote (GitHub). Fetching reads
from it and changes nothing in `/storage/streetzim`. The scratch clone is at
another path, so the checks resolve against `/tmp/sz-stage1`. That is intended: nothing there runs against the real
data.

**Optional: the pull itself, on a copy of the checkout.** This needs disk
for a code-only copy (no data), about the size of `.git`:

```bash
(
  set -eu
  UPSTREAM="$(git -C /storage/streetzim remote get-url origin)"
  S=/tmp/sz-pull
  rm -rf "$S"
  git clone -q /storage/streetzim "$S"
  git -C "$S" fetch -q "$UPSTREAM" claude/adoring-dijkstra-i2vge7
  git -C "$S" merge --ff-only FETCH_HEAD     # must fast-forward, as on the host
  bash "$S/ops/check_stage1.sh" --root "$S" --python /storage/streetzim/venv-linux/bin/python3
)
```

## 3. Decide

Go on to step 4 only if all of these hold:
- step 1 shows no blocking local changes;
- step 2 passed;
- the branch is merged where the host pulls from.

**Timing:**
- Running scripts keep running across the pull; bash keeps the file it
  opened.
- A queue that starts a new script by path picks up the new layout, which
  resolves to the same code plus the guard.
- If you want no overlap at all, pull between queue runs.

## 4. The pull (the only step that changes the host)

```bash
cd /storage/streetzim
git rev-parse HEAD > "$HOME/sz-before-stage1.txt"   # for a rollback (survives a reboot)
git pull --ff-only
```

(After a pull, `ORIG_HEAD` also names the commit before it, until the next
pull or reset.)

- **If it refuses** (local changes, or not a fast-forward), it has
  changed nothing. Go back to step 1.
- **The build VMs** (`ops/cloud/build-vm-startup.sh`) pull on their own
  when they start.

## 5. After: verify on the host (read-only)

```bash
cd /storage/streetzim
bash ops/check_stage1.sh --python venv-linux/bin/python3
ps -eo pid,lstart,args | grep -E '/storage/streetzim/.*\.(sh|py|mjs)' | grep -v grep
```

**Expect:**
- `ALL CHECKS PASSED`. Warnings about host-edited files, crontab lines
  or untracked host scripts are informational: they keep working through
  the symlinks.
- Every PID from step 1 is still running.
- The host-edited lists still show the same `M` as in step 1.
- New starts of the same scripts show the same old paths in `ps`.
- **The next scheduled or queued run** of an ops script (cron, a queue
  moving to its next item) works as before. Watch its log.

**Optional smoke check.** Python tools that only print help:

```bash
venv-linux/bin/python3 cloud/serve_zims.py --help
venv-linux/bin/python3 ops/cloud/serve_zims.py --help
```

Both must work, from any directory.

## 6. Rollback, if anything is wrong

```bash
cd /storage/streetzim
git --no-optional-locks status --short   # host-edited lists may show M: they are kept
git reset --keep "$(cat "$HOME/sz-before-stage1.txt")"
git --no-optional-locks status --short   # expect only the host-edited lists
```

`git reset --keep` moves the branch back and updates the files, but keeps
local edits: the host-edited lists, which are the same in both commits.
If a locally edited file differs between the two commits, it refuses and
changes nothing. Then stop and ask.

**Don't use:**
- `git reset --hard`: it discards the host-edited lists;
- `git checkout <sha> -- .`: it overwrites them, and leaves `ops/`
  staged.

**What a rollback brings back:**
- the regular files replace the symlinks;
- running scripts keep running;
- `attic/` comes back too.

Report what failed, with the check output.

## Prompt for a Claude Code session on the host

> Follow `ops/TESTING-STAGE1.md` in the streetzim repository, from branch
> `claude/adoring-dijkstra-i2vge7`. Fetch the branch **only into the scratch
> clone**, using the step-2 block exactly as written (one subshell with
> `set -e` and `git -C`). Never run `git fetch`, `checkout`, `pull`,
> `merge`, `reset` or `stash` in `/storage/streetzim`. To read the
> runbook before step 2, use
> `git -C /storage/streetzim show origin/claude/adoring-dijkstra-i2vge7:ops/TESTING-STAGE1.md`
> only if that ref exists; otherwise read it on GitHub.
>
> - Do steps 1–3 only.
> - Report the step-1 findings (commit, git status lines, running
>   scripts, crontab entries, untracked host scripts) and the full output
>   of step 2.
> - Change nothing in `/storage/streetzim`, and run no production script.
> - Stop at step 3 and wait for my go-ahead before the pull in step 4.
