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
    tmp.mkdir(parents=True, exist_ok=True)
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
    assert [x[0].decode() for x in t[b"announce-list"]] == lt.LANG_TRACKERS
    assert not any(b"openbittorrent" in x[0] for x in t[b"announce-list"])
    assert t[b"announce"] == b"udp://tracker.opentrackr.org:1337/announce"
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
    # cold isolate: index 1 + 3 blobs + base 2 (redirect) + base 1 + pack 2
    assert ix["subrequests"]["whole"] == 1 + 3 + 2 + 1 + 2 and ix["remote_min"] == lt.REMOTE_MIN


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
    assert res["reused"] > 0 and res["md5_checked"] and "verified" in res["reuse"], res
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
    ix2 = {"size": 100, "parts": [[0, 10, "x", "a", 0], [10, 45, "s", "base", 10], [55, 45, "x", "b", 0]]}
    assert lt.reusable_pieces(ix2, 10) == {1, 2, 3, 4}            # [50, 60) ends past the part (55)
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
    fails = [ln for ln in lines if ln.startswith("FAIL")]
    assert rc != 0 and fails and all(ln.startswith("FAIL TORRENT ") for ln in fails), lines


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


def test_a_base_torrent_wrong_in_one_piece_is_not_used(tmp_path):
    """Plan review H1: the base torrent must match the local base in EVERY
    piece (a stale one from an earlier viewer patch differs in a few)."""
    s = synth(tmp_path)
    full = run(s)
    data = bytearray(s["base"].read_bytes())
    plen = bt.auto_piece_size(len(s["merged"]))
    data[4 * plen + 7] ^= 1                                       # piece 4 differs (one of the reused ones)
    info = {"name": s["base"].name, "piece length": plen, "length": len(data), "pieces": pieces_of(bytes(data), plen)}
    (tmp_path / "stale.torrent").write_bytes(bt.bencode({"announce": "x", "info": info}))
    res = run(s, base_torrent=str(tmp_path / "stale.torrent"))
    assert res["reused"] == 0 and "piece 4 does not match" in res["reuse"], res
    assert res["infohash"] == full["infohash"]


def test_a_truncated_base_torrent_is_not_used(tmp_path):
    s = synth(tmp_path)
    data = s["base"].read_bytes()
    plen = bt.auto_piece_size(len(s["merged"]))
    info = {"name": s["base"].name, "piece length": plen, "length": len(data), "pieces": pieces_of(data, plen)[:-20]}
    (tmp_path / "short.torrent").write_bytes(bt.bencode({"announce": "x", "info": info}))
    res = run(s, base_torrent=str(tmp_path / "short.torrent"))
    assert res["reused"] == 0 and "wrong number of pieces" in res["reuse"], res


def test_dense_short_runs_get_a_higher_remote_threshold(tmp_path, monkeypatch):
    """Plan review M2: 100 runs of 300 KB, each followed by 8 inline bytes, would
    be 100 archive.org parts and 100 blobs at 256 KiB; the entry is rebuilt
    with a higher threshold until a request fits the subrequest limit."""
    s = synth(tmp_path, base_len=40_000_000, pack_len=300_000, slot=(39_000_000, 100_000))
    r = json.loads((s["site"] / "de.json").read_text())
    base = s["base"].read_bytes()
    segs, content = [], bytearray()
    for i in range(100):
        off = 1_000_000 + i * 310_000
        segs.append({"src": "base", "off": off, "len": 300_000})
        content += base[off:off + 300_000]
        sep = bytes([i % 256]) * 8
        segs.append({"src": "inline", "len": 8, "data": base64.b64encode(sep).decode(), "sha256": hashlib.sha256(sep).hexdigest()})
        content += sep
    md5 = hashlib.md5(content).hexdigest()
    segs.append({"src": "md5"})
    r.update(segments=segs, size=len(content) + 16, md5=md5, file_md5=hashlib.md5(bytes(content) + bytes.fromhex(md5)).hexdigest())
    (s["site"] / "de.json").write_text(json.dumps(r))
    at256 = lt.build_index(r, s["region"], str(s["base"]), s["packs"], "de", WEBSEED, lt.REMOTE_MIN)[0]
    assert at256["subrequests"]["whole"] > lt.SUBREQ_LIMIT
    assert at256["subrequests"]["window16m"] > lt.SUBREQ_LIMIT
    res = run(s)
    assert res["remote_min"] == 1 << 20 and res["subrequests"]["window16m"] <= lt.SUBREQ_LIMIT, res
    assert res["subrequests"]["window16m"] <= res["subrequests"]["whole"]
    monkeypatch.setattr(lt, "SUBREQ_LIMIT", 2)
    with pytest.raises(lt.TorrentError, match="subrequests"):
        run(s)
    monkeypatch.setattr(lt, "SUBREQ_LIMIT", 40)
    monkeypatch.setattr(lt, "BLOB_BUDGET", 10 << 20)                # the 1 MiB rung would put 31 MB in blobs
    with pytest.raises(lt.TorrentError, match="blobs"):
        run(s)


