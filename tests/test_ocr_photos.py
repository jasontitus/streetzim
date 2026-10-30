"""tools/ocr_photos.py (the sign reader scored on the labelled photos) and
cloud/serve_zim_entries.py's --overlay, which serves a map with today's
viewer and the reader's models on top."""
from __future__ import annotations

import http.client
import json
import socketserver
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import ocr_photos as op  # noqa: E402


def test_norm_folds_what_two_readings_of_a_sign_share() -> None:
    assert op.norm("Road ٢٩١٢") == op.norm("road 2912") == "road2912"
    assert op.norm("خیابان ۱۲") == op.norm("خیابان12")
    assert op.norm("Bab Al-Bahrain!") == "babalbahrain"
    assert op.norm("  ") == ""
    assert op.norm("Zürich HB") == op.norm("Zurich HB") and op.norm("مَال") == op.norm("مال")
    assert op.norm("नगर") != op.norm("नागर")         # an Indic vowel sign is a letter, kept


def test_coverage_is_the_share_of_the_labelled_line_read_in_order() -> None:
    assert op.coverage("Manama West", "Isa Town 4 Manama West") == 1.0
    assert op.coverage("Bani Jamrah", "Bani Jamrah") == 1.0
    assert 0.5 < op.coverage("Bani Jamrah", "Bani Jarah") < 1.0
    assert op.coverage("Bani Jamrah", "Riffa") < 0.5
    assert op.coverage("", "anything") == 0.0
    assert op.coverage("GST", "OFFICE NAGPUR SODS SERVICESTRANSPOR") < op.READ_MIN   # scattered letters
    assert op.coverage("ONLY", "Ocean National Library") < op.READ_MIN


def test_same_place_needs_the_names_to_be_close() -> None:
    assert op.same_place("Manama", "manama")
    assert op.same_place("Starbucks Seef Mall", "Starbucks")
    assert not op.same_place("Madinat Hamad", "Hamad")
    assert not op.same_place("Budaiya", "Riffa")
    assert not op.same_place("", "")
    assert op.same_place("7", "7") and not op.same_place("7", "17")


def _rec(**kw):
    base = {"id": "bh-0001", "region": "bahrain", "country": "BH", "kind": "bilingual", "difficulty": "easy",
            "lines": [{"text": "بني جمرة", "lang": "ar", "latin": "Bani Jamrah"}, {"text": "Bani Jamrah", "lang": "en"}],
            "expect": {"query": "Bani Jamrah", "record": "", "in_map": None}}
    base.update(kw)
    return base


def test_a_photo_read_whole_and_found_first() -> None:
    raw = {"status": "searched", "ms": 3600, "lines": ["بني جمرة", "Bani Jamrah", "Budaiya Health Centre"],
           "field": "Bani Jamrah", "results": [{"name": "Bani Jamrah", "shown": "Bani Jamrah بني جمرة"},
                                               {"name": "Budaiya", "shown": "Budaiya"}]}
    s = op.score(_rec(), raw)
    assert (s["read"], s["query"], s["found1"], s["found3"]) == (1.0, True, True, True)


def test_a_photo_half_read_searched_for_another_line_and_found_third() -> None:
    raw = {"status": "searched", "lines": ["بني جـره", "Bani Jamrah", "Riffa"], "field": "Riffa",
           "results": [{"name": "Riffa", "shown": "Riffa"}, {"name": "Isa Town", "shown": ""},
                       {"name": "Bani Jamrah", "shown": ""}, {"name": "Bani Jamrah", "shown": ""}]}
    s = op.score(_rec(), raw)
    assert s["read"] == 0.5 and s["read_lines"][0] < op.READ_MIN
    assert (s["query"], s["found1"], s["found3"]) == (False, False, True)


