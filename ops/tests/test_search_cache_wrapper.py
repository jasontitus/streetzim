"""build-region-fast.sh's search cache path: STREETZIM_SEARCH_CACHE replaces
it (a cache extracted from the region's multilingual tiles)."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_wrapper_takes_another_search_cache():
    text = (ROOT / "ops" / "build-region-fast.sh").read_text(encoding="utf-8")
    [line] = [ln for ln in text.splitlines() if ln.startswith("SEARCH=")]

    def run(env: dict[str, str]) -> str:
        return subprocess.run(["bash", "-c", f'SRC_ID=switzerland; {line}; printf %s "$SEARCH"'],
                              env=env, capture_output=True, text=True).stdout
    assert run({"PATH": "/usr/bin:/bin"}) == "/storage/streetzim/world-data/regions/switzerland.search.jsonl"
    assert run({"PATH": "/usr/bin:/bin", "STREETZIM_SEARCH_CACHE": "/x/ml.search.jsonl"}) == "/x/ml.search.jsonl"
