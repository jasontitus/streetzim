"""tools/ml_search_cache.py: a region's search cache from its multilingual
tiles, extracted with the tiles' own languages."""
import datetime
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ml_search_cache", ROOT / "tools" / "ml_search_cache.py")
msc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msc)


def test_the_languages_are_the_tiles(tmp_path):
    (tmp_path / "egypt.ml.mbtiles.langs").write_text("ar,de,fr\n")
    assert msc.tile_languages("egypt", tmp_path) == "ar,de,fr"
    (tmp_path / "empty.ml.mbtiles.langs").write_text("\n")
    with pytest.raises(ValueError):
        msc.tile_languages("empty", tmp_path)


def test_the_default_folder_is_dated():
    assert msc.default_out("egypt", datetime.date(2026, 11, 7)) == \
        Path("/storage/streetzim/search_cache/ml-2026-11-07/egypt")


def test_it_extracts_with_the_tiles_languages(tmp_path, monkeypatch):
    (tmp_path / "egypt.ml.mbtiles").write_bytes(b"")
    (tmp_path / "egypt.ml.mbtiles.langs").write_text("ar,fr")
    seen = {}

    def fake(mbtiles_path=None, output_dir=None):
        seen.update(path=mbtiles_path, out=output_dir, langs=os.environ.get("STREETZIM_TILE_LANGUAGES"))
        return os.path.join(output_dir, "search_features.jsonl")
    monkeypatch.setitem(sys.modules, "streetzim.search_extract",
                        types.SimpleNamespace(extract_searchable_features=fake))
    monkeypatch.delenv("STREETZIM_TILE_LANGUAGES", raising=False)
    out = tmp_path / "out"
    assert msc.main(["egypt", "--regions", str(tmp_path), "--out", str(out)]) == 0
    assert seen == {"path": str(tmp_path / "egypt.ml.mbtiles"), "out": str(out), "langs": "ar,fr"}
    assert out.is_dir()


def test_no_tiles_no_cache(tmp_path, capsys):
    assert msc.main(["nowhere", "--regions", str(tmp_path), "--out", str(tmp_path / "o")]) == 1
    assert "no multilingual tiles" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
