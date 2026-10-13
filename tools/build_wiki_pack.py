#!/usr/bin/env python3
"""Build a Wikipedia pack for the language matrix (streetzim/wiki_pack.py).

    tools/build_wiki_pack.py BASE.zim --lang de --source wikipedia_de_all_maxi_2026-01.zim \\
        --region greece -o osm_greece_wiki-de_2026-10-13.zim [--cache DIR] [--images lead]

The pack holds de Wikipedia's articles about the base's places under ~w/de/
(docs/language-matrix-prototype.md, "Wikipedia packs").
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streetzim.wiki_pack import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
