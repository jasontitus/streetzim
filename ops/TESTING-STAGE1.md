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

Every git command names the checkout with `-C`, so nothing depends on the
working directory.

```bash
git -C /storage/streetzim rev-parse --short HEAD
git -C /storage/streetzim rev-parse --abbrev-ref HEAD
# anything but ?? lines: see below
git -C /storage/streetzim --no-optional-locks status -sb
# "behind ahead": ahead must be 0
git -C /storage/streetzim rev-list --left-right --count '@{u}...HEAD'
# note entries that call scripts in the checkout
crontab -l
ps -eo pid,lstart,args | grep -E '/storage/streetzim/.*\.(sh|py|mjs)' | grep -v grep
# started from inside the checkout (best effort; also matches the physical path)
for d in /proc/[0-9]*; do c=$(readlink "$d/cwd" 2>/dev/null); case "$c" in /storage/streetzim|/storage/streetzim/*|"$(cd /storage/streetzim && pwd -P)"|"$(cd /storage/streetzim && pwd -P)"/*) echo "${d#/proc/} $(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)";; esac; done
# untracked host scripts
ls -1 /storage/streetzim/.*.sh 2>/dev/null
```

(`--no-optional-locks` keeps `git status` from rewriting the index while
host scripts may be running git.)

The process listings are best effort. They see other users' processes
only as root, and they include your own shell and commands; ignore those.

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

Every line below is safe **on its own**. Paths are written out (no
variables), and every git command names the scratch clone with `-C`. So it
doesn't matter whether the lines run as one block, one at a time, or as
separate commands in a Claude Code session (where the working directory
carries over between commands but variables don't). If the clone fails,
the later `git -C /tmp/sz-stage1 …` lines fail with "cannot change to" and
touch nothing else.

```bash
cd /tmp
rm -rf /tmp/sz-stage1
git clone -q /storage/streetzim /tmp/sz-stage1
git -C /tmp/sz-stage1 fetch -q "$(git -C /storage/streetzim remote get-url origin)" claude/adoring-dijkstra-i2vge7
git -C /tmp/sz-stage1 checkout -q FETCH_HEAD
bash /tmp/sz-stage1/ops/check_stage1.sh --root /tmp/sz-stage1 --python /storage/streetzim/venv-linux/bin/python3
/storage/streetzim/venv-linux/bin/python3 -m pytest -q -p no:cacheprovider /tmp/sz-stage1/ops/tests /tmp/sz-stage1/tests/test_check_boundary.py
```

**Expect:**
- `ALL CHECKS PASSED`, with no `FAIL` lines;
- the tests pass. If pytest is missing from the host venv, skip that
  line; the checker is the main test.

**Notes:**
- The fetch reads from the host checkout's remote (GitHub). It changes
  nothing in `/storage/streetzim`, and the clone's own `origin` (the host
  checkout) doesn't have the branch.
- If the fetch fails for want of credentials or an SSH setting, stop and
  report. Don't fetch from inside `/storage/streetzim` instead.
- The checks resolve against `/tmp/sz-stage1`, not the real data.

**Optional: the pull itself, on a copy of the checkout.** This needs disk
for a code-only copy (no data), about the size of `.git`. The same rule
applies: literal paths, each line safe alone.

```bash
cd /tmp
rm -rf /tmp/sz-pull
git clone -q /storage/streetzim /tmp/sz-pull
git -C /tmp/sz-pull branch --unset-upstream
git -C /tmp/sz-pull fetch -q "$(git -C /storage/streetzim remote get-url origin)" claude/adoring-dijkstra-i2vge7
git -C /tmp/sz-pull merge -q --ff-only FETCH_HEAD
bash /tmp/sz-pull/ops/check_stage1.sh --root /tmp/sz-pull --python /storage/streetzim/venv-linux/bin/python3
```

**Four details:**
- `merge --ff-only` must succeed: that is the same fast-forward the host
  will do.
- `--unset-upstream` stops the checker from counting the new commits as
  "local commits". In the copy, the upstream is the host's own branch.
- It fast-forwards to the feature branch. If the host already has commits
  of the branch it pulls that the feature branch lacks, this fails although
  the real pull may not; once the branch is merged, fetch that branch
  instead.
- A clone doesn't carry the host's uncommitted edits to the lists. They
  don't block the real pull because the split doesn't change those files;
  this prints nothing if that still holds:
  `git -C /tmp/sz-pull diff --name-only ORIG_HEAD HEAD -- '*.list' viewer-refresh.tsv cloud/region-variants.tsv tmp/live-inventory.out`

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

One block, run as one command. It:
- stops if it can't read the checkout, or if the checkout is already at
  the split (and says whether a rollback point was recorded);
- records the commit before the pull (the rollback point);
- pulls only if that record was written. `--no-rebase` and the two
  `autoStash=false` settings keep the host's git configuration from
  rebasing, or from stashing the host-edited lists during the pull (they
  work on any git version);
- records the commit the pull reached, and says whether it is the split.

Both records are in `$HOME`, so they survive a reboot.

