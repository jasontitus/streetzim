"""streetzim-langpack (streetzim/langpack_cli.py): its flags per mode, its
Zimfarm definition (offliner-definition-langpack.json), the Wikipedia ZIM
lookup, downloads checked against Kiwix's .meta4, process trees stopped,
--mode pack with stand-in tools, and --mode menu end to end on a small base
and packs (no network, a stand-in zimcheck)."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from streetzim import langpack_cli as lc  # noqa: E402

DEF = json.loads((ROOT / "offliner-definition-langpack.json").read_text())
PK = "https://e.org/zim/maps/osm_x_wiki-{}_2026-10-23_ab12cd34-0123456789ab.zim"


def _argv_for(config: dict) -> list[str]:
    """What Zimfarm runs for a flags config (compute_flags, use_equals=True):
    booleans as bare flags when true, a list as one flag per item."""
    argv = []
    for key, value in config.items():
        opt = "--" + key.replace("_", "-")
        if DEF["flags"][key]["type"] == "boolean":
            if value:
                argv.append(opt)
        elif isinstance(value, list):
            argv += [f"{opt}={v}" for v in value]
        else:
            argv.append(f"{opt}={value}")
    return argv


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------ the definition

def test_committed_definition_is_current():
    od = pytest.importorskip("offliner_definition_langpack")
    assert (ROOT / "offliner-definition-langpack.json").read_text() == od.render(), \
        "run: python tools/offliner_definition_langpack.py"


def test_definition_shape():
    assert DEF["offliner_id"] == "streetzim-langpack"
    assert DEF["stdOutput"] is True and DEF["stdStats"] is True
    flags = DEF["flags"]
    assert flags["output"]["pattern"] == "^/output$"
    assert flags["stats_filename"]["pattern"] == r"^/output/task_progress\.json$"
    assert {k for k, f in flags.items() if f["required"]} == {"mode", "base_url"}
    assert flags["base_url"]["type"] == "url" and flags["wikipedia_zim_url"]["type"] == "url"
    assert flags["pack_url"]["type"] == "list-of-url"
    assert flags["images"]["choices"] == ["none", "lead", "all"]
    # each mode's own flags are its choice's dependents, and nothing else is
    mode = {c["value"]: c["dependents"] for c in flags["mode"]["choices"]}
    assert mode == lc.MODE_FLAGS
    assert all(d in flags for deps in mode.values() for d in deps)
    for dev in ("tmp", "dl", "zimcheck", "overwrite", "keep_temp"):
        assert dev not in flags


def test_every_offered_flag_parses_in_its_mode():
    common = {"base_url": "https://example.org/osm_mul_x_2026-10.zim", "region": "x", "jobs": 2,
              "output": "/output", "stats_filename": "/output/task_progress.json"}
    a = lc.parse_args(_argv_for({"mode": "pack", **common, "language": "de",
                                 "wikipedia_zim_url": "https://example.org/kiwix/zim/wikipedia/w.zim",
                                 "images": "all", "no_wikidata_text": True}))
    assert (a.mode, a.language, a.images, a.no_wikidata_text, a.jobs) == ("pack", "de", "all", True, 2)
    m = lc.parse_args(_argv_for({"mode": "menu", **common, "pack_url": [PK.format("de"), PK.format("fr")],
                                 "languages": "de,fr", "url_template": "https://e.org/{file}"}))
    assert m.pack_url == [PK.format("de"), PK.format("fr")] and m.region == "x"
    assert (m.languages, m.url_template) == ("de,fr", "https://e.org/{file}")


@pytest.mark.parametrize("argv, why", [
    (["--mode=pack", "--base-url=b"], "needs --language"),
    (["--mode=pack", "--base-url=b", "--language=xx"], "xx"),
    (["--mode=pack", "--base-url=b", "--language=DE"], "not a language code"),
    (["--mode=pack", "--base-url=b", "--language=de", f"--pack-url={PK.format('de')}"], "for --mode menu"),
    (["--mode=menu", "--base-url=b"], "at least one --pack-url"),
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}", "--language=de"], "for --mode pack"),
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}", "--images=all"], "for --mode pack"),
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}", "--url-template=https://e/x.zim"],
     "{file}"),
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}", "--url-template=https://e/{file}/{x}"],
     "{file}"),
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}", "--languages=de;fr"], "comma"),
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}", "--region=Bad_Id"], "region id"),
    # one pack per language
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}",
      "--pack-url=https://f.org/osm_x_wiki-de_2026-10-24_ab12cd34-ffffffffffff.zim"], "two packs for de"),
    (["--mode=menu", "--base-url=b", "--pack-url=https://e.org/pack.zim"], "not a pack name"),
    # the region the names give
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}", "--region=y"], "region ids disagree"),
    (["--mode=menu", "--base-url=b", f"--pack-url={PK.format('de')}",
      "--pack-url=https://e.org/osm_y_wiki-fr_2026-10-23_ab12cd34.zim"], "region ids disagree"),
    (["--mode=menu", "--base-url=https://e.org/osm-y-2026-10-23.zim", f"--pack-url={PK.format('de')}"],
     "region ids disagree"),
    (["--mode=pack", "--base-url=https://e.org/osm_mul_y_2026-10.zim", "--language=de", "--region=x"],
     "region ids disagree"),
    # Kiwix URLs: no query or fragment, a /zim/ path whose .meta4 download.kiwix.org gives
    (["--mode=pack", "--base-url=b", "--language=de",
      "--wikipedia-zim-url=https://download.kiwix.org/zim/wikipedia/w.zim?x=1"], "query or fragment"),
    (["--mode=pack", "--base-url=b", "--language=de",
      "--wikipedia-zim-url=https://mirror.example/w.zim#frag"], "query or fragment"),
    (["--mode=pack", "--base-url=b", "--language=de", "--wikipedia-zim-url=https://mirror.example/w.zim"],
     "no /zim/ path"),
    (["--mode=menu", "--base-url=b", "--pack-url=https://download.kiwix.org/zim/maps/"
      "osm_x_wiki-de_2026-10-23_ab.zim?mirror=1"], "query or fragment"),
    # temporary ZIMs never under --output
    (["--mode=pack", "--base-url=b", "--language=de", "--output=/o", "--tmp=/o/t"], "inside --output"),
    (["--mode=pack", "--base-url=b", "--language=de", "--output=/o", "--dl=/o"], "inside --output"),
])
def test_wrong_flags_fail_before_anything_runs(argv, why, capsys):
    with pytest.raises(SystemExit) as e:
        lc.parse_args(argv)
    assert e.value.code == 2 and why in capsys.readouterr().err


def test_mode_word_and_defaults():
    a = lc.parse_args(["pack", "--base-url=b", "--language=fr"])
    assert (a.mode, a.images, a.url_template, a.region) == ("pack", "lead", "{file}", None)
    m = lc.parse_args(["menu", "--base-url=b", f"--pack-url={PK.format('de')}"])
    assert (m.mode, m.region) == ("menu", "x")
    assert lc.parse_args(["pack", "--base-url=/d/osm-monaco-2026-10-23.zim", "--language=fr"]).region == "monaco"
    assert lc.parse_args(["pack", "--base-url=https://e.org/zim/maps/osm_mul_new-york_2026-10.zim",
                          "--language=fr"]).region == "new-york"


def test_zimfarm_accepts_the_definition_and_its_recipes():
    """Zimfarm's own models (zimfarm_backend on PYTHONPATH; CI pins the
    commit): the definition, a recipe per mode, the other mode's flags
    cleared on save, and the command Zimfarm builds parses here."""
    for key in ("POSTGRES_URI", "JWT_SECRET"):
        os.environ.setdefault(key, "unused")
    models = pytest.importorskip("zimfarm_backend.common.schemas.offliners.models")
    from pydantic import ValidationError
    from zimfarm_backend.common.schemas.offliners.builder import build_offliner_model
    from zimfarm_backend.common.schemas.orms import OfflinerSchema
    from zimfarm_backend.utils.offliners import clear_unset_choices_dependents, compute_flags
    spec = models.OfflinerSpecSchema.model_validate(DEF)
    off = OfflinerSchema.model_construct(
        id="streetzim-langpack", base_model="DashModel", docker_image_name="openzim/streetzim",
        command_name="streetzim-langpack", ci_secret_hash=None)
    model = build_offliner_model(off, spec)
    base = {"offliner_id": "streetzim-langpack", "base-url": "https://example.org/osm_mul_x_2026-10.zim"}
    pack = {**base, "mode": "pack", "language": "de", "images": "none",
            # a menu flag left in the form: cleared when the recipe is saved
            "url-template": "https://e.org/{file}"}
    cleared = clear_unset_choices_dependents(spec, pack, "DashModel")
    assert "url-template" not in cleared and cleared["language"] == "de"
    flags = model.model_validate(cleared).model_dump(mode="json")
    args = lc.parse_args([a.replace("'", "") for a in compute_flags(flags)])
    assert (args.mode, args.language, args.images) == ("pack", "de", "none")
    menu = {**base, "mode": "menu", "pack-url": [PK.format("de"), PK.format("fr")],
            "languages": "de,fr", "language": "de"}
    cleared = clear_unset_choices_dependents(spec, menu, "DashModel")
    assert "language" not in cleared
    flags = model.model_validate(cleared).model_dump(mode="json")
    args = lc.parse_args([a.replace("'", "") for a in compute_flags(flags)])
    assert args.pack_url == [PK.format("de"), PK.format("fr")] and args.languages == "de,fr"
    for bad in ({"mode": "both"}, {"mode": "pack", "language": "DE"},
                {"mode": "menu", "pack-url": ["not a url"]}):
        with pytest.raises(ValidationError):
            model.model_validate({**base, **bad})
    with pytest.raises(ValidationError):
        model.model_validate({"offliner_id": "streetzim-langpack", "mode": "pack"})


# ------------------------------------------------------- the Wikipedia ZIM

OPDS = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
  <entry><name>wikipedia_de_all</name><flavour>mini</flavour>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://lb.download.kiwix.org/zim/wikipedia/wikipedia_de_all_mini_2026-10.zim.meta4"/></entry>
  <entry><name>wikipedia_de_top</name><flavour>maxi</flavour>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://lb.download.kiwix.org/zim/wikipedia/wikipedia_de_top_maxi_2026-10.zim.meta4"/></entry>
  <entry><name>wikipedia_de_all</name><flavour>nopic</flavour>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://lb.download.kiwix.org/zim/wikipedia/wikipedia_de_all_nopic_2026-10.zim.meta4"/></entry>
  <entry><name>wikipedia_de_all</name><flavour>maxi</flavour>
    <link rel="http://opds-spec.org/image/thumbnail" href="/catalog/v2/illustration/x/?size=48"/>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://lb.download.kiwix.org/zim/wikipedia/wikipedia_de_all_maxi_2026-10.zim.meta4"/></entry>
</feed>"""
LISTING = ('<a href="wikipedia_de_all_maxi_2026-01.zim">x</a> <a href="wikipedia_de_all_maxi_2026-10.zim">x</a>'
           ' <a href="wikipedia_de_all_nopic_2026-11.zim">x</a> <a href="wikipedia_de_top_maxi_2026-12.zim">x</a>'
           ' <a href="wikipedia_nb_all_maxi_2026-09.zim">x</a>')


