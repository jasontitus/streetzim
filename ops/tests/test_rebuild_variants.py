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


def _mark(pbf: Path) -> None:
    """ops/region-bbox.sh's pbf_mark_complete: cut with --strategy smart."""
    st = pbf.stat()
    Path(str(pbf) + ".strategy").write_text(f"smart {st.st_size} {int(st.st_mtime)}\n")


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
    _mark(tmp / "world-data" / "regions" / "p.osm.pbf")
    (tmp / "world-data" / "regions" / "p.search.jsonl.schema").write_text(
        '{"search_schema": 2, "languages": []}')
    for theme in ("addresses", "places"):
        if theme == "places" and not parent_places:
            continue
        pq = tmp / "overture_cache" / f"{theme}-p-rel.parquet"
        pq.write_text("x")
        pq.with_name(pq.name + ".bbox").write_text(BOX + "\n")
    bin_ = tmp / "bin"
    bin_.mkdir()
    (bin_ / "osmium").write_text("#!/bin/sh\necho \"osmium $*\" >> \"$STUB_LOG\"\n"
                                 "while [ $# -gt 0 ]; do [ \"$1\" = -o ] && echo x > \"$2\"; shift; done\n")
    (bin_ / "osmium").chmod(0o755)
    py = tmp / "py"
    # Fails, unless PY_OK is set: then it writes its --out file (a download).
    py.write_text("#!/bin/bash\necho \"python $*\" >> \"$STUB_LOG\"\n[ -n \"$PY_OK\" ] || exit 1\n"
                  "while [ $# -gt 0 ]; do [ \"$1\" = --out ] && echo x > \"$2\"; shift; done\n")
    py.chmod(0o755)
    return {"PATH": f"{bin_}:{os.environ['PATH']}", "PY": str(py), "STUB_LOG": str(tmp / "calls.log")}


def _run(tmp: Path, env: dict[str, str], rid: str, pre: str = "", named: bool = True) -> list[str]:
    """named: OVERTURE_RELEASE names the release (rel), as an env-file pin
    does; else the driver picks: rel while its parquets are cut, else cur."""
    release = "OVERTURE_RELEASE=rel" if named else "unset OVERTURE_RELEASE"
    body = f"""
. {ROOT}/ops/region-bbox.sh
log() {{ echo "LOG $*" >> "$STUB_LOG"; }}
row() {{ echo "ROW $*" >> "$STUB_LOG"; }}
flock() {{ case "$1" in -*) return 0 ;; esac; shift; "$@"; }}
REGISTRY={tmp}/regions.tsv; VARIANTS={tmp}/variants.tsv
PLANET={tmp}/planet.osm.pbf; PBFLOCK={tmp}/lock; LOG=/dev/null; REL=rel; CUR_REL=cur
{release}
WORLD_MB={tmp}/world.mbtiles; WORLD_SEARCH={tmp}/world.s2.jsonl
ID={rid}
{pre}
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


def test_a_variant_re_cuts_its_parents_extract_when_it_is_not_complete(tmp_path):
    """The parent's extract without the --strategy smart mark (an older
    complete_ways cut): the variant's run re-cuts the parent's extract, under
    the parent's name, and marks it."""
    env = _setup(tmp_path)
    regions = tmp_path / "world-data" / "regions"
    (regions / "p.osm.pbf.strategy").unlink()
    calls = _run(tmp_path, env, "v")
    ext = [c for c in calls if c.startswith("osmium")]
    assert len(ext) == 1 and "--strategy smart" in ext[0] and "-o world-data/regions/p.osm.pbf.part" in ext[0], calls
    assert any("p.osm.pbf was cut without complete multipolygons" in c for c in calls), calls
    st = (regions / "p.osm.pbf").stat()
    assert (regions / "p.osm.pbf.strategy").read_text() == f"smart {st.st_size} {int(st.st_mtime)}\n"
    assert not (regions / "v.osm.pbf").exists() and "BUILD p" in calls


