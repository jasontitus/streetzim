"""Small ZIMs for the language-pack pipeline tests (tools/langpack/).

A StreetZim-like base (map-config, English Wikipedia articles, a geo-index,
tiles, metadata) and Wikipedia packs under ~w/<xx>/ (pack.json, articles
with the back-to-map link, a redirect, an image, metadata), written with
python-libzim's Creator. Article titles include a slash (a nested path) and
an apostrophe (zimcheck's report quotes them). `zimcheck_report` prints what
the official zimcheck -A says about a clean pack, so the pack gate runs
without zim-tools installed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import uuid as uuidlib
from pathlib import Path
from typing import Any

from libzim.writer import Creator, Hint, Item, StringProvider

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "langpack"
REGION = "testland"

# lang -> [(path under wiki-article/, title)]
ARTICLES: dict[str, list[tuple[str, str]]] = {
    "en": [("Alpha_Town", "Alpha Town"), ("Beta_Peak", "Beta Peak"), ("Gamma_Lake", "Gamma Lake")],
    "de": [("Alpha_Stadt", "Alpha Stadt"), ("AC/DC_Platz", "AC/DC Platz"), ("Beta_Gipfel", "Beta Gipfel")],
    "fr": [("Alpha_Ville", "Alpha Ville"), ("L'Île_aux_Oiseaux", "L'Île aux Oiseaux"),
           ("Lac_Gamma", "Lac Gamma"), ("Pic_Beta", "Pic Beta")],
    "th": [("อัลฟา", "อัลฟา")],
    "rm": [("Alfa_Citad", "Alfa Citad"), ("Piz_Beta", "Piz Beta")],
}


class _Item(Item):
    def __init__(self, path: str, title: str, mime: str, data: bytes | str, front: bool = False,
                 compress: bool | None = None):
        super().__init__()
        self._p, self._t, self._m = path, title, mime
        self._d = data.encode() if isinstance(data, str) else data
        self._front = front
        self._compress = compress

    def get_path(self) -> str:
        return self._p

    def get_title(self) -> str:
        return self._t

    def get_mimetype(self) -> str:
        return self._m

    def get_contentprovider(self) -> Any:
        return StringProvider(self._d)

    def get_hints(self) -> dict[Any, bool]:
        h = {Hint.FRONT_ARTICLE: self._front}
        if self._compress is not None:
            h[Hint.COMPRESS] = self._compress
        return h


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (48, 48), (30, 120, 200, 255)).save(buf, "PNG")
    return buf.getvalue()


def _webp(seed: bytes) -> bytes:
    return b"RIFF\x20\x00\x00\x00WEBPVP8 " + hashlib.sha256(seed).digest()


def _page(title: str, depth: int, body: str = "") -> str:
    back = "../" * depth + "index.html"
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{title}</title></head><body>"
            f"<a href='{back}'>map</a><h1>{title}</h1><p>{body or title}</p></body></html>")


VIEWER_V1 = {"index.html": b"<html><body>map, viewer 1</body></html>",
             "places.html": b"<html><body>places, viewer 1</body></html>",
             "routing-worker.js": b"/* routing, viewer 1 */"}


def write_base(path: Path, *, description: str = "Offline map of Testland", wiki: bool = True,
               extra: dict[str, Any] | None = None, slots: dict[str, bytes] | None = None,
               front_redirects: list[tuple[str, str, str]] | None = None, cfg_extra: dict[str, Any] | None = None,
               pages: list[tuple[str, str, str]] | None = None, fulltext: bool = False, fronts: bool = True,
               front_pages: list[tuple[str, str, str]] | None = None, all_front: bool = False) -> str:
    """The base; returns its UUID (hex). `extra`: more JSON entries {path: value};
    `slots`: the three viewer files in fixed-size uncompressed slots
    (cloud/viewer_slots.py), as published bases carry them; `front_redirects`:
    (path, title, target) redirects in the title index, as a base's other
    names for its places ("Δήμος Παλλήνης" -> the Pallini page); `pages`: more
    front pages (path, title, text); `fulltext`: with libzim's full-text index,
    as published bases carry it; `fronts=False`: nothing flagged front, not
    even index.html, as cloud/swap_viewer_rust.py left the bases it swapped
    until 2026-10-29 (their own title index holds every page all the same);
    `front_pages`: pages (path, title, text) flagged front whatever `fronts`
    says; `all_front`: every entry flagged front, tiles and JSON too, as some
    older builds listed them."""
    with Creator(str(path)).config_indexing(fulltext, "eng") as c:
        c.set_mainpath("index.html")
        for k, v in (("Name", f"osm_en_{REGION}"), ("Title", "OSM - Testland"), ("Language", "eng"),
                     ("Description", description), ("Creator", "OpenStreetMap"), ("Publisher", "StreetZim"),
                     ("Date", "2026-10-13")):
            c.add_metadata(k, v)
        c.add_illustration(48, _png())
        if slots is None:
            c.add_item(_Item("index.html", "Testland", "text/html", "<html><body>map</body></html>", front=fronts))
        else:
            if str(ROOT) not in __import__("sys").path:
                __import__("sys").path.insert(0, str(ROOT))
            from cloud.viewer_slots import pad_to_slot
            for name, mime in (("index.html", "text/html"), ("places.html", "text/html"),
                               ("routing-worker.js", "application/javascript")):
                c.add_item(_Item(name, "Testland" if name == "index.html" else "", mime,
                                 pad_to_slot(name, slots[name]), front=fronts and name == "index.html", compress=False))
        cfg = {"name": "Testland", "bounds": [19.0, 9.0, 22.0, 12.0], **(cfg_extra or {})}
        if wiki:
            cfg["hasWikiArticles"] = True
        c.add_item(_Item("map-config.json", "", "application/json", json.dumps(cfg), front=all_front))
        for z in range(3):
            c.add_item(_Item(f"tiles/{z}/0/0.pbf", "", "application/x-protobuf", bytes([z]) * (100 + z), front=all_front))
        c.add_item(_Item("images/marker.webp", "", "image/webp", _webp(b"marker"), front=all_front))
        geo = {}
        if wiki:
            for i, (p, t) in enumerate(ARTICLES["en"]):
                c.add_item(_Item(f"wiki-article/{p}", t, "text/html", _page(t, 1), front=fronts))
                geo[p] = [10.0 + i / 10, 20.0 + i / 10, "place", f"Q{i + 1}", ""]
            c.add_redirection("wiki-article/Gamma", "Gamma", "wiki-article/Gamma_Lake", {Hint.FRONT_ARTICLE: all_front})
        for pp, pt, px in pages or []:
            c.add_item(_Item(pp, pt, "text/html", _page(pt, pp.count("/"), px), front=fronts))
        for pp, pt, px in front_pages or []:
            c.add_item(_Item(pp, pt, "text/html", _page(pt, pp.count("/"), px), front=True))
        for rp, rt, tgt in front_redirects or []:
            c.add_redirection(rp, rt, tgt, {Hint.FRONT_ARTICLE: fronts or all_front})
        c.add_item(_Item("wiki-geo-index.json", "", "application/json", json.dumps(geo), front=all_front))
        for p, v in (extra or {}).items():
            c.add_item(_Item(p, "", "application/json", json.dumps(v)))
    return read_uuid(path)


def write_pack(path: Path, lang: str, base_uuid: str, *, region: str = REGION,
               articles: list[tuple[str, str]] | None = None, title: str | None = None,
               bodies: dict[str, str] | None = None) -> str:
    """A Wikipedia pack under ~w/<lang>/ for the base with base_uuid; returns its UUID (hex).
    `bodies`: more text for some articles {path: text}."""
    arts = ARTICLES[lang] if articles is None else articles
    pre = f"~w/{lang}/"
    iso3 = {"de": "deu", "fr": "fra", "th": "tha", "en": "eng", "es": "spa", "rm": "roh"}[lang]
    with Creator(str(path)).config_indexing(False, iso3) as c:
        if arts:
            c.set_mainpath(f"{pre}wiki-article/{arts[0][0]}")     # as streetzim/wiki_pack.py does
        for k, v in (("Name", f"osm_{region}_wiki-{lang}"), ("Title", title or f"Testland Wikipedia ({lang})"),
                     ("Language", iso3), ("Description", f"Wikipedia ({lang}) articles about Testland"),
                     ("Creator", "Wikipedia"), ("Publisher", "StreetZim"), ("Date", "2026-10-13")):
            c.add_metadata(k, v)
        c.add_illustration(48, _png())
        geo = {}
        for i, (p, t) in enumerate(arts):
            depth = 3 + p.count("/")
            c.add_item(_Item(f"{pre}wiki-article/{p}", t, "text/html",
                             _page(t, depth, (bodies or {}).get(p, "") + f"<img src='{'../' * (depth - 2)}images/{i}.webp'>"),
                             front=True))
            c.add_item(_Item(f"{pre}images/{i}.webp", "", "image/webp", _webp(f"{lang}{i}".encode())))
            geo[p] = [10.0 + i / 10, 20.0 + i / 10, "place", f"Q{i + 1}", ""]
        if arts:
            c.add_redirection(f"{pre}wiki-article/Alias_{lang}", f"Alias {lang}", f"{pre}wiki-article/{arts[0][0]}",
                              {Hint.FRONT_ARTICLE: False})
        c.add_item(_Item(f"{pre}wiki-geo-index.json", "", "application/json", json.dumps(geo)))
        c.add_item(_Item(f"{pre}pack.json", "", "application/json",
                         json.dumps({"lang": lang, "base_uuid": base_uuid, "articles": len(arts)})))
    return read_uuid(path)


def read_uuid(path: Path | str) -> str:
    with open(path, "rb") as fh:
        fh.seek(8)
        return fh.read(16).hex()


def md5_of(path: Path | str) -> str:
    return hashlib.md5(Path(path).read_bytes(), usedforsecurity=False).hexdigest()


def rewrite_uuid(src: Path, dst: Path, new_uuid: bytes) -> None:
    """dst = src with another header UUID and a recomputed checksum (what a
    viewer swap produces, minus the viewer)."""
    data = bytearray(src.read_bytes())
    data[8:24] = new_uuid
    body = bytes(data[:-16])
    dst.write_bytes(body + hashlib.md5(body, usedforsecurity=False).digest())


def child_base(base: Path, dst: Path, *, sidecar: bool = True, **override: Any) -> str:
    """A 'swapped' copy of base (new UUID) with a parent sidecar naming base;
    `override` replaces sidecar fields. Returns the child's UUID (hex)."""
    new = uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"child:{dst.name}").bytes
    rewrite_uuid(base, dst, new)
    if sidecar:
        side = {"child_uuid": new.hex(), "child_md5": md5_of(dst), "child_file": dst.name,
                "parent_uuid": read_uuid(base), "parent_file": base.name, "parents": []}
        side.update(override)
        Path(str(dst) + ".parent.json").write_text(json.dumps(side))
    return new.hex()


