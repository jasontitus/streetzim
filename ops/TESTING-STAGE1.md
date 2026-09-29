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
`claude/adoring-dijkstra-i2vge7`. `pre-ops-split` tags the commit before.
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
git status --short                 # anything but ?? lines: see below
crontab -l                         # note entries that call scripts here
ps -eo pid,lstart,args | grep -E '/storage/streetzim/.*\.(sh|py|mjs)' | grep -v grep
ls -1 .*.sh 2>/dev/null            # untracked host scripts
```

**What to record:**
- the current commit;
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

```bash
rm -rf /tmp/sz-stage1
git clone -q /storage/streetzim /tmp/sz-stage1
cd /tmp/sz-stage1
git fetch -q origin claude/adoring-dijkstra-i2vge7    # or the merged branch
git checkout -q FETCH_HEAD
PY=/storage/streetzim/venv-linux/bin/python3          # the host's venv
bash ops/check_stage1.sh --root /tmp/sz-stage1 --python "$PY"
"$PY" -m pytest -q ops/tests tests/test_check_boundary.py
```

**Expect:**
- `ALL CHECKS PASSED`, with no `FAIL` lines;
- the tests pass.

The scratch clone is at another path, so the checks resolve against
`/tmp/sz-stage1`. That is intended: nothing there runs against the real
data.

**Optional: the pull itself, on a copy of the checkout.** This needs disk
for a code-only copy (no data), about the size of `.git`:

```bash
rm -rf /tmp/sz-pull && git clone -q /storage/streetzim /tmp/sz-pull
cd /tmp/sz-pull
git fetch -q origin claude/adoring-dijkstra-i2vge7
git merge --ff-only FETCH_HEAD      # must fast-forward, as on the host
bash ops/check_stage1.sh --root /tmp/sz-pull --python "$PY"
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
git rev-parse HEAD > /tmp/sz-before-stage1.txt      # for a rollback
git pull --ff-only
```

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
git status --short            # host-edited lists may show M: keep them
git checkout "$(cat /tmp/sz-before-stage1.txt)" -- .
git clean -n                  # review; ops/ now shows as untracked
```

**Or reset the branch:**
`git reset --hard "$(cat /tmp/sz-before-stage1.txt)"`. This is only
safe when step 1 showed no local changes you want to keep, because it
also resets the host-edited lists.

**What a rollback brings back:**
- the regular files replace the symlinks;
- running scripts keep running;
- `attic/` comes back too.

Report what failed, with the check output.

## Prompt for a Claude Code session on the host

> Follow `ops/TESTING-STAGE1.md` in the streetzim repository, using branch
> `claude/adoring-dijkstra-i2vge7` (fetch it; don't switch the
> `/storage/streetzim` checkout).
>
> - Do steps 1–3 only.
> - Report the step-1 findings (commit, git status lines, running
>   scripts, crontab entries, untracked host scripts) and the full output
>   of step 2.
> - Change nothing in `/storage/streetzim`, and run no production script.
> - Stop at step 3 and wait for my go-ahead before the pull in step 4.
