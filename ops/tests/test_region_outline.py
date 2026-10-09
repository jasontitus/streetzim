"""Border outlines for region builds: cloud/region-outlines.tsv,
ops/region-outline.py and ops/region-clip.sh (build-region-fast.sh's
--clip-poly).

What must hold: a listed region gets an outline cut to its exact bbox, with
.bbox and .source sidecars, joined from every outline its row names; an
unlisted region, or CLIP=0, gets no flags and builds by the box; a variant
uses its parent's row; a listed region whose outline cannot be made stops
the build; a changed bbox or table row is picked up on the next build; a cut
off download is never used; and every table row names a real registry
region whose box --clip-poly accepts. The shell function runs for real here
(with the outlines served from local files), not just read as text.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytest.importorskip("shapely")

# An outline split at the antimeridian, as Geofabrik writes Russia's.
SPLIT = """russia
1
   170.0   60.0
   180.0   60.0
   180.0   70.0
   170.0   70.0
   170.0   60.0
END
2
   -180.0   62.0
   -175.0   62.0
   -175.0   66.0
   -180.0   66.0
   -180.0   62.0
END
END
"""
SQ_A = "a\n1\n   0 0\n   4 0\n   4 4\n   0 4\n   0 0\nEND\nEND\n"
SQ_B = "b\n1\n   4 0\n   8 0\n   8 4\n   4 4\n   4 0\nEND\nEND\n"
TABLE = "# comment\nrussia\trussia\nboth\tpart/a+part/b\n"
RUSSIA_BOX = "165.0,55.0,180.0,75.0"
BOTH_BOX = "-1.0,-1.0,9.0,5.0"


def _load():
    spec = importlib.util.spec_from_file_location("region_outline", ROOT / "ops" / "region-outline.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ro():
    return _load()


@pytest.fixture
def host(tmp_path):
    """A data root with the table, and a local 'Geofabrik' to fetch from."""
    (tmp_path / "cloud").mkdir()
    (tmp_path / "cloud" / "region-outlines.tsv").write_text(TABLE)
    geo = tmp_path / "geofabrik"
    (geo / "part").mkdir(parents=True)
    (geo / "russia.poly").write_text(SPLIT)
    (geo / "part" / "a.poly").write_text(SQ_A)
    (geo / "part" / "b.poly").write_text(SQ_B)
    return tmp_path


def _base(host):
    return (host / "geofabrik").as_uri()


def _counting(ro, calls):
    def fetch(url, dst):
        calls.append(url)
        return ro.fetch(url, dst)
    return fetch


# --- ops/region-outline.py -------------------------------------------------

def test_a_listed_region_gets_an_outline_cut_to_its_bbox(ro, host):
    calls = []
    out = ro.make("russia", RUSSIA_BOX, root=str(host), base=_base(host), fetcher=_counting(ro, calls))
    assert out == str(host / "world-data" / "regions" / "russia.clip.poly")
    assert calls == [_base(host) + "/russia.poly"]
    assert Path(out + ".bbox").read_text() == RUSSIA_BOX + "\n"
    assert Path(out + ".source").read_text() == (
        f"{calls[0]}\tsha256={hashlib.sha256(SPLIT.encode()).hexdigest()}\n")
    # What --clip-poly gets parses as it is, inside the antimeridian.
    from streetzim import clip
    g = clip.parse_poly(Path(out).read_text())
    assert g.bounds[0] == 170.0 and g.bounds[2] < 180.0
    leftovers = [p.name for p in (host / "world-data" / "regions").iterdir() if ".part" in p.name]
    assert leftovers == []


def test_a_row_naming_several_outlines_joins_them(ro, host):
    out = ro.make("both", BOTH_BOX, root=str(host), base=_base(host))
    from streetzim import clip
    g = clip.parse_poly(Path(out).read_text())
    assert g.area == pytest.approx(32.0)
    lines = Path(out + ".source").read_text().splitlines()
    assert [ln.split("\t")[0] for ln in lines] == [_base(host) + "/part/a.poly", _base(host) + "/part/b.poly"]


def test_an_unlisted_region_gets_none(ro, host):
    calls = []
    assert ro.make("brazil", "-74.0,-34.0,-32.0,5.5", root=str(host), base=_base(host),
                   fetcher=_counting(ro, calls)) is None
    assert calls == []
    assert not (host / "world-data" / "regions" / "brazil.clip.poly").exists()


def test_outlines_are_fetched_once_per_path_and_again_on_refresh(ro, host):
    calls = []
    f = _counting(ro, calls)
    ro.make("russia", RUSSIA_BOX, root=str(host), base=_base(host), fetcher=f)
    ro.make("russia", "166.0,55.0,180.0,75.0", root=str(host), base=_base(host), fetcher=f)
    assert len(calls) == 1
    assert (host / "world-data" / "regions" / "russia.clip.poly.bbox").read_text() == "166.0,55.0,180.0,75.0\n"
    ro.make("russia", RUSSIA_BOX, root=str(host), base=_base(host), fetcher=f, refresh=True)
    assert len(calls) == 2


def test_a_changed_row_fetches_the_new_outline(ro, host):
    ro.make("russia", RUSSIA_BOX, root=str(host), base=_base(host))
    (host / "geofabrik" / "other.poly").write_text(SPLIT.replace("170.0", "171.0"))
    (host / "cloud" / "region-outlines.tsv").write_text(TABLE.replace("russia\trussia", "russia\tother"))
    out = ro.make("russia", RUSSIA_BOX, root=str(host), base=_base(host))
    from streetzim import clip
    assert clip.parse_poly(Path(out).read_text()).bounds[0] == 171.0
    assert Path(out + ".source").read_text().startswith(_base(host) + "/other.poly\t")


def test_a_cut_off_outline_is_never_used(ro, host):
    cut_off = SPLIT[:SPLIT.index("2\n")]          # ends after ring 1's END
    assert not ro._complete(cut_off.encode())
    assert ro._complete(SPLIT.encode())
    (host / "geofabrik" / "russia.poly").write_text(cut_off)
    with pytest.raises(SystemExit, match="not a whole"):
        ro.make("russia", RUSSIA_BOX, root=str(host), base=_base(host))
    # A cut-off copy already in the cache is fetched again.
    (host / "geofabrik" / "russia.poly").write_text(SPLIT)
    raw = host / "world-data" / "outlines" / "russia.poly"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(cut_off)
    ro.make("russia", RUSSIA_BOX, root=str(host), base=_base(host))
    assert raw.read_text() == SPLIT


def test_a_variant_uses_its_parents_row(ro, host):
    out = ro.make("russia-light", RUSSIA_BOX, outline_id="russia", root=str(host), base=_base(host))
    assert out.endswith("russia-light.clip.poly")
    assert (host / "world-data" / "outlines" / "russia.poly").is_file()


# --- ops/region-clip.sh, run for real --------------------------------------

def _clip_args(host, rid, src, bbox, **env):
    script = (f'. "{ROOT}/ops/region-clip.sh"; region_clip_args "$1" "$2" "$3"; rc=$?; '
              'printf "rc=%s\\n" "$rc"; printf "arg=%s\\n" "${CLIP_ARGS[@]}"')
    e = dict(os.environ, STREETZIM_ROOT=str(host), PY=sys.executable,
             STREETZIM_OUTLINE_BASE=_base(host))
    e.update(env)
    if "CLIP" not in env:
        e.pop("CLIP", None)
    r = subprocess.run(["bash", "-c", script, "x", rid, src, bbox], capture_output=True, text=True, env=e)
    rc = int(re.search(r"rc=(\d+)", r.stdout).group(1))
    args = [ln[4:] for ln in r.stdout.splitlines() if ln.startswith("arg=") and ln != "arg="]
    return rc, args, r.stdout + r.stderr


def test_the_shell_function_clips_a_listed_region(host):
    rc, args, out = _clip_args(host, "russia", "russia", RUSSIA_BOX)
    poly = str(host / "world-data" / "regions" / "russia.clip.poly")
    assert rc == 0, out
    assert args == ["--clip-poly", poly, "--clip-buffer-km", "10", "--clip-min-zoom", "10"]
    assert "border clip: " + _base(host) + "/russia.poly (+10 km" in out
    assert Path(poly + ".bbox").read_text() == RUSSIA_BOX + "\n"


def test_the_shell_function_leaves_an_unlisted_region_and_clip_0_alone(host):
    rc, args, out = _clip_args(host, "brazil", "brazil", "-74.0,-34.0,-32.0,5.5")
    assert (rc, args) == (0, []) and "border clip" not in out
    rc, args, out = _clip_args(host, "russia", "russia", RUSSIA_BOX, CLIP="0")
    assert (rc, args) == (0, []) and "off (CLIP=0)" in out
    assert not (host / "world-data" / "regions" / "russia.clip.poly").exists()


def test_the_shell_function_gives_a_variant_its_parents_outline(host):
    rc, args, out = _clip_args(host, "russia-light", "russia", RUSSIA_BOX)
    assert rc == 0, out
    assert args[1] == str(host / "world-data" / "regions" / "russia-light.clip.poly")
    # A variant's own id is not looked up.
    rc, args, _ = _clip_args(host, "russia", "brazil", RUSSIA_BOX)
    assert (rc, args) == (0, [])


def test_the_shell_function_stops_the_build_when_the_outline_fails(host):
    (host / "geofabrik" / "russia.poly").unlink()
    rc, args, out = _clip_args(host, "russia", "russia", RUSSIA_BOX)
    assert rc == 1 and args == []
    assert "FATAL: no border outline for russia" in out


def test_the_shell_function_stops_when_the_outline_is_not_written(host, tmp_path):
    # region-outline.py exits 0 but the path it prints holds nothing.
    stub = tmp_path / "py"
    stub.write_text("#!/bin/sh\necho /nonexistent/russia.clip.poly\n")
    stub.chmod(0o755)
    rc, args, out = _clip_args(host, "russia", "russia", RUSSIA_BOX, PY=str(stub))
    assert rc == 1 and args == []
    assert "FATAL: no border outline for russia" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_the_shell_function_stops_when_the_table_cannot_be_read(host):
    table = host / "cloud" / "region-outlines.tsv"
    table.chmod(0)
    try:
        rc, args, out = _clip_args(host, "russia", "russia", RUSSIA_BOX)
    finally:
        table.chmod(0o644)
    assert rc == 1 and args == []
    assert "FATAL: cannot read" in out


def test_the_shell_function_remakes_the_outline_for_a_new_bbox(host):
    _clip_args(host, "russia", "russia", RUSSIA_BOX)
    rc, _, out = _clip_args(host, "russia", "russia", "166.0,55.0,180.0,75.0")
    assert rc == 0, out
    assert (host / "world-data" / "regions" / "russia.clip.poly.bbox").read_text() == "166.0,55.0,180.0,75.0\n"


# --- the wrapper and the table ---------------------------------------------

def test_the_wrapper_parses_and_adds_the_flags_before_the_build():
    sh_path = ROOT / "ops" / "build-region-fast.sh"
    assert subprocess.run(["bash", "-n", str(sh_path)]).returncode == 0
    sh = sh_path.read_text()
    assert ". /storage/streetzim/ops/region-clip.sh" in sh
    call = 'region_clip_args "$ID" "$SRC_ID" "$BBOX" || exit 1'
    add = 'ARGS+=( "${CLIP_ARGS[@]}" )'
    assert call in sh and add in sh
    assert sh.index(call) < sh.index(add) < sh.index('"$PY" "$SCRIPT" "${ARGS[@]}"')


def test_every_table_row_is_a_registry_region_with_a_box_clip_accepts():
    reg = {}
    for ln in (ROOT / "cloud" / "regions.tsv").read_text().splitlines():
        if ln and not ln.startswith("#"):
            parts = ln.split("\t")
            reg[parts[0]] = parts[2]
    rows = [ln.split("\t") for ln in (ROOT / "cloud" / "region-outlines.tsv").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert rows
    assert len({r[0] for r in rows}) == len(rows)
    for rid, paths in rows:
        assert rid in reg, rid
        w, _s, e, _n = (float(v) for v in reg[rid].split(","))
        assert -180.0 <= w <= e <= 180.0, f"{rid}: --clip-poly needs a box off the antimeridian"
        for path in paths.split("+"):
            assert re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)*", path), path
