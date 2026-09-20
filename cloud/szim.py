#!/usr/bin/env python3
"""szim — look inside a streetzim ZIM and trim it, with no toolchain.

    szim inspect FILE.zim|URL [--by-zoom] [--by-mime]   what is in it, what is large,
                                                        what each trim would save
    szim plan    FILE.zim [trim options]                which clusters copy / re-encode / drop
    szim trim    SRC.zim DST.zim [trim options]         write the trimmed ZIM
    szim verify  SRC.zim DST.zim [...]                  prove DST == SRC minus the recipe (needs python-libzim)
    szim sim     A.zim [B.zim ...] [...]                clusters read per map interaction, per layout

Needs Python 3.10+ and the ``zstandard`` module. ``verify`` additionally
needs ``libzim`` (pip install libzim). Output is ordinary ZIM: libzim, Kiwix
and zimru read it unchanged.

Trim options (see ``szim trim -h``): --light, --no-satellite,
--satellite-max-zoom N, --no-terrain, --terrain-max-zoom N,
--max-tile-zoom N, --no-routing, --no-wiki, --strip-addresses,
--drop-prefix P, --title/--name/--description, --regroup-tiles.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "inspect":
        from cloud.zim_inventory import main as m
        sys.argv = ["szim inspect", *rest]
        return m()
    if cmd == "plan":
        from cloud.derive_zim import main as m
        return m([*rest, "--dry-run"] if "--dry-run" not in rest else rest)
    if cmd == "trim":
        from cloud.derive_zim import main as m
        return m(rest)
    if cmd == "verify":
        try:
            import libzim  # noqa: F401
        except ImportError:
            print("szim verify needs python-libzim: pip install libzim", file=sys.stderr)
            return 2
        from cloud.verify_derived import main as m
        sys.argv = ["szim verify", *rest]
        return m()
    if cmd == "sim":
        from cloud.zim_access_sim import main as m
        sys.argv = ["szim sim", *rest]
        return m()
    print(f"szim: unknown command {cmd!r}\n", file=sys.stderr)
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