def test_long_runs_stay_on_archive_org(tmp_path):
    """Plan review follow-up M3: 60 runs of 2 MB with 8-byte separators fit
    every 16 MiB window at 256 KiB (8 runs + the separators' one pool blob);
    the whole file (63 subrequests) is not forced into blobs any more — the
    worker cuts large ranges and refuses a whole-file GET instead."""
    s = synth(tmp_path, base_len=130_000_000, pack_len=300_000, slot=(129_000_000, 100_000))
    r = json.loads((s["site"] / "de.json").read_text())
    base = s["base"].read_bytes()
    segs, content = [], bytearray()
    for i in range(60):
        off = 1_000_000 + i * 2_100_000
        segs.append({"src": "base", "off": off, "len": 2_000_000})
        content += base[off:off + 2_000_000]
        sep = bytes([i % 256]) * 8
        segs.append({"src": "inline", "len": 8, "data": base64.b64encode(sep).decode(), "sha256": hashlib.sha256(sep).hexdigest()})
        content += sep
    md5 = hashlib.md5(content).hexdigest()
    segs.append({"src": "md5"})
    r.update(segments=segs, size=len(content) + 16, md5=md5, file_md5=hashlib.md5(bytes(content) + bytes.fromhex(md5)).hexdigest())
    (s["site"] / "de.json").write_text(json.dumps(r))
    res = run(s)
    ix = json.loads((s["site"] / "de.ws.json").read_text())
    assert res["remote_min"] == lt.REMOTE_MIN, res
    assert res["subrequests"]["whole"] > lt.SUBREQ_LIMIT >= res["subrequests"]["window16m"], res
    assert len(ix["blobs"]) == 1 and res["blob_bytes"] == 60 * 8 + 16, res        # the separators and the md5: one pool
    assert sum(1 for p in ix["parts"] if p[2] == "s") == 60


def test_costs_counts_the_worst_window():
    M = 1 << 20
    ix = {"size": 64 * M, "parts": [[0, M, "x", "a", 0], [M, 20 * M, "s", "base", 0],
                                     [21 * M, M, "x", "b", 0], [22 * M, 42 * M, "s", "de", 0]]}
    c = lt.costs(ix)
    assert c["whole"] == 1 + 2 + 2 + 2
    assert c["window16m"] == 1 + 1 + 2 + 2          # [20 MiB, 36 MiB): base, blob b, de
    # the worst window starts at a part boundary, not at a multiple of 16 MiB
    ix2 = {"size": 32 * M, "parts": [[0, 15 * M, "s", "base", 0], [15 * M, M, "x", "b", 0],
                                      [16 * M, M, "x", "c", 0], [17 * M, 15 * M, "s", "de", 0]]}
    assert lt.subrequests(ix2, 0, 16 * M - 1) == 4 and lt.subrequests(ix2, 16 * M, 32 * M - 1) == 4
    assert lt.subrequests(ix2, 15 * M, 31 * M - 1) == 5
    # the worst: starting at the base part's last byte, [15 MiB - 1, 31 MiB - 1): base, b, c, de
    assert lt.costs(ix2)["window16m"] == 7


def test_verify_entry_checks_the_subrequest_bound(tmp_path, monkeypatch):
    s = synth(tmp_path)
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED]) == 0
    region = json.loads((s["site"] / "region.json").read_text())
    assert lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"]) == []
    monkeypatch.setattr(lt, "SUBREQ_LIMIT", 5)                      # a stricter worker: the published index no longer fits
    fails = lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"])
    assert any("subrequests" in f for f in fails), fails


