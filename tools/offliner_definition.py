#!/usr/bin/env python3
"""Generate offliner-definition.json (Zimfarm's description of our flags)
from the `streetzim` argument parser, in the format maps2zim publishes.

    python tools/offliner_definition.py            # rewrite the file
    python tools/offliner_definition.py --check    # fail if it is stale (CI)

Each flag's key is its long option with dashes as underscores; the type,
required-ness and description come from argparse; titles, patterns and
limits come from streetzim.cli.ZIMFARM. Flags marked offliner=False there
(developer options such as --tmp) are left out.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TARGET = ROOT / "offliner-definition.json"

from streetzim.cli import (  # noqa: E402
    MODEL_VALIDATORS, PROFILES, ZIM_METADATA_FLAGS, ZIMFARM, build_parser)

SKIP_ACTIONS = (argparse._HelpAction, argparse._VersionAction)  # pyright: ignore[reportPrivateUsage]
BOOLEAN_ACTIONS = (argparse._StoreTrueAction,  # pyright: ignore[reportPrivateUsage]
                   argparse._StoreFalseAction,  # pyright: ignore[reportPrivateUsage]
                   argparse.BooleanOptionalAction)
PROFILE_FEATURES = {d for feats in PROFILES.values() for d in feats}


def _offered(action: argparse.Action) -> list[str]:
    """The long options of `action` Zimfarm gets a flag for. Zimfarm passes a
    boolean only when it is ticked, so a flag that defaults to on is offered
    as its --no- form ("routing" -> "no_routing"), and a feature --profile
    sets is offered both ways, to override either profile."""
    longs = [o for o in action.option_strings if o.startswith("--")]
    if isinstance(action, argparse.BooleanOptionalAction):
        if action.dest in PROFILE_FEATURES:
            return longs
        if action.default:
            return [o for o in longs if o.startswith("--no-")]
    return longs[:1]


def _description(action: argparse.Action, option: str) -> str:
    text = (action.help or "").replace("%%", "%")
    if option.startswith("--no-") and isinstance(action, argparse.BooleanOptionalAction):
        # One help text serves --x and --no-x; say which way this one goes.
        text = f"Turn {option.replace('--no-', '--', 1)} off, whatever the profile"
    return text


def definition() -> dict[str, Any]:
    flags: dict[str, dict[str, Any]] = {}
    for action in build_parser()._actions:
        if isinstance(action, SKIP_ACTIONS) or action.help == argparse.SUPPRESS:
            continue
        for option in _offered(action):
            key = option[2:].replace("-", "_")
            entry = _entry(action, option, key)
            if entry is not None:
                flags[key] = entry
    return {
        "offliner_id": "streetzim",
        "stdOutput": True,
        "stdStats": True,
        "flags": flags,
        "modelValidators": MODEL_VALIDATORS,
        "zimMetadata": [{"metadata": m, "flag": f} for m, f in ZIM_METADATA_FLAGS.items()
                        if f in flags],
    }


def _entry(action: argparse.Action, option: str, key: str) -> dict[str, Any] | None:
    extra = dict(ZIMFARM.get(key, {}))
    if extra.pop("offliner", True) is False:
        return None
    if isinstance(action, BOOLEAN_ACTIONS):
        typ = "boolean"
    elif action.type is int:
        typ = "integer"
    elif isinstance(action.choices, (list, tuple)):
        typ = "string-enum"
    else:
        typ = "string"
    entry: dict[str, Any] = {
        "type": typ,
        "required": bool(action.required),
        "title": extra.pop("title", key.replace("_", " ").capitalize()),
        "description": _description(action, option),
    }
    if isinstance(action.choices, range):
        entry["min"], entry["max"] = action.choices.start, action.choices.stop - 1
    elif typ == "string-enum":
        entry["choices"] = list(action.choices or ())
    if extra.get("choices") == "KNOWN_AREAS":
        from create_osm_zim import KNOWN_AREAS
        extra["choices"] = sorted(KNOWN_AREAS)
    entry.update(extra)
    return entry


def render() -> str:
    return json.dumps(definition(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != text:
            print("offliner-definition.json is stale: run python tools/offliner_definition.py",
                  file=sys.stderr)
            return 1
        print("ok: offliner-definition.json matches streetzim's flags")
        return 0
    TARGET.write_text(text)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
