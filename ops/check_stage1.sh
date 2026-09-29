#!/usr/bin/env bash
# Read-only checks for stage 1 of the ops split (ops/README.md,
# ops/TESTING-STAGE1.md). It changes nothing and runs no production script:
# it only reads git state, files and the process table, syntax-checks the
# shell scripts, and calls the Python root finder.
#
#   bash ops/check_stage1.sh [--root DIR] [--python PY]
#
# --root   the checkout to check (default: the one this file is in)
# --python interpreter for the Python checks (default: DIR/venv-linux/bin/python3
#          if present, else python3)
# Exit status: 0 if every check passed, 1 otherwise. Warnings don't fail.
set -u
# git status must not take index.lock or rewrite the index while host
# scripts may be running git.
export GIT_OPTIONAL_LOCKS=0

ROOT=""
PY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    -h|--help) sed -n 2,13p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$ROOT" ]; then
  ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
fi
ROOT="$(cd "$ROOT" 2>/dev/null && pwd)" || { echo "no such directory: --root" >&2; exit 2; }
[ -n "$ROOT" ] || { echo "no such directory: --root" >&2; exit 2; }
if [ -z "$PY" ]; then
  if [ -x "$ROOT/venv-linux/bin/python3" ]; then PY="$ROOT/venv-linux/bin/python3"; else PY=python3; fi
fi

fails=0
ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fails=$((fails + 1)); }
warn() { printf '  warn  %s\n' "$*"; }

cd "$ROOT" || exit 2
if ! head=$(git rev-parse --short HEAD 2>&1); then
  echo "not a usable git checkout: $ROOT ($head)"; echo "1 CHECK(S) FAILED"; exit 1
fi
echo "checkout: $ROOT ($head, branch $(git rev-parse --abbrev-ref HEAD))"

echo "1. layout"
if [ -f ops/README.md ] && [ -f ops/in-place.txt ]; then ok "ops/ is present"; else bad "ops/ missing: this checkout is not at the split"; echo "FAILED"; exit 1; fi
nlinks=0; dangling=0; wrong=0
while IFS= read -r -d '' rec; do
  mode="${rec%% *}"; path="${rec#*$'\t'}"
  [ "$mode" = "120000" ] || continue
  nlinks=$((nlinks + 1))
  [ -e "$path" ] || { bad "dangling symlink: $path"; dangling=$((dangling + 1)); continue; }
  [ -L "$path" ] || { bad "tracked symlink is not a symlink on disk: $path (replaced by mv/sed -i?)"; wrong=$((wrong + 1)); }
done < <(git ls-files -s -z)
if [ "$nlinks" -eq 0 ]; then bad "no tracked symlinks found (is this the split? did git ls-files fail?)"
elif [ "$dangling" -eq 0 ] && [ "$wrong" -eq 0 ]; then ok "$nlinks old-path symlinks resolve into ops/"; fi

echo "2. local changes that would block or undo a pull"
# Only the data files the host edits by design are expected to differ: the
# *.list / *.tsv / *.out entries of ops/in-place.txt. A change to any other
# tracked file (web/, scripts, tests) is reported.
inplace_re='^$'
while IFS= read -r e; do inplace_re="$inplace_re|^$(printf '%s' "$e" | sed 's/[.[\*^$()+?{|]/\\&/g')\$"; done \
  < <(grep -v '^#' ops/in-place.txt | grep -E '\.(list|tsv|out)$')
blocking=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  st="${line:0:2}"; path="${line:3}"
  case "$st" in "??") continue ;; esac
  if printf '%s' "$path" | grep -Eq "$inplace_re"; then
    warn "host-edited file changed (expected, keep it): $st $path"
  else
    bad "local change: $st $path (see ops/README.md 'Pulling on the build host')"
    blocking=$((blocking + 1))
  fi
