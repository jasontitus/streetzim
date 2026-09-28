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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TARGET = ROOT / "offliner-definition.json"

from streetzim.cli import ZIM_METADATA_FLAGS, ZIMFARM, build_parser  # noqa: E402

SKIP_ACTIONS = (argparse._HelpAction, argparse._VersionAction)


def definition() -> dict:
    flags: dict = {}
    for action in build_parser()._actions:
        if isinstance(action, SKIP_ACTIONS) or action.help == argparse.SUPPRESS:
            continue
        longs = [o for o in action.option_strings if o.startswith("--")]
        if not longs:
            continue
        # Zimfarm passes a boolean flag only when it is ticked, so a flag that
        # defaults to on is offered as its --no- form ("routing" -> "no_routing").
        if isinstance(action, argparse.BooleanOptionalAction) and action.default:
            longs = [o for o in longs if o.startswith("--no-")]
        key = longs[0][2:].replace("-", "_")
        extra = dict(ZIMFARM.get(key, {}))
        if extra.pop("offliner", True) is False:
            continue
        if isinstance(action, (argparse._StoreTrueAction, argparse.BooleanOptionalAction)):
            typ = "boolean"
        elif action.type is int:
            typ = "integer"
        else:
            typ = "string"
        entry = {
            "type": typ,
            "required": bool(action.required),
            "title": extra.pop("title", key.replace("_", " ").capitalize()),
            "description": (action.help or "").replace("%%", "%"),
        }
        if isinstance(action.choices, range):
            entry["min"], entry["max"] = action.choices.start, action.choices.stop - 1
        entry.update(extra)
        flags[key] = entry
    return {
        "offliner_id": "streetzim",
        "stdOutput": True,
        "stdStats": True,
        "flags": flags,
        "zimMetadata": [{"metadata": m, "flag": f} for m, f in ZIM_METADATA_FLAGS.items()
                        if f in flags],
    }


def render() -> str:
    return json.dumps(definition(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
