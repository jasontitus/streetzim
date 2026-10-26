"""Shared format for the fixed-size viewer slots.

Both cloud/swap_viewer_rust.py (writes them) and
cloud/patch_viewer_inplace.py (overwrites them) must agree byte-for-byte on
this header, so it lives in exactly one place.

Layout of a slot, always exactly `slot` bytes:

    <content><opener>SZVSLOT1:<name padded 24>:<slot len 12 digits>:<newlines><closer>

The marker sits AFTER the content so the file still parses as HTML/JS from
byte zero, and it records the slot length so the patcher never infers it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

SLOT_MAGIC = b"SZVSLOT1"
NAME_WIDTH = 24
LEN_WIDTH = 12
VIEWER_FILES = ("index.html", "places.html", "routing-worker.js")

# Right-sized per file rather than a flat 1 MB each: index.html is the one
# that actually changes and keeps 2.3x growth room, the other two are small
# and static. Flat 1 MB cost 3 MB uncompressed per ZIM; this costs 1.4 MB.
SLOT_SIZES = {
    "index.html":        1024 * 1024,
    "places.html":        256 * 1024,
    "routing-worker.js":  128 * 1024,
}


def comment_delims(name: str) -> tuple[bytes, bytes]:
    return (b"/*", b"*/") if name.endswith(".js") else (b"<!--", b"-->")


def slot_header(name: str, slot_len: int) -> bytes:
    opener, _ = comment_delims(name)
    return (opener + SLOT_MAGIC + b":" + name.encode().ljust(NAME_WIDTH, b" ")
            + b":" + str(slot_len).encode().rjust(LEN_WIDTH, b"0") + b":")


def slot_needle(name: str) -> bytes:
    """What the patcher scans for."""
    return SLOT_MAGIC + b":" + name.encode().ljust(NAME_WIDTH, b" ") + b":"


def slot_free(name: str, size: int, slot_len: int | None = None) -> int:
    """Bytes left in `name`'s slot for content of `size` bytes once the
    marker is in; negative when it would not fit."""
    slot_len = slot_len or SLOT_SIZES[name]
    _, closer = comment_delims(name)
    return slot_len - size - len(slot_header(name, slot_len)) - len(closer)


# Per-map UI tables (docs/i18n.md#adding-a-language). The built viewer
# (resources/viewer) inlines a table for every language it has; a ZIM's copy
# keeps those of the base languages (the core ones and Swedish) and the map's
# own: map-config "language", and the first UI_REGION_TABLES of its
# "regionLanguages" (largest first) that have a table, so the slots hold
# them, with their margin, whatever the number of tables. The website's copy
# keeps all.
UI_PAGES = ("index.html", "places.html")
UI_REGION_TABLES = 6
_I18N_BLOCK = re.compile(rb'(<script type="application/json" id="sz-i18n">)(.*?)(</script>)', re.S)
REGION_LANGUAGES_TSV = Path(__file__).resolve().parent / "region-languages.tsv"


def ui_languages(map_config: dict[str, Any] | None, available: set[str] | None = None) -> set[str]:
    """The UI tables a ZIM with this map-config carries, of `available` (the
    built page's; None: any)."""
    from streetzim.languages import MATRIX_TILE_LANGUAGES
    keep = set(MATRIX_TILE_LANGUAGES) | {"sv"}
    cfg: dict[str, Any] = map_config if isinstance(map_config, dict) else {}
    lang: Any = cfg.get("language")
    if isinstance(lang, str) and lang:
        keep.add(lang.lower().split("-")[0])
    region: Any = cfg.get("regionLanguages")
    items = cast(list[Any], region) if isinstance(region, list) else []
    own: list[str] = [str(x).lower().split("-")[0] for x in items if isinstance(x, str) and x]
    own = [x for x in dict.fromkeys(own) if x not in keep and (available is None or x in available)]
    return keep | set(own[:UI_REGION_TABLES])


def select_ui_tables(data: bytes, keep: set[str]) -> bytes:
    """index.html or places.html with only the UI tables of `keep` in its
    sz-i18n block (tools/build_i18n.py's format: one packed text array per
    language under "t", its settings under "m"); unchanged without one."""
    m = _I18N_BLOCK.search(data)
    if not m:
        return data
    block: dict[str, Any] = json.loads(m.group(2))
    tables: dict[str, Any] = block.get("t") or {}
    if set(tables) <= keep:
        return data
    block["t"] = {lang: v for lang, v in tables.items() if lang in keep}
    settings: dict[str, Any] = block.get("m") or {}
    block["m"] = {lang: v for lang, v in settings.items() if lang in keep}
    packed = json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    return data[:m.start(2)] + packed.encode("utf-8") + data[m.end(2):]


def map_ui_tables(data: bytes, map_config: dict[str, Any] | None) -> bytes:
    """index.html or places.html with the UI tables a ZIM with this
    map-config carries (ui_languages, of the page's own)."""
    m = _I18N_BLOCK.search(data)
    if not m:
        return data
    block: dict[str, Any] = json.loads(m.group(2))
    available = set(block.get("t") or {})
    return select_ui_tables(data, ui_languages(map_config, available))


def viewer_page(name: str, viewer_dir: Path | str, map_config: dict[str, Any] | None) -> bytes:
    """A viewer file as a ZIM with this map-config carries it."""
    data = (Path(viewer_dir) / name).read_bytes()
    return map_ui_tables(data, map_config) if name in UI_PAGES else data


def registry_map_configs() -> list[dict[str, Any]]:
    """A map-config stand-in for every row of cloud/region-languages.tsv:
    what the slots must hold for any map the registry describes."""
    rows: list[dict[str, Any]] = [{}]
    try:
        lines = REGION_LANGUAGES_TSV.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        f = line.split("\t")
        if len(f) > 1 and f[0] and not f[0].startswith("#"):
            rows.append({"regionLanguages": [x.strip() for x in f[1].split(",") if x.strip()]})
    return rows


def check_viewer_fits(viewer_dir: Path | str, map_config: dict[str, Any] | None = None) -> None:
    """Fail at once when a viewer file would overflow its slot: for the map
    of `map_config`, else for every map the registry describes (the most
    UI tables any of them carries).

    create_osm_zim.py and cloud/repackage_zim.py call this first, so a viewer
    grown past its slot (every UI table adds to index.html and places.html)
    stops a build in seconds rather than at the packing step, hours of data
    phases later. pad_to_slot keeps its own check as the last line of
    defence."""
    over: list[str] = []
    configs: list[dict[str, Any] | None] = [map_config] if map_config is not None else list(registry_map_configs())
    for name in VIEWER_FILES:
        p = Path(viewer_dir) / name
        if not p.exists():
            continue
        size = max(len(viewer_page(name, viewer_dir, cfg)) for cfg in configs)
        free = slot_free(name, size)
        if free < 0:
            over.append(f"{name} is {size} B, {-free} B over its {SLOT_SIZES[name]} B slot")
    if over:
        raise SystemExit(
            "viewer slot overflow: " + "; ".join(over) + ". Shrink the viewer, or raise "
            "SLOT_SIZES in cloud/viewer_slots.py and re-pack every region (a full viewer "
            "swap); do NOT truncate.")


def pad_to_slot(name: str, data: bytes, slot_len: int | None = None) -> bytes:
    slot_len = slot_len or SLOT_SIZES[name]
    _, closer = comment_delims(name)
    head = slot_header(name, slot_len)
    overhead = len(head) + len(closer)
    if len(data) + overhead > slot_len:
        raise SystemExit(
            f"viewer slot overflow: {name} is {len(data)} B + {overhead} B "
            f"marker > {slot_len} B slot. Raise SLOT_SIZES[{name!r}] in "
            f"cloud/viewer_slots.py and re-pack that region (a full "
            f"viewer swap); do NOT truncate.")
    return data + head + (b"\n" * (slot_len - len(data) - overhead)) + closer