def test_a_result_is_found_by_its_own_script_name_and_the_signs_lines() -> None:
    rec = _rec(id="tw-0001", region="taipei", kind="street sign",
               lines=[{"text": "朱崙街", "lang": "zh"}, {"text": "Lane 53巷", "lang": "zh"}],
               expect={"query": "朱崙街53巷", "record": ""})
    raw = {"status": "searched", "lines": ["朱崙街", "53巷"], "field": "朱崙街",
           "results": [{"name": "Zhulun Street", "shown": "Zhulun Street 朱崙街"}]}
    s = op.score(rec, raw)
    assert (s["query"], s["found1"]) == (True, True)
    assert op.result_names({"name": "Zhulun Street", "shown": "Zhulun Street 朱崙街"}) == ["Zhulun Street", "朱崙街"]
    # a line is matched whole: "Medical centre" on the sign is not every medical centre
    clinic = _rec(lines=[{"text": "Al'med"}, {"text": "медицинский центр"}], expect={"query": "Al'med Krasnoyarsk"})
    assert not op.score(clinic, {"status": "searched", "lines": ["x"], "field": "x",
                                 "results": [{"name": "Медицинский центр Здоровье", "shown": ""}]})["found3"]
    # another street of the sign's district is not it
    other = dict(raw, results=[{"name": "Section 3, Zhongxiao Road", "shown": "Section 3, Zhongxiao Road 忠孝路"}])
    assert not op.score(rec, other)["found3"]


def test_the_place_by_its_coordinates_not_its_name() -> None:
    """A hotel key's hotel is the one at expect.near, not one of its name elsewhere."""
    rec = _rec(id="kc-1", kind="key card", lines=[{"text": "Hotel Ibis"}],
               expect={"query": "Hotel Ibis", "record": "Ibis", "near": [26.2285, 50.5860]})
    raw = {"status": "searched", "lines": ["Hotel Ibis"], "field": "Hotel Ibis",
           "results": [{"name": "Ibis", "shown": "Ibis", "lat": 26.1, "lon": 50.5, "type": "poi"},
                       {"name": "Ibis", "shown": "Ibis", "lat": 26.2286, "lon": 50.5861, "type": "poi"}]}
    s = op.score(rec, raw)
    assert (s["found1"], s["at1"], s["at3"]) == (True, False, True)
    assert op.score(_rec(), raw)["at1"] is None          # no coordinates: not scored
    street = dict(raw, results=[{"name": "Road 116", "lat": 26.2360, "lon": 50.5861, "type": "street"}])
    assert op.score(rec, street)["at1"] is True          # a street within 1 km


def test_a_photo_not_run_scores_nothing_and_a_negative_passes_on_nothing_found() -> None:
    s = op.score(_rec(), {})
    assert (s["status"], s["read"], s["query"], s["found3"]) == ("not run", 0.0, False, False)
    # no text found: the field still holds the last photo's search, not this one's
    assert not op.score(_rec(), {"status": "none", "lines": [], "field": "Bani Jamrah"})["query"]
    neg = _rec(kind="negative", lines=[{"text": "თბილისი", "lang": "ka"}])
    assert op.score(neg, {"status": "none", "lines": []})["negative_ok"]
    assert not op.score(neg, {"status": "searched", "lines": ["x"], "field": "x",
                              "results": [{"name": "Tbilisi", "shown": ""}]})["negative_ok"]


def test_newest_map_of_a_region_not_another_regions(tmp_path: Path) -> None:
    for n in ("osm-switzerland-2026-10-06.zim", "osm-switzerland-2026-10-24.zim", "osm-switzerland-2026-10-24b.zim",
              "osm-switzerland-de-2026-11-01.zim", "osm-switzerland-light-2026-11-02.zim",
              "osm-switzerland-2026-10-30.zim.tmp"):
        (tmp_path / n).write_bytes(b"")
    assert op.newest_map(tmp_path, "switzerland").name == "osm-switzerland-2026-10-24b.zim"
    assert op.newest_map(tmp_path, "switzerland-de").name == "osm-switzerland-de-2026-11-01.zim"
    assert op.newest_map(tmp_path, "japan") is None


