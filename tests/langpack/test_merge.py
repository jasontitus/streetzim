"""merge_zim.py, verify_merge.py, recipe2.py and checksum_selfcheck.py on
small ZIMs: a base plus three packs merges, verifies, and its recipe
rebuilds the same bytes; the merger refuses what it must."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack.conftest import tool  # noqa: E402
from tools.langpack import merge_zim, recipe2  # noqa: E402
from tools.langpack.merge_zim import MergeError  # noqa: E402


def _packs(lp: SimpleNamespace, *langs: str) -> list[str]:
    return [str(lp.packs[x]) for x in (langs or ("de", "fr", "th"))]


def test_merge_verifies(lp: SimpleNamespace, tmp_path: Path) -> None:
    out = tmp_path / "m-de.zim"
    res = merge_zim.merge(str(lp.base), _packs(lp), str(out), "de", region_langs=["en"], title="Testland")
    assert res["metadata"]["Name"] == "osm_de_testland"
    assert res["metadata"]["Language"] == "deu,fra,tha"
    assert res["metadata"]["Flavour"] == "maxi_wiki-en-de-fr-th"
    r = tool("verify_merge.py", out, lp.base, *_packs(lp), "--lang", "de", "--region-langs", "en",
             "--title", "Testland")
    assert r.returncode == 0, r.stdout[-3000:]
    assert "FAIL" not in r.stdout
    assert r.stdout.strip().splitlines()[-1] == "SUMMARY 22/22 PASS"


def test_map_config_lists_packs_in_the_visitors_order(lp: SimpleNamespace, tmp_path: Path) -> None:
    from tools.langpack.zimfmt import ZimReader
    out = tmp_path / "m-th.zim"
    merge_zim.merge(str(lp.base), _packs(lp), str(out), "th", region_langs=["fr"], language_meta="map")
    cfg = json.loads(merge_zim.blob_of(ZimReader(str(out)), merge_zim.KEY_MAPCFG) or b"")
    assert [w["lang"] for w in cfg["wikiPacks"]] == ["th", "fr", "de"]
    assert (cfg["language"], cfg["baseLanguage"]) == ("th", "en")
    meta = merge_zim.blob_of(ZimReader(str(out)), (ord("M"), b"Language"))
    assert meta == b"tha"                      # --language-meta map: kiwix-serve's /search needs one


def test_recipe_rebuilds_the_merged_bytes(lp: SimpleNamespace, tmp_path: Path, work: Path) -> None:
    out = tmp_path / "m-fr.zim"
    merge_zim.merge(str(lp.base), _packs(lp), str(out), "fr", region_langs=["en"])
    recipe, plan, _ = recipe2.build_recipe(str(lp.base), _packs(lp), "fr", region_langs=["en"])
    rebuilt = tmp_path / "r-fr.zim"
    digest = recipe2.materialize(recipe, str(lp.base), {p.lang: p.path for p in plan.packs}, str(rebuilt))
    assert rebuilt.read_bytes() == out.read_bytes()
    assert digest == recipe["md5"] == out.read_bytes()[-16:].hex()
    import hashlib
    assert recipe["file_md5"] == hashlib.md5(out.read_bytes(), usedforsecurity=False).hexdigest()
    rp = tmp_path / "fr.json"
    rp.write_text(json.dumps(recipe))
    r = tool("checksum_selfcheck.py", rp, out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (work / "sha-cache").is_dir()        # chunk hashes cached under $LANGPACK_WORK


def test_recipe_build_by_hand_takes_the_language_meta(lp: SimpleNamespace, tmp_path: Path) -> None:
    """Review LOW: `recipe2.py build --language-meta map` gives the recipe of
    the file production ships (ship_packs and run_gates merge with map), so a
    by-hand check compares like with like."""
    out = tmp_path / "m-th.zim"
    merge_zim.merge(str(lp.base), _packs(lp), str(out), "th", region_langs=["fr"], language_meta="map")
    r = tool("recipe2.py", "build", lp.base, *_packs(lp), "--lang", "th", "--region-langs", "fr",
             "--language-meta", "map", "-o", tmp_path / "th.json", "--check", out)
    assert r.returncode == 0, r.stderr[-2000:]
    st = json.loads(r.stdout[r.stdout.index("{"):])
    assert st["materialized_equals_merge"] and st["md5"] == out.read_bytes()[-16:].hex(), st


@pytest.mark.parametrize("case", ["inline", "md5", "base_off", "swap_packs", "wrong_pack", "base_byte"])
def test_materialize_refuses_a_bad_recipe_or_input(case: str, lp: SimpleNamespace, tmp_path: Path,
                                                    work: Path) -> None:
    import base64
    recipe, plan, _ = recipe2.build_recipe(str(lp.base), _packs(lp), "de")
    packs = {p.lang: p.path for p in plan.packs}
    base = str(lp.base)
    r = copy.deepcopy(recipe)
    if case == "inline":
        s = next(s for s in r["segments"] if s["src"] == "inline" and s["len"] > 100)
        d = bytearray(base64.b64decode(s["data"]))
        d[50] ^= 1
        s["data"] = base64.b64encode(bytes(d)).decode()
    elif case == "md5":
        r["md5"] = "0" * 32
    elif case == "base_off":
        next(s for s in r["segments"] if s["src"] == "base" and s["len"] > 1000)["off"] += 1
    elif case == "swap_packs":
        segs = r["segments"]
        i, j = [k for k, s in enumerate(segs) if s["src"] == "pack"][:2]
        segs[i], segs[j] = segs[j], segs[i]
    elif case == "wrong_pack":
        packs["de"], packs["fr"] = packs["fr"], packs["de"]
    elif case == "base_byte":
        bad = tmp_path / "base.zim"
        shutil.copyfile(lp.base, bad)
        with open(bad, "r+b") as fh:
            fh.seek(recipe["base"]["range"][0] + 1234)
            b = fh.read(1)
            fh.seek(-1, 1)
            fh.write(bytes([b[0] ^ 0x20]))
        base = str(bad)
    out = tmp_path / "out.zim"
    with pytest.raises(MergeError):
        recipe2.materialize(r, base, packs, str(out))
    assert not out.exists() and not Path(str(out) + ".part").exists()


def test_merge_refuses(lp: SimpleNamespace, tmp_path: Path) -> None:
    from tests.langpack import lpzims
    # a pack built against another base (S4)
    other = tmp_path / "other.zim"
    lpzims.write_base(other, description="another base")
    foreign = tmp_path / "osm_testland_wiki-es_2026-10-13.zim"
    lpzims.write_pack(foreign, "es", lpzims.read_uuid(other), articles=[("Alfa", "Alfa")])
    with pytest.raises(MergeError, match="base_uuid"):
        merge_zim.build_plan(str(lp.base), [str(foreign)], "es")
    # a merged file is not a base
    merged = tmp_path / "m.zim"
    merge_zim.merge(str(lp.base), _packs(lp, "de"), str(merged), "de")
    with pytest.raises(MergeError, match="already a merged file"):
        merge_zim.build_plan(str(merged), _packs(lp, "fr"), "fr")
    # two packs with one prefix
    dup = tmp_path / "osm_testland_wiki-de_dup.zim"
    lpzims.write_pack(dup, "de", lp.base_uuid, articles=[("Zeta", "Zeta")])
    with pytest.raises(MergeError, match="disjoint"):
        merge_zim.build_plan(str(lp.base), [str(lp.packs["de"]), str(dup)], "de")
    # Title over 30 UTF-16 units
    with pytest.raises(MergeError, match="UTF-16"):
        merge_zim.build_plan(str(lp.base), _packs(lp, "de"), "de", title="x" * 31)


def test_long_base_description_is_replaced(lp: SimpleNamespace, tmp_path: Path) -> None:
    from tests.langpack import lpzims
    from tools.langpack.zimfmt import ZimReader
    base = tmp_path / "osm-testland-long.zim"
    bu = lpzims.write_base(base, description="d" * 120)
    pack = tmp_path / "osm_testland_wiki-de_2026-10-13.zim"
    lpzims.write_pack(pack, "de", bu)
    out = tmp_path / "m.zim"
    merge_zim.merge(str(base), [str(pack)], str(out), "de", title="Testland")
    desc = merge_zim.blob_of(ZimReader(str(out)), (ord("M"), b"Description")) or b""
    assert desc.decode() == "Testland: offline map, Wikipedia en/de"


def test_the_uuid_follows_inputs_and_generated_content(lp: SimpleNamespace, tmp_path: Path) -> None:
    a = merge_zim.build_plan(str(lp.base), _packs(lp), "de", title="Testland").header.uuid
    assert a == merge_zim.build_plan(str(lp.base), _packs(lp), "de", title="Testland").header.uuid
    assert a != merge_zim.build_plan(str(lp.base), _packs(lp), "de", title="Testland 2").header.uuid
    assert a != merge_zim.build_plan(str(lp.base), _packs(lp, "de", "fr"), "de", title="Testland").header.uuid
    assert a.hex() not in {lp.base_uuid, *lp.uuids.values()}


def test_menu_writes_a_recipe_per_language(lp: SimpleNamespace, tmp_path: Path, work: Path) -> None:
    from tests.langpack import lpzims
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    for p in lp.packs.values():
        shutil.copy(p, packs_dir / p.name)
    out = tmp_path / "recipes"
    # no pack gate yet: every pack is skipped
    r = tool("recipe2.py", "menu", lp.base, "--packs-dir", packs_dir, "--langs", "de,th", "--region-langs", "en",
             "--region", "testland", "--out-dir", out)
    rows = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    assert r.returncode == 0, r.stderr
    assert all(row["packs"] == [] for row in rows)
    for p in packs_dir.glob("*.zim"):
        g = {"ok": True, "pack_md5": lpzims.md5_of(p)}
        Path(str(p) + ".gate.json").write_text(json.dumps(g))
    r = tool("recipe2.py", "menu", lp.base, "--packs-dir", packs_dir, "--langs", "de,th", "--region-langs", "en",
             "--region", "testland", "--titles", '{"de": "Testland DE"}', "--out-dir", out)
    rows = {row["L"]: row for row in (json.loads(x) for x in r.stdout.splitlines() if x.startswith("{"))}
    assert r.returncode == 0, r.stderr
    assert rows["de"]["packs"] == ["de"] and rows["th"]["packs"] == ["th"]
    assert json.loads((out / "de.json").read_text())["title"] == "Testland DE"
    assert json.loads((out / "th.map-config.json").read_text())["language"] == "th"


def test_the_uuid_follows_the_generated_map_config(lp: SimpleNamespace) -> None:
    """Two merges whose only difference is map-config.json (the visitor's
    pack order) get different UUIDs: the UUID hashes the generated bytes."""
    a = merge_zim.build_plan(str(lp.base), _packs(lp), "th", title="T", language_meta="map", region_langs=["fr"])
    b = merge_zim.build_plan(str(lp.base), _packs(lp), "th", title="T", language_meta="map", region_langs=[])
    assert a.meta == b.meta
    assert a.layout["mapcfg"] != b.layout["mapcfg"]
    assert a.header.uuid != b.header.uuid


def _tamper(src: Path, dst: Path, off: int) -> None:
    shutil.copyfile(src, dst)
    with open(dst, "r+b") as fh:
        fh.seek(off)
        b = fh.read(1)
        fh.seek(off)
        fh.write(bytes([b[0] ^ 0x01]))


@pytest.mark.parametrize("trust_md5", [True, False])
def test_materialize_refuses_a_tampered_pack_chunk(trust_md5: bool, lp: SimpleNamespace, tmp_path: Path,
                                                    work: Path) -> None:
    """Each 16 MiB chunk is checked against the recipe's SHA-256 before use,
    so a wrong pack is refused even when the final MD5 is not compared."""
    recipe, plan, _ = recipe2.build_recipe(str(lp.base), _packs(lp), "fr")
    seg = next(s for s in recipe["segments"] if s["src"] == "pack" and s["pack"] == "fr")
    bad = tmp_path / lp.packs["fr"].name
    _tamper(lp.packs["fr"], bad, seg["off"] + seg["len"] // 2)
    packs = {p.lang: p.path for p in plan.packs} | {"fr": str(bad)}
    with pytest.raises(MergeError, match="pack fr: chunk 0 fails its SHA-256"):
        recipe2.materialize(recipe, str(lp.base), packs, str(tmp_path / "out.zim"), trust_md5=trust_md5)


@pytest.mark.parametrize("trust_md5", [True, False])
def test_materialize_refuses_a_tampered_inline_piece(trust_md5: bool, lp: SimpleNamespace, tmp_path: Path,
                                                      work: Path) -> None:
    import base64
    recipe, plan, _ = recipe2.build_recipe(str(lp.base), _packs(lp), "de")
    r = copy.deepcopy(recipe)
    i, s = next((i, s) for i, s in enumerate(r["segments"]) if s["src"] == "inline" and s["len"] > 100)
    d = bytearray(base64.b64decode(s["data"]))
    d[7] ^= 1
    s["data"] = base64.b64encode(bytes(d)).decode()
    with pytest.raises(MergeError, match=r"inline segment at \d+ fails its SHA-256"):
        recipe2.materialize(r, str(lp.base), {p.lang: p.path for p in plan.packs}, str(tmp_path / "out.zim"),
                            trust_md5=trust_md5)


def test_materialize_refuses_a_recipe_whose_file_md5_is_wrong(lp: SimpleNamespace, tmp_path: Path) -> None:
    """The recipe's file_md5 (what md5sum prints, shown to visitors) must be the
    assembled file's, even when its internal checksum is right (review r3 M-5)."""
    recipe, plan, _ = recipe2.build_recipe(str(lp.base), _packs(lp), "fr", region_langs=["en"])
    recipe["file_md5"] = "0" * 32
    with pytest.raises(merge_zim.MergeError, match="file MD5"):
        recipe2.materialize(recipe, str(lp.base), {p.lang: p.path for p in plan.packs}, str(tmp_path / "x.zim"),
                            trust_md5=True)
