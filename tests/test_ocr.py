"""The sign reader (--ocr, streetzim/ocr.py, docs/ocr.md): which models a
map gets from the scripts of its names, the ZIM entries and manifest that
carry them, map-config's `ocr` key, the pins in the lock file, and the
flag's way from the command line to create_zim. No network: the pinned
downloads are faked."""
import json

import pytest

from streetzim import ocr, viewer_assets as va, zim_writer


class FakeItem:
    def __init__(self, path, title, mimetype, content, is_front=False, compress=True, namespace=None):
        self.path, self.title, self.mimetype, self.content = path, title, mimetype, content


class FakeCreator:
    def __init__(self):
        self.items = {}

    def add_item(self, item):
        self.items[item.path] = item


@pytest.fixture
def offline(monkeypatch):
    """fetch_verified returns placeholder bytes naming what was asked for."""
    asked = []

    def fake(url, digest, **kw):
        asked.append(url)
        return f"bytes of {url}".encode()

    monkeypatch.setattr(va, "fetch_verified", fake)
    return asked


def test_scripts_of_names():
    assert ocr.scripts_of("Boulevard des Moulins") == {"Latn"}
    assert ocr.scripts_of("北京大学") == {"Hani"}
    assert ocr.scripts_of("かっぱ橋道具街") == {"Hira", "Hani"}
    assert ocr.scripts_of("ถนนสีลม") == {"Thai"}
    assert ocr.scripts_of("Улица 7") == {"Cyrl"}
    assert ocr.scripts_of("רחוב הרצל") == {"Hebr"}
    assert ocr.scripts_of("123 - !") == set()


def test_native_names_prefer_the_own_script_name(tmp_path):
    recs = [{"name": "Peking University", "name_native": "北京大学"}, {"name": "Casino"},
            {"name": ""}, {"type": "poi"}]
    assert list(ocr.native_names(recs)) == ["北京大学", "Casino"]
    path = tmp_path / "features.jsonl"
    # extraction writes ASCII escapes, the merge passes raw UTF-8: both read
    path.write_text(json.dumps(recs[0]) + "\n" + json.dumps(recs[1], ensure_ascii=False)
                    + "\nnot json\n", encoding="utf-8")
    assert list(ocr.native_names(str(path))) == ["北京大学", "Casino"]
    assert list(ocr.native_names(recs, limit=1)) == ["北京大学"]
    assert list(ocr.native_names(None)) == []


def _plan(names, **kw):
    return ocr.choose_models(*ocr.count_scripts(names), **kw)


def test_one_recognizer_per_script_used():
    assert _plan(["Rue de la Paix"] * 10).recognizers == ("latin-cjk",)
    # Latin and Chinese/Japanese share PP-OCRv6
    assert _plan(["渋谷駅"] * 5 + ["さくら通り"] * 5 + ["Shibuya"] * 5).recognizers == ("latin-cjk",)
    assert _plan(["Улица"] * 60 + ["Ulica"] * 40).recognizers == ("cyrillic", "latin-cjk")
    assert _plan(["서울"] * 9 + ["몽골"]).detector == "det"


def test_latin_beside_another_script_needs_a_quarter_of_the_names():
    # every recognizer reads plain Latin: brands in Bangkok add no model
    assert _plan(["ถนนสีลม"] * 90 + ["Starbucks"] * 10).recognizers == ("thai",)
    assert _plan(["ถนนสีลม"] * 70 + ["Starbucks"] * 30).recognizers == ("thai", "latin-cjk")
    # other scripts need 5%
    assert _plan(["Москва"] * 96 + ["Αθήνα"] * 4).recognizers == ("cyrillic",)
    assert _plan(["Москва"] * 94 + ["Αθήνα"] * 6).recognizers == ("cyrillic", "greek")


def test_scripts_without_a_recognizer_fall_back_to_latin_and_are_reported():
    plan = _plan(["רחוב הרצל"] * 90 + ["Starbucks"] * 10)
    assert plan.recognizers == ("latin-cjk",) and plan.unread == ("Hebr",)
    assert _plan([]).recognizers == ("latin-cjk",)


def test_scripts_given_on_the_command_line():
    assert _plan(["Rue"], scripts=["Arab", "Latn"]).recognizers == ("arabic", "latin-cjk")
    with pytest.raises(ValueError, match="Hebr"):
        ocr.check_scripts(["Hebr"])
    with pytest.raises(ValueError):
        ocr.check_scripts([])


