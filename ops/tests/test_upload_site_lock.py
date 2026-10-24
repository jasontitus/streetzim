"""upload_validated.sh's web deploy (step 6) runs under the site-deploy lock,
tmp/.site-deploy.lock, which ops/langpack/pack_queue.sh's live step takes
too: two deploys from one tree read each other's writes."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# web/generate.py --deploy's stand-in: is the lock held while it runs?
PY = """#!/bin/bash
"{py}" - <<'PYEOF'
import fcntl, os
f = open(os.environ["LOCK"], "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("FREE")
except BlockingIOError:
    print("HELD")
print("FD7" if os.path.exists("/proc/self/fd/7") else "NO-FD7")   # the lock lasts while generate.py runs
PYEOF
"""


def _run(tmp_path: Path) -> tuple[str, float]:
    text = (ROOT / "ops" / "cloud" / "upload_validated.sh").read_text()
    i = text.index("# --- 6. web deploy ---")
    block = text[i:text.index('echo "DONE ${id}"', i)]
    py = tmp_path / "py"
    py.write_text(PY.format(py=sys.executable))
    py.chmod(0o755)
    t0 = time.time()
    r = subprocess.run(["bash", "-c", f'PROJECT_DIR={tmp_path}; PYTHON={py}; id=x\n{block}'], cwd=tmp_path,
                       env={**os.environ, "LOCK": str(tmp_path / "tmp" / ".site-deploy.lock")},
                       capture_output=True, text=True, timeout=120)
    return r.stdout, time.time() - t0


def test_the_web_deploy_holds_the_site_deploy_lock(tmp_path):
    out, _ = _run(tmp_path)
    assert out.split() == ["HELD", "FD7"], out


def test_the_web_deploy_waits_for_the_lock(tmp_path):
    lock = tmp_path / "tmp" / ".site-deploy.lock"
    lock.parent.mkdir(parents=True)
    fh = open(lock, "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    threading.Timer(2.0, fh.close).start()
    out, took = _run(tmp_path)
    assert out.split()[0] == "HELD" and took >= 1.5, (out, took)
