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
from streetzim.cli import PROFILES, build_parser, parse_args, profile_features  # noqa: E402

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
                  bbox="7.4,43.72,7.44,43.76", default_view="43.7,7.4,12", max_zoom=12,
                  overture_release="2026-09-23.1")
    args = parse_args(_argv_for(config))
    assert args.routing is False             # no_routing ticked
    assert args.stats_filename == "/output/task_progress.json"
    assert args.wikipedia and args.wikipedia_zim_url == "https://example.org/x"


def test_required_flags_agree_with_the_parser():
    required = {k for k, f in DEF["flags"].items() if f["required"]}
    # profile too, on Zimfarm only: every recipe states it next to its
    # resources, while the command line defaults to full.
    assert required == {"name", "title", "description", "profile"}
    with pytest.raises(SystemExit):
        build_parser().parse_args(_argv_for({"name": "n", "title": "t"}))


def test_profile_is_a_required_string_enum():
    f = DEF["flags"]["profile"]
    assert f["type"] == "string-enum" and f["choices"] == list(PROFILES)
    assert f["required"] and "default" not in f


def test_each_profile_feature_is_one_on_off_enum():
    # One key per feature, unset meaning "as the profile says": a recipe
    # cannot switch a feature on and off at once.
    features = profile_features(build_parser())
    assert {"wikidata", "wikipedia", "overture"} <= features
    for dest in features:
        f = DEF["flags"][dest]
        assert f["type"] == "string-enum" and f["choices"] == ["on", "off"], dest
        assert not f["required"] and "default" not in f, dest
        assert f"no_{dest}" not in DEF["flags"], dest


@pytest.mark.parametrize("config, expect", [
    ({"profile": "basic"}, {"wikidata": False, "wikipedia": False, "overture": False}),
    ({"profile": "basic", "wikidata": "on"}, {"wikidata": True, "overture": False}),
    ({"profile": "full", "overture": "off"}, {"overture": False, "wikipedia": True}),
    ({"profile": "full"}, {"wikidata": True, "wikipedia": True, "overture": True}),
])
def test_recipe_configs_become_the_features_they_say(config, expect):
    base = {"name": "osm_en_monaco", "title": "Monaco", "description": "d", "area": "monaco"}
    args = parse_args(_argv_for({**base, **config}))
    assert {k: getattr(args, k) for k in expect} == expect


def test_zimfarm_accepts_the_definition_and_its_recipes():
    """Zimfarm's own models (zimfarm_backend on PYTHONPATH; CI pins the
    commit): the definition, a recipe per profile, a bad profile refused,
    and the command Zimfarm builds from a recipe parses here."""
    import os
    for key in ("POSTGRES_URI", "JWT_SECRET"):
        os.environ.setdefault(key, "unused")
    models = pytest.importorskip("zimfarm_backend.common.schemas.offliners.models")
    from pydantic import ValidationError
    from zimfarm_backend.common.schemas.offliners.builder import build_offliner_model
    from zimfarm_backend.common.schemas.orms import OfflinerSchema
    from zimfarm_backend.utils.offliners import compute_flags
    spec = models.OfflinerSpecSchema.model_validate(DEF)
    # model_construct: upstream's image-name enum has no streetzim yet
    # (docs/zimfarm.md, "What Zimfarm needs on its side").
    off = OfflinerSchema.model_construct(
        id="streetzim", base_model="DashModel", docker_image_name="openzim/streetzim",
        command_name="streetzim", ci_secret_hash=None)
    model = build_offliner_model(off, spec)
    base = {"offliner_id": "streetzim", "name": "osm_en_monaco", "title": "Monaco",
            "description": "Offline Monaco", "area": "monaco"}
    for extra, expect in [({"profile": "basic", "overture": "on"},
                           {"profile": "basic", "overture": True, "wikidata": False}),
                          ({"profile": "full", "wikipedia": "off"},
                           {"profile": "full", "wikipedia": False, "overture": True}),
                          ({"profile": "full"}, {"profile": "full", "wikipedia": True})]:
        recipe = model.model_validate({**base, **extra})
        flags = recipe.model_dump(mode="json")
        argv = compute_flags(flags)
        args = parse_args([a.replace("'", "") for a in argv])
        assert {k: getattr(args, k) for k in expect} == expect, argv
    for bad in ({"profile": "everything"}, {}, {"profile": "full", "wikidata": "yes"},
                {"profile": "full", "wikidata": True}):
        with pytest.raises(ValidationError):
            model.model_validate({**base, **bad})
    with pytest.raises(ValidationError):
        model.model_validate({**base, "overture-release": "newest"})
