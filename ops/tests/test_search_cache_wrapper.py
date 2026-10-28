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


def test_a_region_gets_its_extra_builder_flags(tmp_path):
    """cloud/region-build-flags.tsv gives one region builder flags (Bahrain's
    --ocr) without an edit to build-region-fast.sh; any other region gets none."""
    text = (ROOT / "ops" / "build-region-fast.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith("BUILD_FLAGS_TSV="))
    block = "\n".join(lines[i:i + 4])
    flags = tmp_path / "flags.tsv"
    flags.write_text("# id\tflags\nbahrain\t--ocr --ocr-scripts arabic,latin\n")

    def run(rid: str) -> list[str]:
        out = subprocess.run(["bash", "-c", f'ID={rid}; ARGS=(--name x); {block}\nprintf "%s\\n" "${{ARGS[@]}}"'],
                             env={"PATH": "/usr/bin:/bin", "STREETZIM_BUILD_FLAGS_TSV": str(flags)},
                             capture_output=True, text=True).stdout
        return [ln for ln in out.splitlines() if not ln.startswith("  extra")]
    assert run("bahrain") == ["--name", "x", "--ocr", "--ocr-scripts", "arabic,latin"]
    assert run("iran") == ["--name", "x"]
    assert (ROOT / "cloud" / "region-build-flags.tsv").read_text().count("bahrain\t--ocr") == 1