def test_opds_picks_the_maxi_zim():
    assert lc.opds_maxi_url(OPDS, "de") == \
        "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_de_all_maxi_2026-10.zim"
    assert lc.opds_maxi_url(OPDS, "fr") is None


def test_listing_picks_the_newest_maxi():
    assert lc.listing_maxi_url(LISTING, "de") == lc.KIWIX_WIKIPEDIA + "wikipedia_de_all_maxi_2026-10.zim"
    assert lc.listing_maxi_url(LISTING, "fr") is None


def test_resolve_uses_the_catalog_then_the_listing():
    asked = []

    def get(url):
        asked.append(url)
        if url.startswith(lc.OPDS_ENTRIES):
            return OPDS
        return LISTING.encode()
    assert lc.resolve_wikipedia_zim("de", get=get).endswith("wikipedia_de_all_maxi_2026-10.zim")
    assert asked == [f"{lc.OPDS_ENTRIES}?name=wikipedia_de_all&count=20"]

    def no_catalog(url):
        if url.startswith(lc.OPDS_ENTRIES):
            raise OSError("down")
        return LISTING.encode()
    # Norwegian: Kiwix's file code is nb
    assert lc.resolve_wikipedia_zim("no", get=no_catalog) == \
        lc.KIWIX_WIKIPEDIA + "wikipedia_nb_all_maxi_2026-09.zim"
    with pytest.raises(lc.StepError, match="give --wikipedia-zim-url"):
        lc.resolve_wikipedia_zim("fr", get=no_catalog)


def test_get_reads_a_bounded_document(monkeypatch):
    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.body[:n]
    monkeypatch.setattr(lc.urllib.request, "urlopen", lambda req, timeout: Resp(b"x" * 10))
    assert lc._get("https://e.org/a", limit=10) == b"x" * 10
    with pytest.raises(OSError, match="over 9 bytes"):
        lc._get("https://e.org/a", limit=9)


# -------------------------------------------------------------- downloads

