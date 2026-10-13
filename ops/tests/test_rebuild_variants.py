"""Light variants in the rebuild driver (ops/cloud/rebuild_old_regions.sh).

build-region-fast.sh builds a variant (cloud/region-variants.tsv:
africa-light, switzerland-light) from its parent's extracts and Overture
parquets. The driver's data steps must check those files, not cut and
download a set of the variant's own that nothing reads (for africa-light,
an extract of all Africa), and must never re-cut the parent's files for a
variant whose box differs. The real block runs here with osmium, python
and the log stubbed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "cloud" / "rebuild_old_regions.sh"
BOX = "5.4,45.7,11.2,48.2"
OTHER = "5.0,45.0,11.0,48.0"


def _block() -> str:
    text = SCRIPT.read_text()
    i = text.index("  # A Light variant")
    return text[i:text.index("  # PRODUCTION wrapper.", i)]


def _setup(tmp: Path, *, variant_box: str = BOX, parent_places: bool = True) -> dict[str, str]:
    (tmp / "world-data" / "regions").mkdir(parents=True)
    (tmp / "overture_cache").mkdir()
    (tmp / "regions.tsv").write_text(
        f"# id\tname\tbbox\np\tParent\t{BOX}\tcountry\nv\tParent (Light)\t{variant_box}\tcountry\n"
        f"r\tPlain\t{BOX}\tcountry\n")
    (tmp / "variants.tsv").write_text("# id\tparent\tsatellite\tmax_zoom\nv\tp\tno\t13\n")
    for f in ("p.osm.pbf", "p.mbtiles", "p.search.jsonl"):
        (tmp / "world-data" / "regions" / f).write_text("x")
        (tmp / "world-data" / "regions" / f"{f}.bbox").write_text(BOX + "\n")
    for theme in ("addresses", "places"):
        if theme == "places" and not parent_places:
            continue
        pq = tmp / "overture_cache" / f"{theme}-p-rel.parquet"
        pq.write_text("x")
        pq.with_name(pq.name + ".bbox").write_text(BOX + "\n")
    bin_ = tmp / "bin"
    bin_.mkdir()
    (bin_ / "osmium").write_text("#!/bin/sh\necho \"osmium $*\" >> \"$STUB_LOG\"\n")
    (bin_ / "osmium").chmod(0o755)
    py = tmp / "py"
    # Fails, unless PY_OK is set: then it writes its --out file (a download).
    py.write_text("#!/bin/bash\necho \"python $*\" >> \"$STUB_LOG\"\n[ -n \"$PY_OK\" ] || exit 1\n"
                  "while [ $# -gt 0 ]; do [ \"$1\" = --out ] && echo x > \"$2\"; shift; done\n")
    py.chmod(0o755)
    return {"PATH": f"{bin_}:{os.environ['PATH']}", "PY": str(py), "STUB_LOG": str(tmp / "calls.log")}


def _run(tmp: Path, env: dict[str, str], rid: str) -> list[str]:
    body = f"""
. {ROOT}/ops/region-bbox.sh
log() {{ echo "LOG $*" >> "$STUB_LOG"; }}
row() {{ echo "ROW $*" >> "$STUB_LOG"; }}
flock() {{ shift; "$@"; }}
REGISTRY={tmp}/regions.tsv; VARIANTS={tmp}/variants.tsv
PLANET={tmp}/planet.osm.pbf; PBFLOCK={tmp}/lock; LOG=/dev/null; REL=rel
WORLD_MB={tmp}/world.mbtiles; WORLD_SEARCH={tmp}/world.s2.jsonl
ID={rid}
R=$(awk -F'\\t' -v id="$ID" '$1==id' "$REGISTRY" | head -1)
BBOX=$(echo "$R" | cut -f3); NAME=$(echo "$R" | cut -f2)
for _once in 1; do
{_block()}
echo "BUILD $DATA_ID" >> "$STUB_LOG"
done
"""
    subprocess.run(["bash", "-c", body], env={**os.environ, **env}, cwd=tmp,
                   check=True, capture_output=True, text=True, timeout=60)
    log = Path(env["STUB_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def _files(tmp: Path, rid: str) -> list[str]:
    return sorted(p.name for d in ("world-data/regions", "overture_cache")
                  for p in (tmp / d).iterdir() if p.name.startswith((f"{rid}.", f"addresses-{rid}-",
                                                                       f"places-{rid}-")))


def test_a_variant_uses_its_parents_files_and_makes_none_of_its_own(tmp_path):
    env = _setup(tmp_path)
    calls = _run(tmp_path, env, "v")
    assert "BUILD p" in calls, calls
    assert not [c for c in calls if c.startswith(("osmium", "python", "ROW"))], calls
    assert any("variant of p" in c for c in calls), calls
    assert _files(tmp_path, "v") == []
    # The parent's files are untouched (no re-cut, no parking, no symlink).
    regions = tmp_path / "world-data" / "regions"
    assert all(not (regions / f).is_symlink() and (regions / f).read_text() == "x"
               for f in ("p.osm.pbf", "p.mbtiles", "p.search.jsonl"))
    assert not list(regions.glob("*.stale-*"))


def test_a_variant_whose_parent_lacks_places_is_refused_for_the_parent(tmp_path):
    env = _setup(tmp_path, parent_places=False)
    calls = _run(tmp_path, env, "v")
    py = [c for c in calls if c.startswith("python")]
    assert len(py) == 1 and "--out overture_cache/places-p-rel.parquet" in py[0], calls
    assert f"--bbox={BOX}" in py[0]
    assert "ROW v no-places -" in calls and "BUILD p" not in calls, calls
    assert _files(tmp_path, "v") == []


def test_a_variant_under_another_box_is_skipped_before_any_data_step(tmp_path):
    env = _setup(tmp_path, variant_box=OTHER)
    calls = _run(tmp_path, env, "v")
    assert "ROW v variant-box -" in calls, calls
    assert not [c for c in calls if c.startswith(("osmium", "python", "BUILD"))], calls
    regions = tmp_path / "world-data" / "regions"
    assert (regions / "p.osm.pbf.bbox").read_text() == BOX + "\n"
    assert not list(regions.glob("*.stale-*"))


@pytest.mark.parametrize("variants", ["present", "missing"])
def test_a_plain_region_uses_its_own_files(tmp_path, variants):
    """Not a variant (a commented-out row does not count), or no variants
    table at all: the region's own files, as before."""
    env = {**_setup(tmp_path), "PY_OK": "1"}
    if variants == "missing":
        (tmp_path / "variants.tsv").unlink()
    calls = _run(tmp_path, env, "r")
    assert "BUILD r" in calls, calls
    assert not any("variant of" in c for c in calls), calls
    osm = [c for c in calls if c.startswith("osmium")]
    assert len(osm) == 1 and "-o world-data/regions/r.osm.pbf" in osm[0], calls
    py = [c for c in calls if c.startswith("python")]
    assert [p.split("--out ")[1].split()[0] for p in py] == [
        "overture_cache/addresses-r-rel.parquet", "overture_cache/places-r-rel.parquet"], calls
    regions = tmp_path / "world-data" / "regions"
    assert os.readlink(regions / "r.search.jsonl") == str(tmp_path / "world.s2.jsonl")


def test_the_driver_falls_back_to_the_schema_2_world_search_file():
    text = SCRIPT.read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("WORLD_SEARCH="))
    assert line.endswith(".s2.jsonl"), line