```bash
if ! git -C /storage/streetzim rev-parse -q --verify HEAD >/dev/null; then
  echo "STOP: cannot read /storage/streetzim; ask"
elif git -C /storage/streetzim cat-file -e HEAD:ops/in-place.txt 2>/dev/null; then
  echo "STOP: already at the split"
  cat "$HOME/sz-before-stage1.txt" "$HOME/sz-after-stage1.txt" || echo "records incomplete: no scripted rollback; ask"
else
  rm -f "$HOME/sz-after-stage1.txt" &&
  git -C /storage/streetzim rev-parse HEAD > "$HOME/sz-before-stage1.txt.new" &&
  mv "$HOME/sz-before-stage1.txt.new" "$HOME/sz-before-stage1.txt" &&
  git -c merge.autoStash=false -c rebase.autoStash=false -C /storage/streetzim pull --ff-only --no-rebase &&
  git -C /storage/streetzim rev-parse HEAD > "$HOME/sz-after-stage1.txt" &&
  if git -C /storage/streetzim cat-file -e HEAD:ops/in-place.txt 2>/dev/null; then
    echo "OK: pulled to the split"
  else
    echo "STOP: pulled, but not to the split (is the branch merged?); don't run this block again; ask"
  fi || echo "STOP: step 4 did not finish (the pull refused, or a record could not be written); check git log -1; ask"
fi
```

- **"Step 4 did not finish"**: usually the pull refused (local changes,
  or not a fast-forward) and changed nothing; `git -C /storage/streetzim
  log -1` still shows the commit in `sz-before-stage1.txt`. Then go back
  to step 1; running the block again later records the rollback point
  afresh. If HEAD did move (a record couldn't be written after the pull),
  ask.
- **"Pulled, but not to the split"** means the host took commits that
  step 2 didn't test. Don't roll back and don't run step 4 again (it
  would record the new commit as the rollback point); ask.
- **The build VMs** (`ops/cloud/build-vm-startup.sh`) pull on their own
  when they start.

## 5. After: verify on the host (read-only)

```bash
bash /storage/streetzim/ops/check_stage1.sh --root /storage/streetzim --python /storage/streetzim/venv-linux/bin/python3
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
/storage/streetzim/venv-linux/bin/python3 /storage/streetzim/cloud/serve_zims.py --help
/storage/streetzim/venv-linux/bin/python3 /storage/streetzim/ops/cloud/serve_zims.py --help
```

Both must work, from any directory.

## 6. Rollback, if anything is wrong

The rollback runs only if HEAD is still the commit the pull reached, and
that commit is upstream (not one the host made). If anything committed
since (`ops/cloud/upload_validated.sh` commits torrent files on the host),
a rollback would take those files back off the disk, so the block stops
instead. Then ask.

```bash
# host-edited lists may show M: they are kept
git -C /storage/streetzim --no-optional-locks status --short
```

```bash
if test -s "$HOME/sz-before-stage1.txt" &&
   test "$(git -C /storage/streetzim rev-parse HEAD)" = "$(cat "$HOME/sz-after-stage1.txt" 2>/dev/null)" &&
   git -C /storage/streetzim merge-base --is-ancestor HEAD '@{u}'; then
  git -C /storage/streetzim reset --keep "$(cat "$HOME/sz-before-stage1.txt")" &&
  rm -f "$HOME/sz-after-stage1.txt" && echo "ROLLED BACK"
else
  echo "STOP: HEAD moved since the pull, has a host commit, or no rollback point was recorded; ask"
fi
```

```bash
# expect the same lines as before: the host-edited lists, plus ?? lines
git -C /storage/streetzim --no-optional-locks status --short
git -C /storage/streetzim log --oneline -1
```

`git reset --keep` moves the branch back and updates the files, but keeps
local edits: the host-edited lists, which are the same in both commits.
If a locally edited file differs between the two commits, it refuses and
changes nothing. Then stop and ask.

**Don't use:**
- `git reset --hard`: it discards the host-edited lists;
- `git checkout <sha> -- .`: it overwrites them, leaves the `ops/` files
  in place, and stages regular files over the old-path symlinks.

**What a rollback brings back:**
- the regular files replace the symlinks;
- running scripts keep running;
- `attic/` comes back too.

Report what failed, with the check output.

Once stage 1 is accepted (or rolled back), delete
`$HOME/sz-before-stage1.txt` and `$HOME/sz-after-stage1.txt`.

## Prompt for a Claude Code session on the host

> Test the streetzim ops split on this host, read-only.
>
> 1. First run exactly these commands, each as written. Every git command
>    names `/tmp/sz-stage1` with `-C`:
>    ```
>    cd /tmp
>    rm -rf /tmp/sz-stage1
>    git clone -q /storage/streetzim /tmp/sz-stage1
>    git -C /tmp/sz-stage1 fetch -q "$(git -C /storage/streetzim remote get-url origin)" claude/adoring-dijkstra-i2vge7
>    git -C /tmp/sz-stage1 checkout -q FETCH_HEAD
>    ```
> 2. Then read `/tmp/sz-stage1/ops/TESTING-STAGE1.md` and do its steps 1–3
>    only, running the commands exactly as written there.
>
> Report:
> - the step-1 findings: commit, `status -sb` lines, the local-commit
>   count, running scripts, crontab entries, untracked host scripts;
> - the full output of step 2.
>
> Rules:
> - The only git commands on `/storage/streetzim` are the read-only
>   `git -C /storage/streetzim` ones written in the runbook (`rev-parse`,
>   `status`, `rev-list`, `remote get-url`). Never run `fetch`,
>   `checkout`, `pull`, `merge`, `reset`, `stash` or `commit` there, and
>   change nothing there.
> - Run no production script.
> - If anything fails, stop and report; don't improvise a workaround.
> - Stop at step 3 and wait for my go-ahead before the pull in step 4.