def test_etags_from_the_metadata_cache(tmp_path):
    s = synth(tmp_path)
    ia = tmp_path / "ia"
    ia.mkdir()
    (ia / "ia-streetzim-testland.json").write_text(json.dumps({"metadata": {"identifier": "streetzim-testland"}, "files": [
        {"name": s["base"].name, "size": str(s["base"].stat().st_size), "mtime": "1792287864", "md5": "x"}]}))
    (ia / "ia-streetzim-testland-lang.json").write_text(json.dumps({"files": [{"name": s["pack"].name, "size": "700000"}]}))
    etags = lt.load_etags(str(ia))
    assert etags == {("streetzim-testland", s["base"].name): '"6ad42478-186a00"'}
    res = lt.torrent_for(str(s["site"] / "de.json"), s["region"], "de", str(s["base"]), s["packs"], WEBSEED, etags=etags)
    assert res["etags"] == 1
    ix = json.loads((s["site"] / "de.ws.json").read_text())
    assert ix["sources"]["base"]["etag"] == '"6ad42478-186a00"' and "etag" not in ix["sources"]["de"]
    region = s["region"] | {"menu": [{"lang": "de", "recipe": "recipes/testland/de.json", "torrent": "recipes/testland/de.torrent",
                                      "infohash": res["infohash"]}]}
    assert lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"], etags=etags) == []
    newer = dict.fromkeys(etags, '"6ad42479-186a00"')               # the base re-uploaded since
    assert lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"], etags=newer)
    assert lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"],
                           expected_origin="https://elsewhere.example")


@pytest.mark.parametrize("case", ["base_size", "region_base", "pack_url_file", "size"])
def test_build_index_refuses_inconsistent_inputs(tmp_path, case):
    s = synth(tmp_path)
    r = json.loads((s["site"] / "de.json").read_text())
    region = json.loads(json.dumps(s["region"]))
    base = str(s["base"])
    if case == "base_size":
        other = tmp_path / "other" / s["base"].name
        other.parent.mkdir()
        other.write_bytes(s["base"].read_bytes()[:-1])
        base = str(other)
    elif case == "region_base":
        region["base"]["url"] = "{proxy}/streetzim-testland/osm-testland-2026-10-14.zim"
    elif case == "pack_url_file":
        r["packs"]["de"]["url"] = "{proxy}/streetzim-testland-lang/osm_testland_wiki-fr_2026-10-13.zim"
    else:
        r["size"] += 1
    with pytest.raises(lt.TorrentError):
        lt.build_index(r, region, base, s["packs"], "de", WEBSEED)


@pytest.mark.parametrize("ws", ["ftp://host.example", "https://", "host.example", "https://h.example/x"])
def test_main_refuses_a_webseed_that_is_not_an_origin(tmp_path, ws):
    s = synth(tmp_path)
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", ws]) == 1


def test_a_partial_run_keeps_the_other_entries(tmp_path):
    s = synth(tmp_path)
    fr = json.loads((s["site"] / "de.json").read_text())
    fr.update(uuid="518f0e4d-684b-55b8-a110-d06933e1a0eb", file_name="osm_fr_testland_2026-10-13", name="osm_fr_testland")
    (s["site"] / "fr.json").write_text(json.dumps(fr))
    for name in ("region.json", "index.json"):
        doc = json.loads((s["site"] / name).read_text())
        r = doc["regions"][0] if "regions" in doc else doc
        r["menu"].append({"lang": "fr", "recipe": "recipes/testland/fr.json"})
        (s["site"] / name).write_text(json.dumps(doc))
    args = [str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED]
    assert lt.main(args) == 0
    before = json.loads((s["site"] / "region.json").read_text())["menu"]
    assert lt.main(args + ["--langs", "de"]) == 0
    after = json.loads((s["site"] / "region.json").read_text())["menu"]
    assert after[1]["torrent"] == before[1]["torrent"] and after[1]["infohash"] == before[1]["infohash"]
    assert (s["site"] / "fr.torrent").exists() and (s["site"] / "fr.ws.json").exists()


def test_a_wrong_blob_on_disk_is_rewritten(tmp_path):
    s = synth(tmp_path)
    run(s)
    ix = json.loads((s["site"] / "de.ws.json").read_text())
    f = s["site"] / next(iter(ix["blobs"].values()))["file"]
    good = f.read_bytes()
    f.write_bytes(bytes([good[0] ^ 1]) + good[1:])                 # same size, other bytes
    run(s)
    assert f.read_bytes() == good