def test_meta4_sha256_and_its_url():
    doc = ('<metalink><file name="a.zim"><hash type="md5">' + "0" * 32 + '</hash>'
           '<hash type="sha-256">' + "ab" * 32 + '</hash><pieces length="4" type="sha-1">'
           '<hash>' + "1" * 40 + '</hash></pieces></file></metalink>')
    assert lc.meta4_sha256(doc) == "ab" * 32
    assert lc.meta4_sha256("<metalink/>") is None
    # always download.kiwix.org's own .meta4, built from the path, whichever mirror
    assert lc.meta4_url("https://lb.download.kiwix.org/zim/wikipedia/w.zim") == \
        "https://download.kiwix.org/zim/wikipedia/w.zim.meta4"
    assert lc.meta4_url("https://mirror.example.org/kiwix/zim/wikipedia/w%20x.zim") == \
        "https://download.kiwix.org/zim/wikipedia/w%20x.zim.meta4"
    for bad in ("https://download.kiwix.org/zim/w.zim?a=.meta4", "https://download.kiwix.org/zim/w.zim#x",
                "https://mirror.example.org/w.zim"):
        with pytest.raises(lc.StepError, match="no SHA-256"):
            lc.meta4_url(bad)


def test_kiwix_download_is_checked_against_its_meta4(tmp_path):
    f = tmp_path / "w.zim"
    f.write_bytes(lc.ZIM_MAGIC + b"payload")
    good = hashlib.sha256(f.read_bytes()).hexdigest()
    calls = []

    def get(url):
        calls.append(url)
        return f'<hash type="sha-256">{good}</hash>'.encode()
    mirror = "https://mirror.example.org/kiwix/zim/wikipedia/w.zim"
    lc.verify_kiwix(mirror, f, get)
    assert calls == ["https://download.kiwix.org/zim/wikipedia/w.zim.meta4"]
    lc.verify_kiwix(mirror, f, get)                                       # remembered
    assert len(calls) == 1
    f.write_bytes(lc.ZIM_MAGIC + b"tampered")                             # new size and mtime
    with pytest.raises(lc.StepError, match="is not Kiwix's"):
        lc.verify_kiwix(mirror, f, get)
    assert not f.exists()
    g = tmp_path / "g.zim"
    g.write_bytes(lc.ZIM_MAGIC + b"payload")
    with pytest.raises(lc.StepError, match="gives no SHA-256"):
        lc.verify_kiwix(mirror, g, lambda url: b"<metalink/>")


def test_local_inputs_are_used_in_place_and_must_be_zims(tmp_path):
    z = tmp_path / "osm_mul_x.zim"
    z.write_bytes(lc.ZIM_MAGIC + b"\0" * 60)
    assert lc.fetch_input(str(z), tmp_path / "dl") == z.resolve()
    assert lc.fetch_input(z.as_uri(), tmp_path / "dl") == z.resolve()
    assert not (tmp_path / "dl").exists()
    bad = tmp_path / "bad.zim"
    bad.write_bytes(b"<html>")
    with pytest.raises(lc.StepError, match="not a ZIM"):
        lc.fetch_input(str(bad), tmp_path / "dl")
    with pytest.raises(lc.StepError, match="no such file"):
        lc.fetch_input(str(tmp_path / "missing.zim"), tmp_path / "dl")


def test_a_cached_download_is_reused_only_in_an_explicit_dl(tmp_path, monkeypatch):
    from streetzim import download
    seen = []

    def fake(url, dest, **k):
        seen.append(dest.exists())
        dest.write_bytes(lc.ZIM_MAGIC + b"new")
        return dest
    monkeypatch.setattr(download, "fetch_resumable", fake)
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "a.zim").write_bytes(lc.ZIM_MAGIC + b"old")
    lc.fetch_input("https://e.org/a.zim", dl)
    assert seen == [False]                    # a stale file is never trusted
    lc.fetch_input("https://e.org/a.zim", dl, reuse=True)
    assert seen == [False, True]


def test_downloads_are_checked_against_kiwix_sha256(tmp_path, monkeypatch):
    """The Wikipedia ZIM (verify) from any mirror, and any file from a Kiwix
    host, is checked against download.kiwix.org's .meta4; other files are not."""
    from streetzim import download
    body = lc.ZIM_MAGIC + b"payload"
    monkeypatch.setattr(download, "fetch_resumable", lambda url, dest, **k: dest.write_bytes(body) or dest)
    asked = []

    def get(sha):
        def fetch(url):
            asked.append(url)
            return f'<hash type="sha-256">{sha}</hash>'.encode()
        return fetch
    good, bad = hashlib.sha256(body).hexdigest(), "0" * 64
    lc.fetch_input("https://mirror.example/kiwix/zim/wikipedia/w.zim", tmp_path / "a", verify=True, get=get(good))
    assert asked == ["https://download.kiwix.org/zim/wikipedia/w.zim.meta4"]
    with pytest.raises(lc.StepError, match="is not Kiwix's"):
        lc.fetch_input("https://mirror.example/kiwix/zim/wikipedia/w.zim", tmp_path / "b", verify=True,
                       get=get(bad))
    with pytest.raises(lc.StepError, match="is not Kiwix's"):
        lc.fetch_input("https://download.kiwix.org/zim/maps/osm_x.zim", tmp_path / "c", get=get(bad))
    asked.clear()
    lc.fetch_input("https://warehouse.example/maps/osm_x.zim", tmp_path / "d", get=get(bad))
    assert asked == []


def test_url_file_names():
    assert lc.url_file_name("https://e.org/zim/maps/osm_x_wiki-de_2026-10-23_ab12cd34.zim") == \
        "osm_x_wiki-de_2026-10-23_ab12cd34.zim"
    assert lc.url_file_name("https://e.org/a%2Db.zim?x=1") == "a-b.zim"
    for bad in ("https://e.org/", "https://e.org/x.zim.meta4", "https://e.org/..%2F.zim"):
        with pytest.raises(lc.StepError):
            lc.url_file_name(bad)


def test_region_ids_and_timeouts():
    assert lc.region_of("osm_mul_monaco") == "monaco"
    assert lc.region_of("osm_en_new-york-state") == "new-york-state"
    assert lc.region_of("wikipedia_en_all") is None and lc.region_of("") is None
    assert lc.base_name_region("osm-new-york-state-2026-10-16.zim") == "new-york-state"
    assert lc.base_name_region("https://e.org/zim/osm_mul_monaco_2026-10.zim") == "monaco"
    assert lc.base_name_region("monaco.zim") is None
    assert lc.build_timeout(5_000_000) == 3 * 3600
    assert lc.build_timeout(3_000_000_000) == 3 * 3600
    assert lc.build_timeout(3_000_000_001) == 6 * 3600


def test_publish_never_gives_a_name_other_bytes(tmp_path):
    src = tmp_path / "a.zim"
    src.write_bytes(b"x" * 10)
    out = tmp_path / "out"
    out.mkdir()
    assert lc.publish(src, out / "b.zim") is True
    assert (out / "b.zim").read_bytes() == b"x" * 10 and [p.name for p in out.iterdir()] == ["b.zim"]
    assert lc.publish(src, out / "b.zim") is False                   # same bytes: kept
    src.write_bytes(b"y" * 10)
    with pytest.raises(lc.StepError, match="other bytes"):
        lc.publish(src, out / "b.zim")
    assert (out / "b.zim").read_bytes() == b"x" * 10
    assert lc.publish(src, out / "b.zim.gate.json", replace=True) is True   # a sidecar may change
    assert lc.publish(src, out / "b.zim.gate.json", replace=True) is False


