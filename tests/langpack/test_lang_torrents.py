"""tools/langpack/lang_torrents.py: language torrents and the build worker's
segment index (streetzim-webseed/1).

Synthetic recipes (no libzim) for the index/hashing logic; one real recipe
from the session's small base and pack (recipe2.build_recipe) checked against
recipe2.materialize byte for byte."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.cloud import build_torrent as bt
from tools.langpack import lang_torrents as lt
from tools.langpack.merge_zim import MergeError
from tools.langpack.recipe2 import FORMAT, masked

CHUNK = 64 << 10
WEBSEED = "https://build.example.workers.dev"


def chunk_hashes(data: bytes, mask=None) -> list[str]:
    return [hashlib.sha256(masked(data[i:i + CHUNK], i, mask)).hexdigest() for i in range(0, len(data), CHUNK)]


def synth(tmp: Path, *, base_len=1_600_000, pack_len=700_000, slot=(600_000, 200_000), seed=7):
    """A base, a pack and a recipe whose merged file is
    inline header | base (identity) | inline viewer slot | base (identity) |
    pack | a run of short base reads and inline bytes | md5."""
    rnd = random.Random(seed)
    base = bytes(rnd.getrandbits(8) for _ in range(base_len))
    pack = bytes(rnd.getrandbits(8) for _ in range(pack_len))
    bpath, ppath = tmp / "osm-testland-2026-10-13.zim", tmp / "osm_testland_wiki-de_2026-10-13.zim"
    bpath.write_bytes(base)
    ppath.write_bytes(pack)
    so, sl = slot
    mask = [[so, sl], [base_len - 16, 16]]
    header = bytes(rnd.getrandbits(8) for _ in range(80))
    slot_bytes = bytes(rnd.getrandbits(8) for _ in range(sl))      # "the viewer the recipe carries"
    segs = [("inline", header), ("base", 80, so - 80), ("inline", slot_bytes), ("base", so + sl, base_len - 16 - so - sl),
            ("pack", 2048, 400_000)]
    off = 1_000
    for i in range(40):                                            # dirent-like tail: short base runs + inline
        segs.append(("base", off, 300 + 13 * i))
        segs.append(("inline", bytes([i]) * (4 + i % 7)))
        off += 9_000
    segs.append(("pack", 450_000, 100_000))                       # a short pack read
    out_segs, content = [], bytearray()
    for s in segs:
        if s[0] == "inline":
            out_segs.append({"src": "inline", "len": len(s[1]), "data": base64.b64encode(s[1]).decode(),
                             "sha256": hashlib.sha256(s[1]).hexdigest()})
            content += s[1]
        elif s[0] == "base":
            out_segs.append({"src": "base", "off": s[1], "len": s[2]})
            content += base[s[1]:s[1] + s[2]]
        else:
            out_segs.append({"src": "pack", "pack": "de", "off": s[1], "len": s[2]})
            content += pack[s[1]:s[1] + s[2]]
    md5 = hashlib.md5(content).hexdigest()
    out_segs.append({"src": "md5"})
    merged = bytes(content) + bytes.fromhex(md5)
    recipe = {
        "format": FORMAT, "merger": "test", "uuid": "418f0e4d-684b-55b8-a110-d06933e1a0eb",
        "name": "osm_de_testland", "file_name": "osm_de_testland_2026-10-13", "size": len(merged),
        "md5": md5, "file_md5": hashlib.md5(merged).hexdigest(),
        "base": {"file": bpath.name, "size": base_len, "uuid": "00" * 16, "chunk": CHUNK,
                 "sha256": chunk_hashes(base, mask), "mask": mask, "slots": []},
        "packs": {"de": {"file": ppath.name, "uuid": "11" * 16, "url": "{proxy}/streetzim-testland-lang/" + ppath.name,
                         "size": pack_len, "chunk": CHUNK, "articles": 3, "sha256": chunk_hashes(pack)}},
        "segments": out_segs,
    }
    region = {"id": "testland", "title": "Testland",
              "base": {"file": bpath.name, "size": base_len,
                       "download": f"https://archive.org/download/streetzim-testland/{bpath.name}",
                       "url": f"{{proxy}}/streetzim-testland/{bpath.name}"},
              "menu": [{"lang": "de", "recipe": "recipes/testland/de.json"}]}
    site = tmp / "site"
    site.mkdir()
    (site / "de.json").write_text(json.dumps(recipe))
    (site / "region.json").write_text(json.dumps(region))
    (site / "index.json").write_text(json.dumps({"format": "streetzim-build-index/1", "regions": [region]}))
    return {"base": bpath, "pack": ppath, "merged": merged, "recipe": recipe, "region": region, "site": site,
            "packs": {"de": str(ppath)}}


def pieces_of(data: bytes, plen: int) -> bytes:
    return b"".join(hashlib.sha1(data[i:i + plen]).digest() for i in range(0, len(data), plen))


def run(s, **kw):
    return lt.torrent_for(str(s["site"] / "de.json"), s["region"], "de", str(s["base"]), s["packs"], WEBSEED, **kw)


def test_full_torrent_is_the_merged_file(tmp_path):
    s = synth(tmp_path)
    res = run(s)
    assert res["md5_checked"] and res["reused"] == 0
    t = lt.bdecode((s["site"] / "de.torrent").read_bytes())
    info = t[b"info"]
    plen = bt.auto_piece_size(len(s["merged"]))
    assert info[b"name"] == b"osm_de_testland_2026-10-13.zim"
    assert info[b"length"] == len(s["merged"]) and info[b"piece length"] == plen
    assert info[b"pieces"] == pieces_of(s["merged"], plen)
    assert t[b"url-list"] == [f"{WEBSEED}/merged/testland/de/{s['recipe']['uuid']}/osm_de_testland_2026-10-13.zim".encode()]
    assert [x[0].decode() for x in t[b"announce-list"]] == bt.TRACKERS
    assert res["infohash"] == hashlib.sha1(bt.bencode(info)).hexdigest()


def test_index_reproduces_the_bytes_and_coalesces_short_runs(tmp_path):
    s = synth(tmp_path)
    run(s)
    ix = json.loads((s["site"] / "de.ws.json").read_text())
    pos = 0
    out = bytearray()
    for start, n, kind, ref, off in ix["parts"]:
        assert start == pos and n > 0
        if kind == "x":
            blob = (s["site"] / ix["blobs"][ref]["file"]).read_bytes()
            assert hashlib.sha256(blob).hexdigest() == ref
            out += blob[off:off + n]
        else:
            src = s["base"] if ref == "base" else s["pack"]
            assert ix["sources"][ref]["file"] == src.name
            out += src.read_bytes()[off:off + n]
            assert n >= lt.REMOTE_MIN                     # short reads are never remote
        pos += n
    assert bytes(out) == s["merged"]
    assert ix["sources"]["base"]["item"] == "streetzim-testland"
    assert ix["sources"]["de"]["item"] == "streetzim-testland-lang"
    # 1 header blob, 2 remote base parts, 1 slot blob, 1 remote pack part, 1 tail blob
    kinds = [p[2] for p in ix["parts"]]
    assert kinds == ["x", "s", "x", "s", "s", "x"], kinds
    assert len(ix["parts"]) <= 8 and len(json.dumps(ix)) < 4096


def test_blobs_split_at_blob_max(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "BLOB_MAX", 64 << 10)
    s = synth(tmp_path)
    run(s)
    ix = json.loads((s["site"] / "de.ws.json").read_text())
    sizes = [b["size"] for b in ix["blobs"].values()]
    assert max(sizes) <= 64 << 10 and len(sizes) > 3


def test_reused_base_hashes_equal_full_hashing(tmp_path):
    s = synth(tmp_path)
    full = run(s)
    full_t = (s["site"] / "de.torrent").read_bytes()
    bt.build_torrent("https://archive.org/download/streetzim-testland/" + s["base"].name,
                     str(tmp_path / "base.torrent"), local_path=str(s["base"]))
    res = run(s, base_torrent=str(tmp_path / "base.torrent"))
    assert res["reused"] > 0 and not res["md5_checked"], res
    assert res["hashed_bytes"] < full["hashed_bytes"]
    assert lt.bdecode((s["site"] / "de.torrent").read_bytes())[b"info"] == lt.bdecode(full_t)[b"info"]
    assert res["infohash"] == full["infohash"]


def test_reuse_needs_this_base(tmp_path):
    s = synth(tmp_path)
    full = run(s)
    other = tmp_path / "other" / s["base"].name
    other.parent.mkdir()
    other.write_bytes(bytes(len(s["base"].read_bytes())))           # same name and size, other bytes
    bt.build_torrent("https://x/" + other.name, str(tmp_path / "other.torrent"), local_path=str(other))
    res = run(s, base_torrent=str(tmp_path / "other.torrent"))
    assert res["reused"] == 0 and res["md5_checked"] and "does not match" in res["reuse"]
    assert res["infohash"] == full["infohash"]
    renamed = tmp_path / "renamed" / "osm-elsewhere-2026-10-13.zim"
    renamed.parent.mkdir()
    shutil.copy(s["base"], renamed)
    bt.build_torrent("https://x/" + renamed.name, str(tmp_path / "renamed.torrent"), local_path=str(renamed))
    res = run(s, base_torrent=str(tmp_path / "renamed.torrent"))
    assert res["reused"] == 0 and "not osm-testland" in res["reuse"]


def test_full_flag_ignores_the_base_torrent(tmp_path):
    s = synth(tmp_path)
    bt.build_torrent("https://x/" + s["base"].name, str(tmp_path / "b.torrent"), local_path=str(s["base"]))
    res = run(s, base_torrent=str(tmp_path / "b.torrent"), full=True)
    assert res["reused"] == 0 and res["md5_checked"]


def test_a_changed_base_byte_is_refused(tmp_path):
    s = synth(tmp_path)
    b = bytearray(s["base"].read_bytes())
    b[300_000] ^= 1                                              # inside an identity part
    s["base"].write_bytes(bytes(b))
    with pytest.raises(MergeError, match="chunk"):
        run(s)


def test_a_changed_inline_segment_is_refused(tmp_path):
    s = synth(tmp_path)
    r = json.loads((s["site"] / "de.json").read_text())
    d = bytearray(base64.b64decode(r["segments"][0]["data"]))
    d[0] ^= 1
    r["segments"][0]["data"] = base64.b64encode(bytes(d)).decode()
    (s["site"] / "de.json").write_text(json.dumps(r))
    with pytest.raises(lt.TorrentError, match="inline segment"):
        run(s)


def test_a_wrong_md5_is_refused(tmp_path):
    s = synth(tmp_path)
    r = json.loads((s["site"] / "de.json").read_text())
    r["file_md5"] = "0" * 32
    (s["site"] / "de.json").write_text(json.dumps(r))
    with pytest.raises(lt.TorrentError, match="file_md5"):
        run(s)


def test_masked_base_reads_are_refused(tmp_path):
    s = synth(tmp_path)
    r = json.loads((s["site"] / "de.json").read_text())
    r["base"]["mask"] = [[100, 10]]                              # inside the first identity part
    (s["site"] / "de.json").write_text(json.dumps(r))
    with pytest.raises(lt.TorrentError, match="masked"):
        run(s)


@pytest.mark.parametrize("url,ok", [
    ("{proxy}/streetzim-greece-lang/osm_greece_wiki-el_2026-10-21.zim", True),
    ("https://archive.org/download/streetzim-greece/osm-greece-2026-10-15.zim", True),
    ("{proxy}/osm_greece_wiki-el_2026-10-21.zim", False),          # no item
    ("{proxy}/other-item/x.zim", False),
    ("https://evil.example/download/streetzim-x/y.zim", False),
    ("{proxy}/streetzim-x/y.txt", False),
])
def test_item_of(url, ok):
    if ok:
        assert lt.item_of(url, "t")[0].startswith("streetzim-")
    else:
        with pytest.raises(lt.TorrentError):
            lt.item_of(url, "t")


def test_main_annotates_the_menu_and_prunes(tmp_path):
    s = synth(tmp_path)
    stale = s["site"] / ("ws-" + "f" * 32 + ".bin")
    stale.write_bytes(b"stale")
    (s["site"] / "xx.torrent").write_bytes(b"stale")
    rc = lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED])
    assert rc == 0
    for name in ("region.json", "index.json"):
        doc = json.loads((s["site"] / name).read_text())
        m = (doc["regions"][0] if "regions" in doc else doc)["menu"][0]
        assert m["torrent"] == "recipes/testland/de.torrent" and len(m["infohash"]) == 40
    assert not stale.exists() and not (s["site"] / "xx.torrent").exists()
    region = json.loads((s["site"] / "region.json").read_text())
    assert lt.stray_files(str(s["site"]), region) == []
    assert lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"]) == []


def test_main_refuses_a_webseed_with_a_path(tmp_path):
    s = synth(tmp_path)
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path),
                    "--webseed", WEBSEED + "/merged"]) == 1


def test_a_failed_language_loses_its_torrent(tmp_path):
    s = synth(tmp_path)
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED]) == 0
    s["pack"].write_bytes(b"x" * len(s["pack"].read_bytes()))       # the pack changed: chunk check fails
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED]) == 3
    region = json.loads((s["site"] / "region.json").read_text())
    assert "torrent" not in region["menu"][0]
    assert not list(s["site"].glob("*.torrent")) and not list(s["site"].glob("ws-*.bin"))


@pytest.mark.parametrize("tamper", ["blob", "torrent_webseed", "index_part", "infohash", "piece"])
def test_verify_entry_catches_tampering(tmp_path, tamper):
    s = synth(tmp_path)
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED]) == 0
    region = json.loads((s["site"] / "region.json").read_text())
    ix = json.loads((s["site"] / "de.ws.json").read_text())
    if tamper == "blob":
        f = s["site"] / next(iter(ix["blobs"].values()))["file"]
        f.write_bytes(b"\0" + f.read_bytes()[1:])
    elif tamper == "index_part":
        ix["parts"][1][4] += 1
        (s["site"] / "de.ws.json").write_text(json.dumps(ix))
    elif tamper == "infohash":
        region["menu"][0]["infohash"] = "0" * 40
    else:
        t = lt.bdecode((s["site"] / "de.torrent").read_bytes())
        if tamper == "torrent_webseed":
            t[b"url-list"] = [b"https://elsewhere.example/x.zim"]
        else:
            p = bytearray(t[b"info"][b"pieces"])
            p[0] ^= 1
            t[b"info"][b"pieces"] = bytes(p)
            region["menu"][0]["infohash"] = hashlib.sha1(bt.bencode(t[b"info"])).hexdigest()
            ix["infohash"] = region["menu"][0]["infohash"]
            (s["site"] / "de.ws.json").write_text(json.dumps(ix))
        (s["site"] / "de.torrent").write_bytes(bt.bencode(t))
    fails = lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"])
    assert fails, tamper


def test_reusable_pieces_only_inside_identity_base_parts():
    ix = {"size": 100, "parts": [[0, 10, "x", "a", 0], [10, 50, "s", "base", 10], [60, 20, "s", "base", 99],
                                 [80, 20, "s", "de", 80]]}
    assert lt.reusable_pieces(ix, 10) == {1, 2, 3, 4, 5}
    assert lt.reusable_pieces(ix, 20) == {1, 2}                   # [20, 40) and [40, 60); [0, 20) starts before the part
    ix["parts"][1] = [10, 50, "s", "base", 11]                    # not at its own offset
    assert lt.reusable_pieces(ix, 10) == set()


# ---------- a real recipe (recipe2.build_recipe on the session's base and pack) ----------

def test_real_recipe_torrent_matches_materialize(lp, work, tmp_path):
    from tools.langpack import recipe2
    recipe, plan, st = recipe2.build_recipe(str(lp.base), [str(lp.packs["de"])], "de",
                                            url_template="{proxy}/streetzim-testland-lang/{file}")
    site = tmp_path / "site"
    site.mkdir()
    (site / "de.json").write_text(json.dumps(recipe))
    region = {"id": "testland", "title": "Testland",
              "base": {"file": lp.base.name, "size": os.path.getsize(lp.base),
                       "download": f"https://archive.org/download/streetzim-testland/{lp.base.name}",
                       "url": f"{{proxy}}/streetzim-testland/{lp.base.name}"},
              "menu": [{"lang": "de", "recipe": "recipes/testland/de.json"}]}
    (site / "region.json").write_text(json.dumps(region))
    out = tmp_path / "merged.zim"
    recipe2.materialize(recipe, str(lp.base), {"de": str(lp.packs["de"])}, str(out))
    res = lt.torrent_for(str(site / "de.json"), region, "de", str(lp.base), {"de": str(lp.packs["de"])}, WEBSEED)
    assert res["md5_checked"]
    info = lt.bdecode((site / "de.torrent").read_bytes())[b"info"]
    assert info[b"pieces"] == pieces_of(out.read_bytes(), info[b"piece length"])
    assert info[b"length"] == out.stat().st_size == recipe["size"]


# ---------- site_check.py on a site with torrents (make_site's real output) ----------

@pytest.fixture(scope="module")
def tsite(lp, tmp_path_factory):
    from tests.langpack import lpzims
    from tests.langpack.conftest import tool
    from tests.langpack.test_site import LANGS, TITLES, _gate_outputs
    d = tmp_path_factory.mktemp("tsite")
    work = d / "work"
    env = {"LANGPACK_WORK": str(work)}
    packs = d / "packs"
    packs.mkdir()
    zc = lpzims.fake_zimcheck(d / "zc", "clean", None)
    for p in lp.packs.values():
        shutil.copy(p, packs / p.name)
        assert tool("pack_gate.py", packs / p.name, "--base", lp.base, env={**env, "PACK_GATE_ZIMCHECK": zc}).returncode == 0
    tag = lp.base.name[:-4]
    r = tool("recipe2.py", "menu", lp.base, "--packs-dir", packs, "--langs", LANGS, "--region-langs", "en",
             "--region", "testland", "--titles", TITLES, "--language-meta", "map",
             "--out-dir", work / "recipes" / tag, env=env)
    assert r.returncode == 0, r.stderr
    _gate_outputs(d / "gates", r.stdout, LANGS.split(","), base=lp.base)
    assert tool("compose_verdicts.py", d / "gates", lp.base, LANGS, env=env).returncode == 0
    out = d / "site" / "recipes" / "testland"
    r = tool("make_site.py", lp.base, "--packs-dir", packs, "--region", "testland", "--region-title", "Testland",
             "--region-langs", "en", "--langs", LANGS, "--titles", TITLES, "--language-meta", "map",
             "--url-template", "{proxy}/streetzim-testland-lang/{file}", "--recipe-prefix", "recipes/testland/",
             "--base-download", f"https://archive.org/download/streetzim-testland/{lp.base.name}",
             "--base-url", f"{{proxy}}/streetzim-testland/{lp.base.name}", "-o", out, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    ia = d / "ia"
    ia.mkdir()
    for item, files in {"streetzim-testland-lang": list(packs.glob("*.zim")), "streetzim-testland": [lp.base]}.items():
        meta = {"metadata": {"identifier": item},
                "files": [{"name": f.name, "size": str(f.stat().st_size), "md5": lpzims.md5_of(f)} for f in files]}
        (ia / f"ia-{item}.json").write_text(json.dumps(meta))
    r = tool("lang_torrents.py", out, "--base", lp.base, "--packs-dir", packs, "--webseed", WEBSEED, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return SimpleNamespace(dir=d, env=env, packs=packs, out=out, ia=ia, base=lp.base)


def _site_check(s, out):
    from tests.langpack.conftest import tool
    r = tool("site_check.py", out.parent.parent, "--recipes-dir", out, "--base", s.base, "--packs-dir", s.packs,
             "--ia-cache", s.ia, env=s.env)
    return r.returncode, [ln for ln in r.stdout.splitlines() if ln.startswith(("FAIL", "SUMMARY"))]


def test_site_check_passes_with_torrents(tsite):
    region = json.loads((tsite.out / "region.json").read_text())
    assert all(m.get("torrent") == f"recipes/testland/{m['lang']}.torrent" for m in region["menu"])
    rc, lines = _site_check(tsite, tsite.out)
    assert rc == 0, lines


@pytest.mark.parametrize("tamper", ["blob", "stray_blob", "torrent_gone_from_menu", "index"])
def test_site_check_fails_bad_torrent_files(tsite, tmp_path, tamper):
    s = tmp_path / "site" / "recipes" / "testland"
    shutil.copytree(tsite.out, s)
    ix = json.loads((s / "de.ws.json").read_text())
    if tamper == "blob":
        f = s / next(iter(ix["blobs"].values()))["file"]
        f.write_bytes(bytes([f.read_bytes()[0] ^ 1]) + f.read_bytes()[1:])
    elif tamper == "stray_blob":
        (s / ("ws-" + "a" * 32 + ".bin")).write_bytes(b"x")
    elif tamper == "torrent_gone_from_menu":
        for name in ("region.json", "index.json"):
            doc = json.loads((s / name).read_text())
            r = doc["regions"][0] if "regions" in doc else doc
            r["menu"][0].pop("torrent")
            (s / name).write_text(json.dumps(doc))
    else:
        ix["size"] += 1
        (s / "de.ws.json").write_text(json.dumps(ix))
    rc, lines = _site_check(tsite, s)
    assert rc != 0 and any("torrent" in ln or "ws.json" in ln or "blob" in ln for ln in lines), lines


def test_strip_removes_every_torrent_file_and_field(tmp_path):
    s = synth(tmp_path)
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED]) == 0
    assert list(s["site"].glob("*.torrent"))
    assert lt.main([str(s["site"]), "--strip"]) == 0
    assert not list(s["site"].glob("*.torrent")) and not list(s["site"].glob("*.ws.json"))
    assert not list(s["site"].glob("ws-*.bin"))
    for name in ("region.json", "index.json"):
        doc = json.loads((s["site"] / name).read_text())
        m = (doc["regions"][0] if "regions" in doc else doc)["menu"][0]
        assert "torrent" not in m and "infohash" not in m
    assert (s["site"] / "de.json").exists()                       # the recipe stays


def test_reuse_needs_the_same_piece_length(tmp_path):
    s = synth(tmp_path)
    full = run(s)
    plen = bt.auto_piece_size(len(s["merged"])) * 2                # a base torrent with other pieces
    data = s["base"].read_bytes()
    info = {"name": s["base"].name, "piece length": plen, "length": len(data), "pieces": pieces_of(data, plen)}
    (tmp_path / "b2.torrent").write_bytes(bt.bencode({"announce": bt.TRACKERS[0], "info": info}))
    res = run(s, base_torrent=str(tmp_path / "b2.torrent"))
    assert res["reused"] == 0 and "piece length" in res["reuse"], res
    assert res["infohash"] == full["infohash"]