def test_verify_entry_catches_a_tampered_name_or_middle_piece(tmp_path):
    """Every field and every piece is checked, not a sample: the torrent's
    name, and a piece in the middle (with a consistent infohash)."""
    for what in ("name", "middle"):
        s = synth(tmp_path / what)
        assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path / what), "--webseed", WEBSEED]) == 0
        region = json.loads((s["site"] / "region.json").read_text())
        t = lt.bdecode((s["site"] / "de.torrent").read_bytes())
        if what == "name":
            t[b"info"][b"name"] = b"osm_de_testland_2026-10-14.zim"
        else:
            p = bytearray(t[b"info"][b"pieces"])
            k = len(p) // 20 // 2
            p[20 * k] ^= 1
            t[b"info"][b"pieces"] = bytes(p)
        ih = hashlib.sha1(bt.bencode(t[b"info"])).hexdigest()
        region["menu"][0]["infohash"] = ih
        ix = json.loads((s["site"] / "de.ws.json").read_text())
        ix["infohash"] = ih
        (s["site"] / "de.ws.json").write_text(json.dumps(ix))
        (s["site"] / "de.torrent").write_bytes(bt.bencode(t))
        fails = lt.verify_entry(str(s["site"]), region, region["menu"][0], s["recipe"], str(s["base"]), s["packs"])
        assert fails and all(f.startswith("TORRENT de: ") for f in fails), (what, fails)


@pytest.mark.parametrize("out,want", [
    ("FAIL TORRENT de: blob x missing\nSUMMARY 9/10 checks pass -> FAIL\n", True),
    ("FAIL TORRENT de: x\nFAIL TORRENT stray files no menu entry names: []\n", True),
    ("FAIL TORRENT de: x\nFAIL de: base chunk sha256 != local base\n", False),
    ("FAIL de: verdict missing or not all_pass\n", False),
    ("SUMMARY 10/10 checks pass -> OK\n", False),
])
def test_only_torrent_failures(tmp_path, out, want):
    assert lt.only_torrent_failures(out) is want
    f = tmp_path / "check.out"
    f.write_text(out)
    assert lt.main(["x", "--only-torrent-failures", str(f)]) == (0 if want else 1)


def test_webseed_smoke_against_the_local_worker(tmp_path):
    """tools/langpack/webseed_smoke.py through build-proxy/serve-local.mjs and
    stand-ins for archive.org and the site (all on 127.0.0.1): passes, and
    fails once a blob on the site is not the one the index names."""
    import socket
    import subprocess
    import time
    from tools.langpack import webseed_e2e, webseed_smoke
    node = os.environ.get("NODE") or shutil.which("node")
    if not node:
        pytest.skip("no node")
    s = synth(tmp_path)
    assert lt.main([str(s["site"]), "--base", str(s["base"]), "--packs-dir", str(tmp_path), "--webseed", WEBSEED]) == 0
    files = {f"streetzim-testland/{s['base'].name}": str(s["base"]),
             f"streetzim-testland-lang/{s['pack'].name}": str(s["pack"])}
    counter = {"download": 0, "node": 0, "site": 0}
    srv = webseed_e2e.ranged_server(files, str(s["site"]), "testland", counter)
    port = srv.server_address[1]
    wport = webseed_e2e.free_port()
    env = {**os.environ, "SITE_ORIGIN": f"http://127.0.0.1:{port}", "UPSTREAM": f"http://127.0.0.1:{port}/download/"}
    w = subprocess.Popen([node, str(Path(lt.ROOT) / "build-proxy" / "serve-local.mjs"), str(wport)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", wport), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        args = ["--site", f"http://127.0.0.1:{port}", "--region", "testland", "--webseed", WEBSEED,
                "--via", f"http://127.0.0.1:{wport}", "--pieces", "4"]
        assert webseed_smoke.main(args) == 0
        assert webseed_smoke.main(args[:4] + ["--webseed", "https://other.example"] + args[6:]) == 1
        ix = json.loads((s["site"] / "de.ws.json").read_text())
        f = s["site"] / ix["blobs"][ix["parts"][0][3]]["file"]           # the header blob (piece 0)
        f.write_bytes(bytes([f.read_bytes()[0] ^ 1]) + f.read_bytes()[1:])
        w.terminate()
        w.wait(timeout=10)
        w = subprocess.Popen([node, str(Path(lt.ROOT) / "build-proxy" / "serve-local.mjs"), str(wport)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", wport), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        assert webseed_smoke.main(args) == 1
    finally:
        w.terminate()
        w.wait(timeout=10)
        srv.shutdown()