def test_publish_writes_through_a_hidden_name(tmp_path, monkeypatch):
    """Zimfarm's uploader takes a ZIM by its name: the copy is made under a
    hidden name that does not end in .zim and renamed when complete."""
    src = tmp_path / "a.zim"
    src.write_bytes(b"x" * 10)
    out = tmp_path / "out"
    out.mkdir()
    copied, real = [], shutil.copyfile

    def spy(s, d, *a, **k):
        copied.append(Path(d).name)
        assert not (out / "b.zim").exists()
        return real(s, d, *a, **k)
    monkeypatch.setattr(lc.shutil, "copyfile", spy)
    lc.publish(src, out / "b.zim")
    assert copied == [f".b.zim.{os.getpid()}.part"]
    assert sorted(p.name for p in out.iterdir()) == ["b.zim"]


def test_content_names(tmp_path):
    f = tmp_path / "x.zim"
    f.write_bytes(b"abc")
    assert lc.content_name(f, "osm_t_search-de_2026_ab") == \
        f"osm_t_search-de_2026_ab-{hashlib.sha256(b'abc').hexdigest()[:12]}.zim"


# ------------------------------------------------------- process trees

def _late(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in zimcheck that writes `marker` after 3 s, unless killed."""
    marker = tmp_path / "late"
    zc = tmp_path / "zimcheck.py"
    zc.write_text(f"import sys, time\nprint('zimcheck started', flush=True)\ntime.sleep(3)\n"
                  f"open({str(marker)!r}, 'w').close()\ntime.sleep(60)\n")
    return zc, marker


def test_run_kills_the_whole_group_at_its_timeout(tmp_path):
    # the child starts a grandchild that would outlive it
    zc, marker = _late(tmp_path)
    child = tmp_path / "child.py"
    child.write_text("import subprocess, sys, time\n"
                     f"subprocess.Popen([sys.executable, {str(zc)!r}])\n"
                     "print('started', flush=True)\ntime.sleep(60)\n")
    t0 = time.time()
    rc = lc.run([sys.executable, str(child)], tmp_path / "log", timeout=1, echo=False)
    assert rc == 124 and time.time() - t0 < 30
    assert "started" in (tmp_path / "log").read_text()
    time.sleep(4)
    assert not marker.exists()
    assert lc.run([sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path / "l2", timeout=30) == 3


def test_run_kills_a_zimcheck_under_timeout(tmp_path):
    """GNU timeout puts itself and the zimcheck it runs in a process group of
    their own (as pack_gate.py runs it): run() finds them through /proc."""
    zc, marker = _late(tmp_path)
    child = tmp_path / "gate.py"
    child.write_text("import subprocess, sys\n"
                     f"sys.exit(subprocess.call(['timeout', '60', 'nice', '-n', '1', sys.executable, {str(zc)!r}]))\n")
    rc = lc.run([sys.executable, str(child)], tmp_path / "log", timeout=2, echo=False)
    assert rc == 124 and "zimcheck started" in (tmp_path / "log").read_text()
    time.sleep(4)
    assert not marker.exists()


def test_sigterm_kills_the_tree(tmp_path):
    zc, marker = _late(tmp_path)
    child = tmp_path / "gate.py"
    child.write_text("import subprocess, sys\n"
                     f"sys.exit(subprocess.call(['timeout', '60', sys.executable, {str(zc)!r}]))\n")
    log = tmp_path / "log"
    driver = tmp_path / "driver.py"
    driver.write_text("import signal, sys\nfrom pathlib import Path\n"
                      f"sys.path.insert(0, {str(ROOT)!r})\n"
                      "from streetzim import langpack_cli as lc\n"
                      "def stop(s, f):\n    raise SystemExit(128 + s)\n"
                      "signal.signal(signal.SIGTERM, stop)\n"
                      f"lc.run([sys.executable, {str(child)!r}], Path({str(log)!r}), timeout=60, echo=False)\n")
    proc = subprocess.Popen([sys.executable, str(driver)])
    t0 = time.time()
    while "zimcheck started" not in (log.read_text() if log.exists() else ""):
        assert time.time() - t0 < 30
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 128 + signal.SIGTERM
    time.sleep(4)
    assert not marker.exists()


def test_run_leaves_nothing_behind_when_the_child_exits(tmp_path):
    marker = tmp_path / "late"
    late = tmp_path / "late.py"
    late.write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').close()\n")
    child = tmp_path / "child.py"
    child.write_text("import subprocess, sys\n"
                     f"subprocess.Popen([sys.executable, {str(late)!r}], stdout=subprocess.DEVNULL, "
                     "stderr=subprocess.DEVNULL)\n")
    assert lc.run([sys.executable, str(child)], tmp_path / "log", timeout=30, echo=False) == 0
    time.sleep(4)
    assert not marker.exists()


# ------------------------------------------------------------ verdicts

LINK = "  - 'Overall%20Test%20Status%3A%20Pass' (resolves to 'Overall Test Status: Pass')"


def test_verify_and_zimcheck_verdicts():
    assert lc.verify_summary("PASS  a\nPASS  b\nSUMMARY 2/2 PASS\n") == (True, "SUMMARY 2/2 PASS")
    assert lc.verify_summary("PASS  a\nFAIL  b\nSUMMARY 1/2 PASS\n") == (False, "SUMMARY 1/2 PASS")
    assert lc.verify_summary("SUMMARY 0/0 PASS") == (False, "SUMMARY 0/0 PASS")
    assert lc.verify_summary("Traceback ...") == (False, None)
    passed = "[INFO] Overall Test Status: Pass\n"
    assert lc.zimcheck_passed(passed, 0, "zim-tools 3.8.0")
    assert not lc.zimcheck_passed(passed, 1, "zim-tools 3.8.0")              # rc must be 0
    assert not lc.zimcheck_passed("[INFO] Overall Test Status: Fail\n", 0, "zim-tools 3.8.0")
    assert not lc.zimcheck_passed(passed, 0, "zimru 0.1")
    # a link that reads like the verdict, then the real one
    linked = f"[ERROR] Internal URL: Dangling link(s) in article 'x':\n{LINK}\n[INFO] Overall Test Status: Fail\n"
    assert not lc.zimcheck_passed(linked, 0, "zim-tools 3.8.0")
    assert not lc.zimcheck_passed(f"{LINK}\n", 0, "zim-tools 3.8.0")


def test_prefetch_results(tmp_path):
    def logged(*rows):
        p = tmp_path / "p.log"
        p.write_text("noise\n" + "\n".join(json.dumps(r) for r in rows) + "\n")
        return p
    done = {"base": "b", "base_uuid": "u", "langs": ["de"], "ok": True, "failed": [], "text_incomplete": []}
    assert lc.prefetch_ok(0, logged({"lang": "de", "titles": 3}, done)) == (True, "complete")
    text = dict(done, ok=False, text_incomplete=["de"])
    assert lc.prefetch_ok(1, logged({"lang": "de", "titles": 3}, text)) == \
        (True, "Wikidata text incomplete for de")
    fail = dict(done, ok=False, failed=["de"])
    ok, why = lc.prefetch_ok(1, logged({"lang": "de", "titles_error": "OSError: no dumps"}, fail))
    assert not ok and why == "de: OSError: no dumps"
    p = tmp_path / "crash.log"
    p.write_text("Traceback (most recent call last):\nKeyError: 'x'\n")
    ok, why = lc.prefetch_ok(1, p)
    assert not ok and "KeyError" in why
    assert not lc.prefetch_ok(2, logged(done))[0]


# ----------------------------------------------- --mode pack, stand-in tools

STUB_PREFETCH = """import json, os, sys
mode = os.environ.get("FAKE_PREFETCH", "ok")
lang = sys.argv[sys.argv.index("--langs") + 1]
cache = sys.argv[sys.argv.index("--cache") + 1]
os.makedirs(cache, exist_ok=True)
open(os.path.join(cache, "prefetched"), "w").close()
if mode == "crash":
    raise SystemExit("Traceback (most recent call last): boom")
row = {"lang": lang, "titles": 3}
if mode == "fail":
    row = {"lang": lang, "titles_error": "OSError: dumps.wikimedia.org unreachable"}
print(json.dumps(row))
print(json.dumps({"base": "b", "base_uuid": "u", "qids": 3, "langs": [lang], "ok": mode == "ok",
                  "failed": [lang] if mode == "fail" else [],
                  "text_incomplete": [lang] if mode == "text" else []}))
sys.exit(0 if mode == "ok" else 1)
"""
STUB_BUILD = """import json, os, shutil, sys
a = sys.argv
open(os.environ["FAKE_BUILD_RAN"], "a").write(a[a.index("--lang") + 1] + "\\n")
out, stats = a[a.index("-o") + 1], a[a.index("--stats") + 1]
shutil.copyfile(os.environ["FAKE_PACK"], out)
size = os.path.getsize(out) + (1 if os.environ.get("FAKE_BUILD") == "badstats" else 0)
json.dump({"zim_bytes": size, "articles": 3, "check": {"ok": True, "problems": []}}, open(stats, "w"))
sys.exit(int(os.environ.get("FAKE_BUILD_RC", "0")))
"""
STUB_GATE = """import hashlib, json, os, sys
pack = sys.argv[1]
ok = os.environ.get("FAKE_GATE", "pass") == "pass"
md5 = hashlib.md5(open(pack, "rb").read()).hexdigest()
json.dump({"pack": os.path.basename(pack), "ok": ok, "pack_md5": md5}, open(pack + ".gate.json", "w"))
print(json.dumps({"pack": os.path.basename(pack), "ok": ok}))
sys.exit(0 if ok else 1)
"""


def _version_zimcheck(d: Path) -> str:
    zc = d / "zimcheck"
    zc.write_text(f"#!{sys.executable}\nprint('zim-tools 3.8.0')\n")
    zc.chmod(0o755)
    return str(zc)


@pytest.fixture
def packrun(tmp_path, monkeypatch):
    pytest.importorskip("libzim")
    from tests.langpack import lpzims
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("prefetch.py", STUB_PREFETCH), ("build_wiki_pack.py", STUB_BUILD),
                       ("pack_gate.py", STUB_GATE)):
        (stubs / name).write_text(body)
        monkeypatch.setitem(lc.TOOL_OVERRIDES, name, stubs / name)
    base = tmp_path / "in" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    buuid = lpzims.write_base(base)
    packs = [tmp_path / "in" / f"de{i}.zim" for i in (1, 2)]
    for p in packs:
        lpzims.write_pack(p, "de", buuid)
    wiki = tmp_path / "in" / "wikipedia_de_all_maxi_2026-10.zim"
    wiki.write_bytes(lc.ZIM_MAGIC + b"\0" * 100)
    monkeypatch.setenv("FAKE_PACK", str(packs[0]))
    monkeypatch.setenv("FAKE_BUILD_RAN", str(tmp_path / "built"))
    monkeypatch.setenv("STREETZIM_CACHE_DIR", str(tmp_path / "cache"))
    out = tmp_path / "out"
    argv = ["--mode=pack", f"--base-url={base}", "--language=de", f"--wikipedia-zim-url={wiki}",
            f"--output={out}", f"--stats-filename={tmp_path / 'progress.json'}",
            f"--zimcheck={_version_zimcheck(tmp_path)}"]
    return {"argv": argv, "out": out, "base": base, "buuid": buuid, "packs": packs, "dir": tmp_path}


def _zims(out: Path) -> list[str]:
    return sorted(p.name for p in out.glob("*.zim")) if out.exists() else []


def test_pack_publishes_a_gated_pack_under_its_content_name(packrun, capsys):
    out, (de1, de2) = packrun["out"], packrun["packs"]
    assert lc.main(packrun["argv"]) == 0
    stem = f"osm_testland_wiki-de_{datetime.date.today().isoformat()}_{packrun['buuid'][:8]}"
    first = f"{stem}-{_sha(de1)[:12]}.zim"
    assert _zims(out) == [first] and (out / first).read_bytes() == de1.read_bytes()
    assert json.loads((out / (first + ".gate.json")).read_text())["pack"] == first
    assert (out / (first + ".stats.json")).exists()
    progress = json.loads((packrun["dir"] / "progress.json").read_text())
    assert progress["done"] == progress["total"] == 6
    # the caches go where $STREETZIM_CACHE_DIR says (the image: /output/cache)
    assert (packrun["dir"] / "cache" / "wiki_pack_cache" / "prefetched").exists()
    # the same bytes again: the same name, kept
    assert lc.main(packrun["argv"]) == 0
    assert "already there, same bytes" in capsys.readouterr().out and _zims(out) == [first]
    # other bytes: another name; the first file is untouched
    os.environ["FAKE_PACK"] = str(de2)
    assert lc.main(packrun["argv"]) == 0
    assert _zims(out) == sorted([first, f"{stem}-{_sha(de2)[:12]}.zim"])
    assert (out / first).read_bytes() == de1.read_bytes()
    assert not list(out.glob(".*"))


def test_pack_temporary_files_never_reach_output(packrun, monkeypatch):
    tmp = packrun["dir"] / "tmp"
    assert lc.main([*packrun["argv"], f"--tmp={tmp}"]) == 0
    assert list(tmp.iterdir()) == []                    # the work folder is removed
    for p in packrun["out"].rglob("*"):
        assert not p.name.startswith(".") and not p.is_dir()
    # without $STREETZIM_CACHE_DIR: beside the downloads, in a private folder,
    # removed with it; the variable is not left set
    monkeypatch.delenv("STREETZIM_CACHE_DIR")
    import tempfile
    private = packrun["dir"] / "private"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    assert lc.main(packrun["argv"]) == 0
    assert "STREETZIM_CACHE_DIR" not in os.environ and list(private.iterdir()) == []


@pytest.mark.parametrize("env, why", [
    ({"FAKE_GATE": "fail"}, "pack_gate.py FAIL"),
    ({"FAKE_BUILD_RC": "2"}, "refused"),
    ({"FAKE_BUILD": "badstats"}, "refused"),
    ({"FAKE_BUILD_RC": "1"}, "refused"),             # rc 1 and no zimcheck result builder_ok excuses
])
def test_pack_publishes_nothing_that_fails(packrun, monkeypatch, capsys, env, why):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert lc.main(packrun["argv"]) == 1
    assert why in capsys.readouterr().err
    assert _zims(packrun["out"]) == []


def test_pack_prefetch_failures_stop_it_with_their_cause(packrun, monkeypatch, capsys):
    built = packrun["dir"] / "built"
    monkeypatch.setenv("FAKE_PREFETCH", "fail")
    assert lc.main(packrun["argv"]) == 1
    assert "dumps.wikimedia.org unreachable" in capsys.readouterr().err and not built.exists()
    monkeypatch.setenv("FAKE_PREFETCH", "crash")
    assert lc.main(packrun["argv"]) == 1
    assert "boom" in capsys.readouterr().err and not built.exists()
    # incomplete Wikidata text: built, and said
    monkeypatch.setenv("FAKE_PREFETCH", "text")
    assert lc.main(packrun["argv"]) == 0
    assert "Wikidata text incomplete for de" in capsys.readouterr().out
    assert built.read_text() == "de\n" and len(_zims(packrun["out"])) == 1


def test_pack_region_must_agree_with_the_names(packrun, capsys):
    with pytest.raises(SystemExit) as e:
        lc.main([*packrun["argv"], "--region=otherland"])
    assert e.value.code == 2 and "region ids disagree" in capsys.readouterr().err
    # a base whose file name says another region than its Name
    other = packrun["base"].with_name("osm-otherland-2026-10-13.zim")
    shutil.copyfile(packrun["base"], other)
    argv = [a if not a.startswith("--base-url") else f"--base-url={other}" for a in packrun["argv"]]
    assert lc.main(argv) == 1
    assert "is region testland, not otherland" in capsys.readouterr().err
    assert _zims(packrun["out"]) == []


def test_an_unofficial_zimcheck_stops_it_before_any_download(tmp_path, capsys):
    from tests.langpack.lpzims import fake_zimcheck
    zc = fake_zimcheck(tmp_path, "zimru", "", rc=0, version="zimru-zimcheck 0.1")
    rc = lc.main(["--mode=pack", "--base-url=https://example.invalid/osm_mul_x.zim", "--language=de",
                  f"--zimcheck={zc}", f"--output={tmp_path / 'out'}", f"--tmp={tmp_path / 'tmp'}"])
    assert rc == 1 and "not the official zimcheck" in capsys.readouterr().err
    assert not (tmp_path / "tmp").exists()


def test_site_check_url_names_without_an_item():
    from tools.langpack.site_check import url_item_file
    assert url_item_file("osm_x_search-de_2026_ab.zim") == ("", "osm_x_search-de_2026_ab.zim")
    assert url_item_file("https://archive.org/download/item/f.zim") == ("item", "f.zim")


def test_one_search_pack_per_version():
    avail = {"de": "d", "fr": "f", "it": "i"}
    # de and en carry the same packs, each gets its own; es lacks its pack: none
    sets = lc.search_pack_sets(["de", "en", "fr", "es"], ["de"], None, {**avail, "en": "e"})
    assert sets == [["de", "en"], ["de", "en"], ["de", "en", "fr"]]
    # an English base: the en version carries the region's packs alone
    assert lc.search_pack_sets(["de", "en"], ["de", "it"], "en", avail) == [["de", "it"], ["de", "it"]]
    assert lc.search_pack_sets(["en"], [], "en", avail) == []      # no pack: the base's own indexes


def test_search_disk_need():
    # two search packs (estimated), and the bigger version twice
    need = lc.search_disk_need(1000, [[100], [100, 300]])
    est = [150 + 50, 150 + 200]
    assert need == sum(est) + 2 * (1000 + 400 + 350)
    assert lc.search_disk_need(1000, []) == 2000


# ------------------------------------------------- --mode menu, end to end

def _fake_zimcheck(d: Path, merged: str = "Pass") -> str:
    """zim-tools' answers: a pack's clean report (its back-to-map links),
    and `merged` for a merged file (checked with --threads=1)."""
    lpzims_dir = ROOT / "tests" / "langpack"
    script = d / f"zimcheck-{merged}"
    script.write_text("\n".join([
        f"#!{sys.executable}", "import sys",
        f"sys.path.insert(0, {str(lpzims_dir)!r})",
        "if '--version' in sys.argv: print('zim-tools 3.8.0'); sys.exit(0)",
        f"if '--threads=1' in sys.argv: print('[INFO] Overall Test Status: {merged}'); "
        f"sys.exit({0 if merged == 'Pass' else 1})",
        "from lpzims import zimcheck_report",
        "sys.stdout.write(zimcheck_report(sys.argv[-1])); sys.exit(1)"]) + "\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture(scope="module")
def menu_run(tmp_path_factory):
    pytest.importorskip("libzim")
    from tests.langpack import lpzims
    d = tmp_path_factory.mktemp("lpmenu")
    base = d / "in" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    buuid = lpzims.write_base(base, cfg_extra={"regionLanguages": ["de", "../../x"]})
    packs = []
    for lang in ("de", "fr", "th"):
        p = d / "in" / f"osm_testland_wiki-{lang}_2026-10-13_{buuid[:8]}.zim"
        lpzims.write_pack(p, lang, buuid)
        packs.append(p)
    # another base's pack: refused by its gate
    other = d / "in" / "other"
    other.mkdir()
    stray = other / "osm_testland_wiki-th_2026-10-01_00000000.zim"
    lpzims.write_pack(stray, "th", "0" * 32)
    zc = _fake_zimcheck(d)
    out = d / "out"
    argv = ["--mode=menu", f"--base-url={base}", *[f"--pack-url={p}" for p in packs],
            "--languages=de,fr,th,es", "--url-template=https://example.org/maps/{file}",
            f"--output={out}", f"--stats-filename={out / 'task_progress.json'}",
            f"--tmp={d / 'tmp'}", f"--zimcheck={zc}", "--jobs=1"]
    rc = lc.main(argv)
    return {"rc": rc, "out": out, "base": base, "packs": packs, "dir": d, "argv": argv,
            "stray": stray, "zc": zc}


def _menu_argv(menu_run: dict, *extra: str, drop: tuple[str, ...] = ()) -> list[str]:
    keep = [a for a in menu_run["argv"] if not a.startswith(("--output", "--stats", *drop))]
    return [*keep, *extra]


def test_menu_publishes_checked_versions(menu_run):
    out = menu_run["out"]
    assert menu_run["rc"] == 0
    menu = json.loads((out / "recipes" / "menu.json").read_text())
    # es has no pack; en is the base's own Wikipedia (lpzims writes English articles)
    assert sorted(menu["published"]) == ["de", "en", "fr", "th"]
    # "../../x" in the base's regionLanguages is no language code
    assert menu["region_languages"] == ["de"] and menu["region"] == "testland"
    assert {d["lang"] for d in menu["dropped"]} == {"es", "../../x"}
    for L in menu["published"]:
        assert menu["checks"][L] == {"recipe_eq_merge": True, "verify": True, "zimcheck": True,
                                     "search": True}
        v = json.loads((out / "recipes" / "verdicts" / "osm-testland-2026-10-13" / f"{L}.json").read_text())
        assert v["all_pass"] and "viewer" in v["not_run"]
    idx = json.loads((out / "recipes" / "index.json").read_text())
    entry = {m["lang"]: m for m in idx["regions"][0]["menu"]}
    r = json.loads((out / entry["fr"]["recipe"]).read_text())
    assert r["format"] == "streetzim-recipe/2"
    assert all(p["url"] == f"https://example.org/maps/{p['file']}" for p in r["packs"].values())
    # a local base: its URL is the template's, as the parts'
    assert idx["regions"][0]["base"]["download"] == "https://example.org/maps/osm-testland-2026-10-13.zim"
    # the search packs the recipes use are published, under their content names, nothing else is
    zims = sorted(p.name for p in out.glob("*.zim"))
    assert zims == sorted(menu["search_packs"]) and all("_search-" in z for z in zims)
    # one per version, indexed in its language: de and en carry the same pack (de) and
    # still get one each; fr's and th's own language names theirs
    search = {L: next(p["file"] for p in json.loads((out / "recipes" / f"{L}.json").read_text())["packs"].values()
                      if p.get("kind") == "search") for L in menu["published"]}
    assert {L: re.sub(r"_\d{4}-\d{2}-\d{2}_.*", "", f) for L, f in search.items()} == {
        "de": "osm_testland_search-de.de", "en": "osm_testland_search-de.en",
        "fr": "osm_testland_search-de-fr", "th": "osm_testland_search-de-th"}
    assert len(set(search.values())) == len(zims) == 4
    assert all(z.endswith(f"-{_sha(out / z)[:12]}.zim") for z in zims)
    assert {p["file"] for p in r["packs"].values() if p.get("kind") == "search"} <= set(zims)
    assert not list(out.glob(".*"))                         # no staging left
    progress = json.loads((out / "task_progress.json").read_text())
    assert progress["done"] == progress["total"]


def test_menu_recipe_builds_the_checked_file(menu_run, tmp_path):
    """recipe2.py materialize from the published recipe: the file's MD5 is the recipe's."""
    from tools.langpack.recipe2 import materialize
    out = menu_run["out"]
    r = json.loads((out / "recipes" / "de.json").read_text())
    files = {p.name: p for p in menu_run["packs"]} | {p.name: p for p in out.glob("*.zim")}
    got = tmp_path / "de.zim"
    materialize(r, str(menu_run["base"]), {k: str(files[p["file"]]) for k, p in r["packs"].items()}, str(got))
    assert hashlib.md5(got.read_bytes(), usedforsecurity=False).hexdigest() == r["file_md5"]


def test_menu_refuses_a_pack_of_another_base(menu_run, tmp_path, capsys):
    rc = lc.main(_menu_argv(menu_run, f"--pack-url={menu_run['stray']}", f"--output={tmp_path / 'o'}",
                            drop=("--pack-url",)))
    out = capsys.readouterr()
    assert rc == 1 and "no pack passed its gate" in out.err      # its only pack is another base's
    assert "osm_testland_wiki-th_2026-10-01_00000000.zim: FAIL" in out.out
    assert not list((tmp_path / "o").glob("*.zim"))


def test_menu_refuses_a_pack_whose_name_says_another_language(menu_run, tmp_path, capsys):
    liar = tmp_path / "osm_testland_wiki-fr_2026-10-13_x.zim"
    shutil.copyfile(next(p for p in menu_run["packs"] if "wiki-de" in p.name), liar)
    rc = lc.main(_menu_argv(menu_run, f"--pack-url={liar}", f"--output={tmp_path / 'o'}",
                            drop=("--pack-url",)))
    assert rc == 1 and "its name says fr, its articles are de" in capsys.readouterr().err


def test_menu_reruns_keep_earlier_recipes_building(menu_run, tmp_path, capsys):
    """Without --overwrite an earlier recipes/ stops it; with it, new search
    packs come under new names and the earlier recipes still build."""
    from tools.langpack.recipe2 import materialize
    o = tmp_path / "o"
    shutil.copytree(menu_run["out"], o)
    before = {p.name: p.read_bytes() for p in o.glob("*.zim")}
    old = json.loads((o / "recipes" / "fr.json").read_text())
    assert lc.main(_menu_argv(menu_run, f"--output={o}")) == 1
    assert "recipes exists (use --overwrite)" in capsys.readouterr().err
    assert lc.main(_menu_argv(menu_run, f"--output={o}", "--overwrite")) == 0
    assert all((o / n).read_bytes() == b for n, b in before.items())
    assert set(before) < {p.name for p in o.glob("*.zim")}
    files = {p.name: p for p in menu_run["packs"]} | {p.name: p for p in o.glob("*.zim")}
    got = tmp_path / "fr.zim"
    materialize(old, str(menu_run["base"]), {k: str(files[p["file"]]) for k, p in old["packs"].items()}, str(got))
    assert hashlib.md5(got.read_bytes(), usedforsecurity=False).hexdigest() == old["file_md5"]


def test_menu_never_gives_a_published_name_other_bytes(menu_run, tmp_path, monkeypatch, capsys):
    # every search pack this run makes is called <stem>_fixed.zim; the de
    # version's (search-de.de: the de pack, indexed in German) is already there
    monkeypatch.setattr(lc, "content_name", lambda path, stem: stem.split("_20")[0] + "_fixed.zim")
    o = tmp_path / "o"
    o.mkdir()
    taken = "osm_testland_search-de.de_fixed.zim"
    (o / taken).write_bytes(b"older")
    assert lc.main(_menu_argv(menu_run, f"--output={o}", "--overwrite")) == 1
    assert f"{o / taken} exist(s) with other bytes: not replaced" in capsys.readouterr().err
    assert sorted(p.name for p in o.iterdir()) == [taken]
    assert (o / taken).read_bytes() == b"older"


def test_menu_publishes_nothing_whose_merged_file_fails_zimcheck(menu_run, tmp_path, capsys):
    zc = _fake_zimcheck(tmp_path, merged="Fail")
    rc = lc.main(_menu_argv(menu_run, f"--output={tmp_path / 'o'}", f"--zimcheck={zc}", drop=("--zimcheck",)))
    out = capsys.readouterr()
    assert rc == 1 and "no version passed its checks" in out.err
    assert "de: FAIL recipe_eq_merge=ok verify=ok zimcheck=FAIL search=ok" in out.out
    assert not (tmp_path / "o" / "recipes").exists() and not list((tmp_path / "o").glob("*.zim"))


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_menu_checks_the_recipe_against_the_merge(menu_run, tmp_path, monkeypatch, capsys):
    """A merge that differs from what the recipe builds fails recipe == merge."""
    real = lc.LANGPACK / "merge_zim.py"
    monkeypatch.setitem(lc.TOOL_OVERRIDES, "merge_zim.py", _stub(tmp_path, "merge_zim.py", (
        "import subprocess, sys\n"
        f"rc = subprocess.call([sys.executable, {str(real)!r}, *sys.argv[1:]])\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "with open(out, 'r+b') as f:\n"
        "    f.seek(-17, 2); b = f.read(1); f.seek(-17, 2); f.write(bytes([b[0] ^ 1]))\n"
        "sys.exit(rc)\n")))
    rc = lc.main(_menu_argv(menu_run, f"--output={tmp_path / 'o'}", "--languages=de", drop=("--languages",)))
    out = capsys.readouterr()
    assert rc == 1 and "de: FAIL recipe_eq_merge=FAIL" in out.out
    assert not list((tmp_path / "o").glob("*.zim"))


def test_menu_needs_verify_merge_to_pass(menu_run, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(lc.TOOL_OVERRIDES, "verify_merge.py", _stub(
        tmp_path, "verify_merge.py", "print('FAIL  metadata  x')\nprint('SUMMARY 1/2 PASS')\nraise SystemExit(1)\n"))
    rc = lc.main(_menu_argv(menu_run, f"--output={tmp_path / 'o'}", "--languages=de", drop=("--languages",)))
    out = capsys.readouterr()
    assert rc == 1 and "de: FAIL recipe_eq_merge=ok verify=FAIL" in out.out
    assert not list((tmp_path / "o").glob("*.zim"))


def test_menu_publishes_nothing_site_check_fails(menu_run, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(lc.TOOL_OVERRIDES, "site_check.py", _stub(
        tmp_path, "site_check.py", "print('FAIL de: verdict is for another recipe')\nraise SystemExit(1)\n"))
    rc = lc.main(_menu_argv(menu_run, f"--output={tmp_path / 'o'}", "--languages=de", drop=("--languages",)))
    assert rc == 1 and "site_check.py FAIL" in capsys.readouterr().err
    assert not (tmp_path / "o" / "recipes").exists() and not list((tmp_path / "o").glob("*.zim"))


def test_menu_checks_the_disk_first(menu_run, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lc, "free_bytes", lambda folder: 10)
    asked, real = [], lc.search_disk_need
    monkeypatch.setattr(lc, "search_disk_need", lambda base, sets: asked.append(sets) or real(base, sets))
    rc = lc.main(_menu_argv(menu_run, f"--output={tmp_path / 'o'}"))
    err = capsys.readouterr().err
    assert rc == 1 and "the search packs and the largest version's checks needs about" in err
    assert not list((tmp_path / "o").glob("*.zim"))
    # a search pack per version: de, en (both the de pack alone), fr, th
    sizes = {p.name.split("_")[2]: p.stat().st_size for p in menu_run["packs"]}
    assert sorted(asked[0]) == sorted([[sizes["wiki-de"]], [sizes["wiki-de"]],
                                       [sizes["wiki-de"], sizes["wiki-fr"]], [sizes["wiki-de"], sizes["wiki-th"]]])


def test_menu_refuses_a_version_kiwix_search_cannot_find_its_pack_in(menu_run, tmp_path, capsys):
    """A th pack whose articles all have titles the base has too: the th
    version's pack-only titles are none, so its search check fails."""
    from tests.langpack import lpzims
    d = tmp_path / "in"
    d.mkdir()
    buuid = lpzims.read_uuid(menu_run["base"])
    th = d / f"osm_testland_wiki-th_2026-10-13_{buuid[:8]}.zim"
    lpzims.write_pack(th, "th", buuid, articles=list(lpzims.ARTICLES["en"]))
    packs = [p for p in menu_run["packs"] if "wiki-th" not in p.name] + [th]
    rc = lc.main(_menu_argv(menu_run, *[f"--pack-url={p}" for p in packs], f"--output={tmp_path / 'o'}",
                            drop=("--pack-url",)))
    out = capsys.readouterr().out
    assert rc == 0 and "th: FAIL recipe_eq_merge=ok verify=ok zimcheck=ok search=FAIL" in out
    menu = json.loads((tmp_path / "o" / "recipes" / "menu.json").read_text())
    assert "th" not in menu["published"] and "de" in menu["published"]
    assert any(r.get("lang") == "th" for r in menu["refused"])


def test_menu_titles_ask_wikidata_in_language_codes_only(tmp_path, monkeypatch):
    """The base's regionLanguages are data: a value that is no language code
    never reaches the Wikidata cache's file names; the titles still come."""
    pytest.importorskip("libzim")
    from tests.langpack import lpzims
    base = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(base, cfg_extra={"regionLanguages": ["de", "../../x", "/x/y"]},
                      extra={"category-index/admin.json": [{"n": "Testland", "s": "country", "q": "Q41"}]})
    asked: list[list[str]] = []

    def fetch(ids, codes):
        asked.append(codes)
        return {q: {"labels": {c: {"value": f"Testland-{c}"} for c in codes}} for q in ids}
    monkeypatch.setattr(lc, "title_fetcher", lambda: fetch)
    plan = lc.plan_menu(base, {"de": "-", "fr": "-"}, ["de", "fr"], "testland", tmp_path / "cache")
    assert plan["titles"]["de"] == "Testland-de" and plan["titles"]["fr"] == "Testland-fr"
    assert asked and not any("/" in c or "." in c for c in asked[0])
    assert sorted(p.name for p in (tmp_path / "cache" / "wikidata-text").iterdir()) == \
        sorted(f"{c}.jsonl" for c in ("de", "en", "fr"))


def test_same_bytes(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"x" * 100)
    c.write_bytes(b"x" * 99 + b"y")
    assert lc.same_bytes(a, b) and not lc.same_bytes(a, c)
    c.write_bytes(b"x" * 101)
    assert not lc.same_bytes(a, c)