# ---- the overlay -------------------------------------------------------------

@pytest.fixture()
def base(tmp_path: Path) -> Path:
    pytest.importorskip("libzim")
    from tests.langpack import lpzims
    z = tmp_path / "osm-testland-2026-10-13.zim"
    lpzims.write_base(z)
    return z


def _serve(zim: Path, overlay: Path | None):
    from libzim.reader import Archive

    from cloud.serve_zim_entries import make_handler
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), make_handler(Archive(str(zim)), overlay and str(overlay)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _get(srv, path: str) -> tuple[int, str, bytes]:
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.getheader("Content-Type") or "", r.read()


def test_the_overlay_serves_its_files_in_place_of_the_maps(base: Path, tmp_path: Path) -> None:
    ov = tmp_path / "ov"
    (ov / "ocr").mkdir(parents=True)
    (ov / "index.html").write_text("<html>today's viewer</html>", encoding="utf-8")
    kept = tmp_path / "models"
    kept.mkdir()
    (kept / "det.onnx").write_bytes(b"\x08model")
    (ov / "ocr" / "det.onnx").symlink_to(kept / "det.onnx")
    (tmp_path / "secret.txt").write_text("not served", encoding="utf-8")
    srv = _serve(base, ov)
    try:
        assert _get(srv, "/index.html") == (200, "text/html", b"<html>today's viewer</html>")
        status, ctype, body = _get(srv, "/map-config.json")              # not in the overlay: the map's
        assert status == 200 and json.loads(body)["name"] == "Testland"
        assert _get(srv, "/ocr/det.onnx")[::2] == (200, b"\x08model")      # its link to the models, followed
        assert _get(srv, "/ocr/../../secret.txt")[0] == 404
        assert _get(srv, "/ocr/%2e%2e/%2e%2e/secret.txt")[0] == 404
        assert _get(srv, "/ocr/missing.onnx")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
    plain = _serve(base, None)
    try:
        status, _, body = _get(plain, "/index.html")
        assert status == 200 and b"today's viewer" not in body
    finally:
        plain.shutdown()
        plain.server_close()


def test_overlay_for_a_map_without_the_reader_gets_latin_and_the_photos_scripts(base: Path, tmp_path: Path) -> None:
    from libzim.reader import Archive
    models = tmp_path / "models"
    (models / "ocr").mkdir(parents=True)
    full = {"version": 1, "det": {"model": "ocr/det.onnx"},
            "rec": [{"name": n, "model": f"ocr/{n}.onnx"} for n in ("latin-cjk", "korean", "arabic", "greek")]}
    (models / "ocr" / "manifest.json").write_text(json.dumps(full), encoding="utf-8")
    for n in ("det", "latin-cjk", "korean", "arabic", "greek"):
        (models / "ocr" / f"{n}.onnx").write_bytes(n.encode())
    dest = tmp_path / "ov"
    names = op.overlay_for(Archive(str(base)), [{"scripts": ["Arab", "Latn"]}, {"scripts": ["Geor", "Zyyy"]}],
                           models, dest, "map")
    assert names == ["latin-cjk", "arabic"]
    assert [r["name"] for r in json.loads((dest / "ocr" / "manifest.json").read_text())["rec"]] == names
    cfg = json.loads((dest / "map-config.json").read_text())
    assert cfg["ocr"] == "ocr/manifest.json" and cfg["name"] == "Testland"
    assert (dest / "ocr" / "greek.onnx").read_bytes() == b"greek"     # every model linked; the manifest picks
    assert b"<html" in (dest / "index.html").read_bytes()[:400].lower()
    assert op.overlay_for(Archive(str(base)), [{"scripts": ["Arab"]}], models, tmp_path / "ov2", "all") == \
        ["latin-cjk", "korean", "arabic", "greek"]