def test_the_lock_pins_what_the_tool_names():
    import importlib.util
    from streetzim.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("pin", REPO_ROOT / "tools" / "pin_viewer_assets.py")
    pin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin)
    lock = va.load_lock()
    assert pin.check_ocr(lock)
    models = lock[va.OCR_MODELS]["models"]
    for m in models.values():
        assert m["url"].startswith("https://huggingface.co/") and m["revision"] in m["url"]
        assert len(m["sha256"]) == 64
    # every script with a recognizer is one ocr.py can detect
    assert set(ocr.recognizers_by_script(lock)) <= set(ocr.SCRIPT_RANGES)
    assert lock[va.OCR_ENGINE]["wasm"]["url"].endswith("/" + lock[va.OCR_ENGINE]["wasm"]["file"])


def test_entries_and_manifest(offline):
    plan = ocr.Plan("det", ("arabic", "latin-cjk"), ("Hebr",))
    items = {p: (t, mime, data) for p, t, mime, data in ocr.entries(plan)}
    manifest = json.loads(items[ocr.MANIFEST_ENTRY][2])
    assert manifest["version"] == ocr.MANIFEST_VERSION
    for key in ("worker", "engine", "wasm", "licences"):
        assert manifest[key] in items, key
    assert manifest["det"] == {"model": "ocr/det.onnx", "preset": "PP-OCRv6_tiny",
                               "options": {"maxSideLength": 960, "limitType": "max"}}
    assert [r["name"] for r in manifest["rec"]] == ["arabic", "latin-cjk"]
    assert manifest["rec"][0]["reverse"] is True and manifest["rec"][1]["reverse"] is False
    for r in manifest["rec"]:
        assert items[r["model"]][1] == "application/octet-stream"
        dictionary = items[r["dict"]][2].decode("utf-8").split("\n")
        assert len(dictionary) > 50
    assert items[manifest["wasm"]][1] == "application/wasm"
    assert items[manifest["worker"]][2].startswith(b"/* StreetZim sign reader")
    licences = items[manifest["licences"]][2].decode()
    for text in ("Apache License", "MIT License", "Microsoft", "x3zvawq"):
        assert text in licences
    # only the plan's models are fetched, with the runtime
    assert len(offline) == 4 and not any("korean" in u for u in offline)


def test_the_zim_gets_the_entries_and_map_config_the_key(offline):
    c = FakeCreator()
    zim_writer._add_ocr(c, FakeItem, plan=ocr.Plan("det", ("thai",)))
    assert ocr.MANIFEST_ENTRY in c.items and "ocr/thai.onnx" in c.items
    zim_writer._add_map_config(c, FakeItem, map_config={"name": "t"}, has_wiki_articles=False,
                               has_ocr=True)
    assert json.loads(c.items["map-config.json"].content)["ocr"] == ocr.MANIFEST_ENTRY
    zim_writer._add_map_config(c, FakeItem, map_config={"name": "t"}, has_wiki_articles=False)
    assert "ocr" not in json.loads(c.items["map-config.json"].content)


@pytest.mark.parametrize("argv,want", [([], None), (["--ocr"], ("latin-cjk",)),
                                       (["--ocr", "--ocr-scripts", "thai,latn"], ("thai", "latin-cjk"))])
def test_create_osm_zim_passes_the_plan_to_create_zim(tmp_path, monkeypatch, argv, want):
    """--ocr must reach create_zim(ocr=Plan): create_osm_zim -> _write_zim -> create_zim."""
    import create_osm_zim as c
    seen = {}
    monkeypatch.setattr(c, "create_zim", lambda **kw: seen.update(kw))
    args = c.build_parser().parse_args(["--bbox", "7.39,43.715,7.46,43.765"] + argv)
    c._write_zim(
        address_count=0, args=args, bbox_str="7.39,43.715,7.46,43.765", fonts={},
        map_config={}, maplibre_css="c", maplibre_js="j", mbtiles_path=None, name="M",
        output_path=str(tmp_path / "t.zim"), overture_sources=None, overture_themes=None,
        routing_graph_path=None, satellite_dir=None, satellite_format=None,
        satellite_max_zoom=0, search_features=[{"name": "Casino"}], terrain_dir=None,
        terrain_max_zoom=0, tile_metadata={}, tiles={}, tmpdir=str(tmp_path),
        total_tile_count=0, use_streaming=False, wiki_cross_refs=None, wikidata_data=None,
        zim_illustration=None, zim_metadata=None)
    assert (seen["ocr"].recognizers if seen["ocr"] else None) == want