def test_a_variant_whose_parent_lacks_places_is_refused_for_the_parent(tmp_path):
    env = _setup(tmp_path, parent_places=False)
    calls = _run(tmp_path, env, "v")
    py = [c for c in calls if c.startswith("python")]
    assert len(py) == 1 and "--out overture_cache/places-p-rel.parquet" in py[0], calls
    assert f"--bbox={BOX}" in py[0] and "--release rel " in py[0]
    assert "ROW v no-places -" in calls and "BUILD p" not in calls, calls
    assert _files(tmp_path, "v") == []


@pytest.mark.parametrize("py_ok", [False, True])
def test_with_no_release_named_a_parent_without_places_takes_the_current_one(tmp_path, py_ok):
    """The round's pin left Overture's bucket: a region missing one of its
    pin parquets downloads both themes for CUR_REL, and the places check and
    the build then name that release."""
    env = {**_setup(tmp_path, parent_places=False), **({"PY_OK": "1"} if py_ok else {})}
    calls = _run(tmp_path, env, "v", named=False)
    py = [c for c in calls if c.startswith("python")]
    assert [p.split("--out ")[1].split()[0] for p in py] == [
        "overture_cache/addresses-p-cur.parquet", "overture_cache/places-p-cur.parquet"], calls
    assert all("--release cur " in p for p in py), py
    assert "LOG   overture: no rel parquets for this box; using cur" in calls, calls
    if py_ok:
        assert "BUILD p" in calls and not [c for c in calls if c.startswith("ROW")], calls
    else:
        assert "ROW v no-places -" in calls and "BUILD p" not in calls, calls
    # The pin's addresses cut stays where it was.
    assert (tmp_path / "overture_cache" / "addresses-p-rel.parquet").read_text() == "x"


def test_with_no_release_named_the_pins_cuts_are_kept(tmp_path):
    env = _setup(tmp_path)
    calls = _run(tmp_path, env, "v", named=False)
    assert "BUILD p" in calls and not [c for c in calls if c.startswith(("python", "ROW"))], calls
    assert not any("using cur" in c for c in calls), calls


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


# --- search cuts without native-script names --------------------------------

def _own_files(tmp: Path, rid: str, *, search_schema: int | None) -> Path:
    """A full, fresh set of rid's own inputs; its search cut has a .schema
    sidecar of the given schema (None: no sidecar, as the 09-06 cuts)."""
    regions = tmp / "world-data" / "regions"
    for f in (f"{rid}.osm.pbf", f"{rid}.mbtiles", f"{rid}.search.jsonl"):
        (regions / f).write_text("x")
        (regions / f"{f}.bbox").write_text(BOX + "\n")
    _mark(regions / f"{rid}.osm.pbf")
    if search_schema is not None:
        (regions / f"{rid}.search.jsonl.schema").write_text(
            '{"search_schema": %d, "languages": []}' % search_schema)
    for theme in ("addresses", "places"):
        pq = tmp / "overture_cache" / f"{theme}-{rid}-rel.parquet"
        pq.write_text("x")
        pq.with_name(pq.name + ".bbox").write_text(BOX + "\n")
    (tmp / "world.s2.jsonl").write_text("w")
    (tmp / "world.s2.jsonl.schema").write_text('{"search_schema": 2, "languages": []}')
    return regions


@pytest.mark.parametrize("schema", [None, 1])
def test_a_search_cut_without_native_names_is_parked_for_the_world_file(tmp_path, schema):
    env = _setup(tmp_path)
    regions = _own_files(tmp_path, "r", search_schema=schema)
    calls = _run(tmp_path, env, "r")
    assert "BUILD r" in calls, calls
    assert not [c for c in calls if c.startswith(("osmium", "python", "ROW"))], calls
    dst = regions / "r.search.jsonl"
    assert dst.is_symlink() and os.readlink(dst) == str(tmp_path / "world.s2.jsonl")
    _schema_copy(regions / "r.search.jsonl.schema", tmp_path)
    parked = sorted(regions.glob("r.search.jsonl.stale-*"))
    main = [p for p in parked if not p.name.endswith((".bbox", ".schema"))]
    assert len(main) == 1 and main[0].read_text() == "x"
    want = {main[0].name, main[0].name + ".bbox"} | ({main[0].name + ".schema"} if schema else set())
    assert {p.name for p in parked} == want
    assert any("no native-script names" in c for c in calls), calls
    # The tiles have no schema: kept as they are.
    assert not (regions / "r.mbtiles").is_symlink() and (regions / "r.mbtiles").read_text() == "x"