done < <(git status --porcelain)
[ "$blocking" -eq 0 ] && ok "no local changes to tracked code"
if counts=$(git rev-list --left-right --count '@{u}...HEAD' 2>/dev/null); then
  ahead=${counts##*[[:space:]]}
  [ "$ahead" = "0" ] && ok "no local commits (a fast-forward pull is possible)" \
    || bad "$ahead local commit(s) not upstream: git pull --ff-only will refuse"
else
  warn "no upstream branch configured; local commits not checked"
fi

echo "3. shell scripts under ops/ parse"
nsh=0; synerr=0
while IFS= read -r -d '' f; do
  nsh=$((nsh + 1))
  bash -n "$f" 2>/dev/null || { bad "syntax: $f"; synerr=$((synerr + 1)); }
done < <(git ls-files -z 'ops/*.sh' 'ops/**/*.sh')
if [ "$nsh" -eq 0 ]; then bad "no shell scripts found under ops/"
elif [ "$synerr" -eq 0 ]; then ok "$nsh scripts parse (bash -n)"; fi
# every moved script (one with a symlink at its old path) carries the guard
nmoved=0; noguard=0
while IFS= read -r -d '' f; do
  [ -L "${f#ops/}" ] || continue
  nmoved=$((nmoved + 1))
  grep -q '^# ops split, stage 1:' "$f" || { bad "no old-path guard: $f"; noguard=$((noguard + 1)); }
done < <(git ls-files -z 'ops/*.sh' 'ops/**/*.sh')
if [ "$nmoved" -eq 0 ]; then bad "no moved shell scripts found"
elif [ "$noguard" -eq 0 ]; then ok "all $nmoved moved scripts carry the old-path guard"; fi

echo "4. Python ops files find this checkout"
if "$PY" -I - "$ROOT" <<'EOF'
import ast, os, sys, subprocess
root = sys.argv[1]
def defines_root(p):
    tree = ast.parse(open(os.path.join(root, p), encoding="utf-8").read())
    return any(isinstance(n, ast.FunctionDef) and n.name == "_streetzim_root" for n in tree.body)
files = [p for p in subprocess.run(["git", "-C", root, "ls-files", "ops/*.py", "ops/**/*.py"],
                                    capture_output=True, text=True).stdout.split()
         if defines_root(p)]
env = dict(os.environ); env.pop("STREETZIM_ROOT", None); os.environ.clear(); os.environ.update(env)
bad = 0
for rel in files:
    src = open(os.path.join(root, rel), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_streetzim_root")
    code = ast.get_source_segment(src, fn)
    for f in {os.path.join(root, rel), os.path.join(root, rel[len("ops/"):])}:
        if not os.path.exists(f):
            continue
        ns = {"os": os, "__file__": f}
        exec(code, ns)
        got = ns["_streetzim_root"]()
        if os.path.realpath(got) != os.path.realpath(root):
            print(f"  FAIL  {f}: root {got}"); bad += 1
if not files:
    print("  FAIL  no Python ops files define _streetzim_root", end=""); sys.exit(1)
print(f"  ok    {len(files)} files, by old and ops/ paths" if not bad else "", end="")
sys.exit(1 if bad else 0)
EOF
then echo; else fails=$((fails + 1)); fi

echo "5. boundary check"
if out=$("$PY" -I tools/check_boundary.py --root "$ROOT" 2>&1); then ok "$(printf '%s\n' "$out" | tail -1)"; else bad "$(printf '%s\n' "$out" | tail -3)"; fi

echo "6. host view (informational)"
# By path on the command line, or started from inside the checkout (cwd).
# Best effort: other users' processes show only to root, and /proc gives the
# physical cwd, so both the given and the physical path are matched.
PHYS="$(pwd -P)"
running=$( { ps -eo pid=,args= | awk -v r="$ROOT/" -v p="$PHYS/" '(index($0, r) || index($0, p)) && /\.(sh|py|mjs)( |$)/ && !/check_stage1/';
             for d in /proc/[0-9]*; do
               c=$(readlink "$d/cwd" 2>/dev/null) || continue
               case "$c" in "$ROOT"|"$ROOT"/*|"$PHYS"|"$PHYS"/*) ;; *) continue ;; esac
               pid=${d#/proc/}; [ "$pid" = "$$" ] && continue
               args=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
               case "$args" in *check_stage1*|"") ;; *.sh*|*.py*|*.mjs*) printf '%s %s (cwd)\n' "$pid" "$args" ;; esac
             done; } | sort -u -n )
if [ -n "$running" ]; then warn "scripts running from this checkout, best effort (they keep running across a pull):"; printf '%s\n' "$running" | sed 's/^/          /'; else ok "no scripts from this checkout found running (best effort)"; fi
if command -v crontab >/dev/null && crontab -l >/dev/null 2>&1; then
  n=$(crontab -l 2>/dev/null | grep -v '^#' | grep -c "$ROOT")
  warn "$n crontab line(s) name this checkout (they keep working through the symlinks)"
fi
hidden=$(git ls-files --others -- '.*.sh' | grep -vc '/' )
[ "$hidden" -gt 0 ] && warn "$hidden untracked .*.sh host scripts at the root (they keep working through the symlinks; stage 2 must switch them)"

echo
if [ "$fails" -eq 0 ]; then echo "ALL CHECKS PASSED"; exit 0; else echo "$fails CHECK(S) FAILED"; exit 1; fi
