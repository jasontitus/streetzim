#!/usr/bin/env python3
"""Generate offliner-definition-langpack.json (Zimfarm's description of the
`streetzim-langpack` flags) from its argument parser, as
tools/offliner_definition.py does for `streetzim`.

    python tools/offliner_definition_langpack.py            # rewrite the file
    python tools/offliner_definition_langpack.py --check    # fail if it is stale (CI)

Each flag's key is its long option with dashes as underscores; the type,
required-ness and description come from argparse; titles, patterns and
types argparse cannot say (url, list-of-url) come from
streetzim.langpack_cli.ZIMFARM. Flags marked offliner=False there are left
out. --mode is a string-enum whose choices list their own flags as
"dependents": Zimfarm clears the other mode's flags when a recipe is saved.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TARGET = ROOT / "offliner-definition-langpack.json"

from offliner_definition import BOOLEAN_ACTIONS, SKIP_ACTIONS  # noqa: E402

from streetzim.langpack_cli import (  # noqa: E402
    MODE_FLAGS, MODE_TITLES, MODES, ZIMFARM, build_parser, positive_int)


def _entry(action: argparse.Action, key: str) -> dict[str, Any] | None:
    extra = dict(ZIMFARM.get(key, {}))
    if extra.pop("offliner", True) is False:
        return None
    if isinstance(action, BOOLEAN_ACTIONS):
        typ = "boolean"
    elif action.type in (int, positive_int):
        typ = "integer"
    elif action.choices:
        typ = "string-enum"
    else:
        typ = "string"
    entry: dict[str, Any] = {
        "type": typ,
        "required": bool(action.required),
        "title": extra.pop("title", key.replace("_", " ").capitalize()),
        "description": (action.help or "").replace("%%", "%"),
    }
    if key == "mode":
        entry["choices"] = [{"title": MODE_TITLES[m], "value": m, "dependents": MODE_FLAGS[m]}
                            for m in MODES]
    elif typ == "string-enum":
        entry["choices"] = list(action.choices or ())
    entry.update(extra)
    return entry


def definition() -> dict[str, Any]:
    flags: dict[str, dict[str, Any]] = {}
    for action in build_parser()._actions:
        if isinstance(action, SKIP_ACTIONS) or action.help == argparse.SUPPRESS:
            continue
        longs = [o for o in action.option_strings if o.startswith("--")]
        if not longs:
            continue
        key = longs[0][2:].replace("-", "_")
        entry = _entry(action, key)
        if entry is not None:
            flags[key] = entry
    return {
        "offliner_id": "streetzim-langpack",
        "stdOutput": True,
        "stdStats": True,
        "flags": flags,
        "modelValidators": [],
        "zimMetadata": [],
    }


def render() -> str:
    return json.dumps(definition(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != text:
            print(f"{TARGET.name} is stale: run python tools/offliner_definition_langpack.py",
                  file=sys.stderr)
            return 1
        print(f"ok: {TARGET.name} matches streetzim-langpack's flags")
        return 0
    TARGET.write_text(text)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