def test_a_schema_2_search_cut_for_the_box_is_kept(tmp_path):
    env = _setup(tmp_path)
    regions = _own_files(tmp_path, "r", search_schema=2)
    calls = _run(tmp_path, env, "r")
    assert "BUILD r" in calls, calls
    assert not (regions / "r.search.jsonl").is_symlink()
    assert not list(regions.glob("*.stale-*"))


def test_a_cut_that_cannot_be_parked_is_never_replaced(tmp_path):
    """ln -sfn over a regular file deletes it: if the park fails, the region
    is not built and the file stays."""
    env = _setup(tmp_path)
    regions = _own_files(tmp_path, "r", search_schema=None)
    calls = _run(tmp_path, env, "r", pre="mv() { return 1; }")
    assert "ROW r park-failed -" in calls and "BUILD r" not in calls, calls
    dst = regions / "r.search.jsonl"
    assert not dst.is_symlink() and dst.read_text() == "x"


def test_a_symlinked_cut_gets_the_world_files_schema(tmp_path):
    env = _setup(tmp_path)
    regions = _own_files(tmp_path, "r", search_schema=2)
    (regions / "r.search.jsonl").unlink()
    (regions / "r.search.jsonl.schema").unlink()
    calls = _run(tmp_path, env, "r")
    assert "BUILD r" in calls, calls
    assert os.readlink(regions / "r.search.jsonl") == str(tmp_path / "world.s2.jsonl")
    _schema_copy(regions / "r.search.jsonl.schema", tmp_path)


def _schema_copy(side: Path, tmp: Path) -> None:
    """A copy of the world file's .schema, never a symlink to it (the next
    derive's copy_schema would write through a link)."""
    assert not side.is_symlink() and side.read_text() == (tmp / "world.s2.jsonl.schema").read_text()


@pytest.mark.parametrize("which", ["search.jsonl", "mbtiles"])
def test_a_cut_for_another_box_is_parked_for_the_world_file(tmp_path, which):
    env = _setup(tmp_path)
    regions = _own_files(tmp_path, "r", search_schema=2)
    (regions / f"r.{which}.bbox").write_text(OTHER + "\n")
    calls = _run(tmp_path, env, "r")
    assert "BUILD r" in calls and any("cut for another bbox" in c for c in calls), calls
    world = tmp_path / ("world.s2.jsonl" if which == "search.jsonl" else "world.mbtiles")
    assert os.readlink(regions / f"r.{which}") == str(world)
    main = [p for p in regions.glob(f"r.{which}.stale-*") if not p.name.endswith((".bbox", ".schema"))]
    assert len(main) == 1 and main[0].read_text() == "x"
    assert (regions / (main[0].name + ".bbox")).read_text() == OTHER + "\n"
    other = "mbtiles" if which == "search.jsonl" else "search.jsonl"
    assert not (regions / f"r.{other}").is_symlink()


def test_a_later_schema_is_kept_and_a_stale_sidecar_beside_a_link_is_replaced(tmp_path):
    env = _setup(tmp_path)
    regions = _own_files(tmp_path, "r", search_schema=3)
    calls = _run(tmp_path, env, "r")
    assert "BUILD r" in calls and not (regions / "r.search.jsonl").is_symlink(), calls
    # A symlinked cut whose old sidecar is a link (as fd0c6f8 made them).
    (regions / "r.search.jsonl").unlink()
    (regions / "r.search.jsonl.schema").unlink()
    (regions / "r.search.jsonl").symlink_to(tmp_path / "world.s2.jsonl")
    (regions / "r.search.jsonl.schema").symlink_to(tmp_path / "world.s2.jsonl.schema")
    _run(tmp_path, env, "r")
    _schema_copy(regions / "r.search.jsonl.schema", tmp_path)
