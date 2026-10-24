"""ops/extract-region-pbfs.sh: one osmium pass cuts the regions to do with
--strategy smart and marks each extract (ops/region-bbox.sh); an extract
newer than the planet and marked for this very file is skipped, an unmarked
one (an older complete_ways cut) is cut again."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# osmium extract -c CONFIG PLANET ...: logs its arguments and the outputs the
# config names, and writes each output.
OSMIUM = """#!{py}
import json, os, sys
args = sys.argv[1:]
cfg = json.load(open(args[args.index("-c") + 1]))
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
    for e in cfg["extracts"]:
        log.write("output " + e["output"] + "\\n")
        with open(os.path.join(cfg["directory"], e["output"]), "w") as f:
            f.write("cut " + e["output"] + "\\n")
"""


def _mark(pbf: Path) -> None:
    st = pbf.stat()
    Path(str(pbf) + ".strategy").write_text(f"smart {st.st_size} {int(st.st_mtime)}\n")


def test_an_unmarked_extract_is_cut_again_with_the_smart_strategy(tmp_path):
    text = (ROOT / "ops" / "extract-region-pbfs.sh").read_text().replace("/storage/streetzim", str(tmp_path))
    script = tmp_path / "extract-copy.sh"
    script.write_text(text)
    (tmp_path / "ops").mkdir()
    shutil.copy(ROOT / "ops" / "region-bbox.sh", tmp_path / "ops" / "region-bbox.sh")
    (tmp_path / "cloud").mkdir()
    (tmp_path / "tmp").mkdir()
    regions = tmp_path / "world-data" / "regions"
    regions.mkdir(parents=True)
    (tmp_path / "cloud" / "regions.tsv").write_text(
        "# id\tname\tbbox\ttier\na\tA\t1,2,3,4\tcountry\nb\tB\t5,6,7,8\tcountry\n")
    planet = tmp_path / "planet.osm.pbf"
    planet.write_text("planet")
    os.utime(planet, (1_000_000, 1_000_000))
    for rid, box in (("a", "1,2,3,4"), ("b", "5,6,7,8")):
        (regions / f"{rid}.osm.pbf").write_text(f"old {rid}\n")
        (regions / f"{rid}.osm.pbf.bbox").write_text(box + "\n")
    _mark(regions / "a.osm.pbf")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "osmium").write_text(OSMIUM.format(py=sys.executable))
    (bin_ / "osmium").chmod(0o755)
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "PLANET": str(planet),
           "TMPDIR": str(tmp_path / "tmp"), "STUB_LOG": str(log)}
    r = subprocess.run(["bash", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skip a (extract newer than planet, cut with --strategy smart)" in r.stdout
    calls = log.read_text().splitlines()
    assert len([c for c in calls if c.startswith("extract ")]) == 1 and "--strategy smart" in calls[0], calls
    assert [c for c in calls if c.startswith("output ")] == ["output b.osm.pbf.part"]
    assert (regions / "a.osm.pbf").read_text() == "old a\n"
    b = regions / "b.osm.pbf"
    st = b.stat()
    assert b.read_text() == "cut b.osm.pbf.part\n" and not Path(str(b) + ".part").exists()
    assert Path(str(b) + ".strategy").read_text() == f"smart {st.st_size} {int(st.st_mtime)}\n"
