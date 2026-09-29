"""offliner-definition.json: current with the `streetzim` parser, and every
flag it offers is one Zimfarm can actually pass."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

od = pytest.importorskip("offliner_definition")
from streetzim.cli import build_parser  # noqa: E402

DEF = json.loads((ROOT / "offliner-definition.json").read_text())


def test_committed_file_is_current():
    assert (ROOT / "offliner-definition.json").read_text() == od.render(), \
        "run: python tools/offliner_definition.py"


def test_shape_matches_what_zimfarm_expects():
    assert DEF["offliner_id"] == "streetzim"
    assert DEF["stdOutput"] is True and DEF["stdStats"] is True
    for key, f in DEF["flags"].items():
        assert set(f) >= {"type", "required", "title", "description"}, key
        assert f["type"] in ("string", "boolean", "integer", "url", "string-enum"), key
        assert ("choices" in f) == (f["type"] == "string-enum"), key
    assert DEF["flags"]["output"]["pattern"] == "^/output$"
    assert DEF["flags"]["stats_filename"]["pattern"] == r"^/output/task_progress\.json$"
    flags = set(DEF["flags"])
    assert all(m["flag"] in flags for m in DEF["zimMetadata"])
    assert {m["metadata"] for m in DEF["zimMetadata"]} >= {"Name", "Title", "Description"}
    assert "satellite" not in flags          # never offered: CC BY-NC-SA
    assert DEF["modelValidators"] == [{"name": "check_exclusive_fields",
                                       "fields": ["area", "include_poly", "bbox"]}]
    assert "monaco" in DEF["flags"]["area"]["choices"]


def _argv_for(config: dict) -> list[str]:
    """What Zimfarm runs for a flags config: --key-with-dashes=value
    (zimfarm_backend compute_flags, use_equals=True), booleans as bare flags
    when true."""
    argv = []
    for key, value in config.items():
        opt = "--" + key.replace("_", "-")
        if DEF["flags"][key]["type"] == "boolean":
            if value:
                argv.append(opt)
        else:
            argv.append(f"{opt}={value}")
    return argv


def test_every_offered_flag_parses():
    sample = {"string": "x", "integer": 3, "boolean": True, "url": "https://example.org/x"}
    config = {k: (f["choices"][0] if f["type"] == "string-enum" else sample[f["type"]])
              for k, f in DEF["flags"].items()}
    del config["area"], config["include_poly"]      # exclusive with bbox
    config.update(output="/output", stats_filename="/output/task_progress.json",
                  bbox="7.4,43.72,7.44,43.76", default_view="43.7,7.4,12", max_zoom=12)
    args = build_parser().parse_args(_argv_for(config))
    assert args.routing is False             # no_routing ticked
    assert args.terrain is False             # no_terrain ticked
    assert args.stats_filename == "/output/task_progress.json"


def test_required_flags_agree_with_the_parser():
    required = {k for k, f in DEF["flags"].items() if f["required"]}
    assert required == {"name", "title", "description"}
    with pytest.raises(SystemExit):
        build_parser().parse_args(_argv_for({"name": "n", "title": "t"}))


def test_terrain_is_on_unless_the_recipe_ticks_no_terrain():
    flags = DEF["flags"]
    assert "terrain" not in flags
    assert flags["no_terrain"]["type"] == "boolean"
    assert not flags["no_terrain"]["required"]
    args = build_parser().parse_args(_argv_for({"name": "n", "title": "t",
                                                "description": "d", "area": "monaco"}))
    assert args.terrain is True and args.routing is True