def zimcheck_report(pack: Path | str) -> str:
    """What the official zimcheck -A prints for a clean pack: one dangling
    back-to-map link per article, nothing else."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.langpack.zimfmt import ZimReader
    z = ZimReader(str(pack))
    v1 = z.v1_listing()
    assert v1 is not None
    lines = [f"[INFO] Checking zim file {pack}", "[INFO] Zimcheck version is 3.8.0",
             "[INFO] Verifying ZIM-archive structure integrity...",
             "[INFO] Avoiding redundant checksum test (already performed by the integrity check).",
             "[INFO] Checking metadata...",
             "[INFO] Searching for Favicon...", "[INFO] Searching for main page...",
             "[INFO] Verifying Articles' content..."]
    for k in z.iter_u32_blob(*v1):
        p = z.dirent_at(k).path.decode()
        if "/wiki-article/" in p:
            lines += [f"[ERROR] Internal URL: Dangling link(s) in article '{p}':",
                      f"  - '{'../' * p.count('/')}index.html' (resolves to 'index.html')", ""]
    lines += ["[INFO] Searching for redundant articles...", "  Verifying Similar Articles for redundancies...",
              "[INFO] Checking for redirect loops...", "[INFO] Overall Test Status: Fail",
              "[INFO] Total time taken by zimcheck: <3 seconds."]
    return "\n".join(lines) + "\n"


def fake_zimcheck(dirpath: Path, name: str, report: str | None, rc: int = 1, version: str = "zim-tools 3.8.0") -> str:
    """An executable that answers --version with `version` and otherwise prints
    `report` (None: the clean report of whatever pack it is given) and exits rc."""
    import sys
    dirpath.mkdir(parents=True, exist_ok=True)
    script = dirpath / f"{name}.py"
    body = [f"#!{sys.executable}", "import sys",
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})",
            f"if '--version' in sys.argv: print({version!r}); sys.exit(0)"]
    if report is None:
        body += ["from lpzims import zimcheck_report", "sys.stdout.write(zimcheck_report(sys.argv[-1]))"]
    else:
        rp = dirpath / f"{name}.out"
        rp.write_text(report)
        body += [f"sys.stdout.write(open({str(rp)!r}).read())"]
    body += [f"sys.exit({rc})"]
    script.write_text("\n".join(body) + "\n")
    script.chmod(0o755)
    return str(script)


def official_zimcheck() -> str | None:
    """The official zimcheck, if this machine has one ($PACK_GATE_ZIMCHECK or PATH)."""
    import subprocess
    zc = os.environ.get("PACK_GATE_ZIMCHECK") or shutil.which("zimcheck")
    if not zc:
        return None
    try:
        out = subprocess.run([zc, "--version"], capture_output=True, text=True, timeout=60).stdout
    except OSError:
        return None
    return zc if out.startswith("zim-tools ") else None
