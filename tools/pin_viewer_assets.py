#!/usr/bin/env python3
"""Pin, check and prefetch the viewer's third-party files
(resources/viewer-assets.lock.json; docs/viewer-supply-chain.md).

    python tools/pin_viewer_assets.py --check              # offline; CI runs it
    python tools/pin_viewer_assets.py --maplibre 5.24.0    # vendor another MapLibre
    python tools/pin_viewer_assets.py --rtl-text 0.3.0     # vendor the RTL text plugin
    python tools/pin_viewer_assets.py --fonts              # re-pin the glyph ranges
    python tools/pin_viewer_assets.py --prefetch DIR       # fill a cache (Docker image)
    python tools/pin_viewer_assets.py --ocr                # re-pin the sign reader (--ocr)
    python tools/pin_viewer_assets.py --prefetch-ocr DIR   # its runtime and models, cached

--maplibre downloads the npm tarball, checks it against the sha512 the npm
registry publishes for that version, and writes dist/maplibre-gl.js,
dist/maplibre-gl.css and LICENSE.txt to resources/vendor/maplibre-gl/ with
their SHA-256s in the lock file. --rtl-text does the same for
@mapbox/mapbox-gl-rtl-text (dist/mapbox-gl-rtl-text.js and LICENSE.md, to
resources/vendor/mapbox-gl-rtl-text/). --fonts downloads every range of every
fontstack from the font CDN and records its SHA-256 (null for a range the
CDN answers 404), printing how many changed, and does the same for the
fallback Noto Sans ranges (``fonts.fallback``: the ranges holding the
Unicode blocks of its scripts, from a commit-pinned URL) and the hash of its
vendored licence. The settings (URLs, fontstacks, fallback scripts) come
from DEFAULT_FONTS and DEFAULT_FALLBACK below, so edit those and re-pin;
--keep-lock keeps the lock file's settings instead (and records that it
did; --check fails on settings that differ from the defaults otherwise,
and --fonts prints each setting it resets). A download that is not
a glyph range (an HTML error page) stops it. Review the diff and commit both. --prefetch downloads every pinned range, checks it, and stores it in
DIR as the builder's cache (streetzim/viewer_assets.py) expects, so builds
that use DIR need no network for fonts.

--ocr pins what the builder's --ocr writes into a ZIM (docs/ocr.md):
the engine (resources/vendor/ocr-engine/ocr-engine.js: ONNX Runtime Web's
WebAssembly build and the paddleocr inference runtime, from their npm
tarballs checked against the registry's sha512, bundled by a pinned
esbuild into one classic script; it needs node and npx), the WebAssembly
file it runs (pinned by URL and SHA-256, fetched at build time, too big to
vendor), and the PaddleOCR models named in OCR_MODELS: each model's ONNX
file pinned by URL at a Hugging Face commit and SHA-256 (fetched at build
time) and its character dictionary vendored in resources/vendor/ocr-models/
(it needs PyYAML to read the model's inference.yml). --prefetch-ocr fills a
cache with the runtime and every model, like --prefetch.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import viewer_assets as va  # noqa: E402
from streetzim.glyph_fallback import is_glyph_pbf  # noqa: E402

NPM_REGISTRY = "https://registry.npmjs.org"
# lock-file key -> (npm package, licence, {tarball member: vendored file name})
NPM_PACKAGES: dict[str, tuple[str, str, dict[str, str]]] = {
    "maplibre-gl": ("maplibre-gl", "BSD-3-Clause (LICENSE.txt)", {
        "package/dist/maplibre-gl.js": "maplibre-gl.js",
        "package/dist/maplibre-gl.css": "maplibre-gl.css",
        "package/LICENSE.txt": "LICENSE.txt",
    }),
    va.RTL_TEXT_PLUGIN: ("@mapbox/mapbox-gl-rtl-text",
                         "BSD-2-Clause, ICU under the Unicode licence (LICENSE.md)", {
        "package/dist/mapbox-gl-rtl-text.js": "mapbox-gl-rtl-text.js",
        "package/LICENSE.md": "LICENSE.md",
    }),
}
DEFAULT_FONTS: dict[str, Any] = {
    "base_url": "https://fonts.openmaptiles.org",
    "licence": "Open Sans, as served by openmaptiles/fonts; see "
               "https://github.com/openmaptiles/fonts",
    # our name (style + ZIM path, no spaces) -> CDN fontstack name
    "fontstacks": {
        "OpenSansRegular": "Open Sans Regular",
        "OpenSansBold": "Open Sans Bold",
        "OpenSansItalic": "Open Sans Italic",
    },
}
RANGES = [f"{s}-{s + 255}" for s in range(0, 65536, 256)]
# Noto Sans glyphs for the scripts Open Sans has none for, merged into the
# Open Sans ranges at build time (streetzim/glyph_fallback.py). The URL names
# a git commit, so its files cannot change under the pins.
FALLBACK_COMMIT = "028c18f713baecad011301ff7a69acc39bcc2ae7"
DEFAULT_FALLBACK: dict[str, Any] = {
    "base_url": ("https://raw.githubusercontent.com/protomaps/basemaps-assets/"
                 f"{FALLBACK_COMMIT}/fonts"),
    "licence": "Noto Sans, SIL Open Font License 1.1 (Copyright 2022 The Noto Project "
               "Authors); glyphs made by maplibre/font-maker, as published by "
               "https://github.com/protomaps/basemaps-assets",
    "licence_file": "resources/vendor/noto-sans/OFL.txt",
    # our name -> the source's fontstack name
    "fontstacks": {
        "NotoSansRegular": "Noto Sans Regular",
        "NotoSansMedium": "Noto Sans Medium",
    },
    # which fallback each Open Sans stack gets (Noto Sans Italic has none of
    # these scripts; Medium is the source's boldest weight)
    "for": {
        "OpenSansRegular": "NotoSansRegular",
        "OpenSansBold": "NotoSansMedium",
        "OpenSansItalic": "NotoSansRegular",
    },
    # script -> Unicode blocks merged when the map's labels use it. Scripts
    # that need shaping MapLibre does not do (Devanagari, Bengali, ...) are
    # left out: their glyphs would render in the wrong order and shape.
    "scripts": {
        "Arabic": ["0600-06FF", "FB50-FBFF", "FE70-FEFF"],
        # with the small ligatures (U+FB13-FB17, e.g. U+FB13 men-now)
        "Armenian": ["0530-058F", "FB13-FB17"],
        # Mkhedruli, Mtavruli (what text-transform uppercase makes of
        # Mkhedruli: suburb/state/country labels) and Nuskhuri
        "Georgian": ["10A0-10FF", "1C90-1CBF", "2D00-2D2F"],
        "Hebrew": ["0590-05FF", "FB1D-FB4F"],
        "Lao": ["0E80-0EFF"],
        "Thai": ["0E00-0E7F"],
    },
}


# The sign reader (--ocr, docs/ocr.md). npm packages bundled into
# ocr-engine.js, and the esbuild that bundles them.
OCR_NPM = {"onnxruntime-web": "1.31.1", "paddleocr": "1.2.0"}
OCR_ESBUILD = "0.28.2"
# The entry esbuild bundles: the names the worker (resources/viewer/ocr-worker.js) uses.
OCR_ENGINE_ENTRY = """\
import * as ort from 'onnxruntime-web/wasm';
import { DetectionService, RecognitionService, getModelPresetOptions,
  normalizeInputToRgb } from 'paddleocr';
self.SZOCR = { ort, DetectionService, RecognitionService, getModelPresetOptions,
  normalizeInputToRgb };
"""
OCR_WASM = "ort-wasm-simd-threaded.wasm"
OCR_LICENCES = {
    "LICENSE-onnxruntime.txt":
        "https://raw.githubusercontent.com/microsoft/onnxruntime/v{onnxruntime-web}/LICENSE",
}
# paddleocr ships no licence file; its package.json and README say MIT.
PADDLEOCR_JS_LICENCE = """\
paddleocr (npm) {version}, https://github.com/x3zvawq/paddleocr.js
Licensed under the MIT License (package.json "license": "MIT"; the package
ships no licence file).

MIT License

Copyright (c) x3zvawq

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
OCR_MODELS_LICENCE = "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/LICENSE"
HF = "https://huggingface.co"
# model key -> Hugging Face repository (its inference.onnx and inference.yml),
# what it is, and, for a recognizer, the scripts (ISO 15924) it reads. One
# detector; a map gets the recognizers of the scripts its names use
# (streetzim/ocr.py). Chosen by measurement: docs/ocr.md.
OCR_MODELS: dict[str, dict[str, Any]] = {
    # PP-OCRv6 tiny with v5's input size (the longest side at most 960 px,
    # never enlarged): as accurate as v5's detector or more, in a third of
    # the bytes, faster and lighter than v6's own 736 px-minimum input.
    "det": {"repo": "PaddlePaddle/PP-OCRv6_tiny_det_onnx", "kind": "det",
            "preset": "PP-OCRv6_tiny", "options": {"maxSideLength": 960, "limitType": "max"}},
    "latin-cjk": {"repo": "PaddlePaddle/PP-OCRv6_small_rec_onnx", "kind": "rec",
                  "scripts": ["Latn", "Hani", "Hira", "Kana"]},
    "korean": {"repo": "PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
               "scripts": ["Hang"]},
    "thai": {"repo": "PaddlePaddle/th_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
             "scripts": ["Thai"]},
    "arabic": {"repo": "PaddlePaddle/arabic_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
               "scripts": ["Arab"], "reverse": True},
    "cyrillic": {"repo": "PaddlePaddle/cyrillic_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
                 "scripts": ["Cyrl"]},
    "greek": {"repo": "PaddlePaddle/el_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
              "scripts": ["Grek"]},
    "devanagari": {"repo": "PaddlePaddle/devanagari_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
                   "scripts": ["Deva"]},
    "tamil": {"repo": "PaddlePaddle/ta_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
              "scripts": ["Taml"]},
    "telugu": {"repo": "PaddlePaddle/te_PP-OCRv5_mobile_rec_onnx", "kind": "rec",
               "scripts": ["Telu"]},
}


def _npm_tarball(package: str, version: str) -> tuple[bytes, dict[str, str]]:
    meta = json.loads(va.fetch(f"{NPM_REGISTRY}/{package}/{version}"))
    tgz = va.fetch(meta["dist"]["tarball"])
    check_npm_integrity(tgz, meta["dist"]["integrity"])
    return tgz, {"version": version, "tarball": meta["dist"]["tarball"],
                 "integrity": meta["dist"]["integrity"]}


def pin_ocr_engine() -> None:
    """Bundle ocr-engine.js from the pinned npm tarballs and pin the
    WebAssembly file it loads."""
    out_dir = va.VENDOR / va.OCR_ENGINE
    out_dir.mkdir(parents=True, exist_ok=True)
    sources: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wasm = b""
        for package, version in OCR_NPM.items():
            tgz, sources[package] = _npm_tarball(package, version)
            dest = root / "node_modules" / package
            with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
                for m in tar.getmembers():
                    if not m.isfile() or not m.name.startswith("package/"):
                        continue
                    f = tar.extractfile(m)
                    if f is None:
                        continue
                    target = dest / m.name[len("package/"):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = f.read()
                    target.write_bytes(data)
                    if package == "onnxruntime-web" and m.name == f"package/dist/{OCR_WASM}":
                        wasm = data
        if not wasm:
            raise SystemExit(f"{OCR_WASM} not in onnxruntime-web {OCR_NPM['onnxruntime-web']}")
        (root / "entry.js").write_text(OCR_ENGINE_ENTRY, encoding="utf-8")
        subprocess.run(["npx", "--yes", f"esbuild@{OCR_ESBUILD}", "entry.js", "--bundle",
                        "--format=iife", "--minify", "--legal-comments=inline",
                        "--log-level=warning", "--outfile=ocr-engine.js"],
                       cwd=root, check=True)
        bundle = (root / "ocr-engine.js").read_bytes()
    files: dict[str, str] = {}

    def write(fname: str, data: bytes) -> None:
        (out_dir / fname).write_bytes(data)
        files[fname] = va.sha256_hex(data)
        print(f"  {fname}: {len(data):,} bytes, sha256 {files[fname]}")

    write(va.OCR_ENGINE_FILE, bundle)
    for fname, url in OCR_LICENCES.items():
        write(fname, va.fetch(url.replace("{onnxruntime-web}", OCR_NPM["onnxruntime-web"])))
    write("LICENSE-paddleocr.txt",
          PADDLEOCR_JS_LICENCE.format(version=OCR_NPM["paddleocr"]).encode())
    # The builder fetches the WebAssembly from the CDN's copy of the same
    # npm file; pinned here to the tarball's bytes.
    wasm_url = (f"https://cdn.jsdelivr.net/npm/onnxruntime-web@{OCR_NPM['onnxruntime-web']}"
                f"/dist/{OCR_WASM}")
    digest = va.sha256_hex(wasm)
    if va.sha256_hex(va.fetch(wasm_url)) != digest:
        raise SystemExit(f"{wasm_url} differs from the npm tarball's {OCR_WASM}")
    lock = read_lock_or_empty()
    lock[va.OCR_ENGINE] = {
        "version": " + ".join(f"{p} {v}" for p, v in OCR_NPM.items()),
        "licence": "MIT (ONNX Runtime: LICENSE-onnxruntime.txt; paddleocr: "
                   "LICENSE-paddleocr.txt)",
        "sources": sources,
        "esbuild": OCR_ESBUILD,
        "files": files,
        "wasm": {"file": OCR_WASM, "url": wasm_url, "sha256": digest, "bytes": len(wasm)},
    }
    write_lock(lock)
    print(f"bundled {va.OCR_ENGINE_FILE} ({lock[va.OCR_ENGINE]['version']}) "
          f"in {out_dir.relative_to(ROOT)}")


def _model_dictionary(yml: bytes) -> list[str]:
    try:
        import yaml  # pyright: ignore[reportMissingModuleSource]
    except ImportError:
        raise SystemExit("--ocr needs PyYAML to read the models' inference.yml "
                         "(pip install pyyaml)") from None
    # BaseLoader: every scalar a string ("1" stays "1", "no" stays "no").
    cfg = yaml.load(yml.decode("utf-8"), Loader=yaml.BaseLoader)
    chars: Any = cfg.get("PostProcess", {}).get("character_dict")
    if not isinstance(chars, list) or not chars:
        raise SystemExit("inference.yml has no usable PostProcess.character_dict")
    out = [str(c) for c in cast(list[Any], chars)]
    if any("\n" in c for c in out):
        raise SystemExit("inference.yml has a dictionary entry with a newline")
    return out


def pin_ocr_models() -> None:
    """Pin each model of OCR_MODELS at its repository's current commit."""
    out_dir = va.VENDOR / va.OCR_MODELS
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    models: dict[str, dict[str, Any]] = {}
    for key, spec in OCR_MODELS.items():
        info = json.loads(va.fetch(f"{HF}/api/models/{spec['repo']}"))
        revision = info["sha"]
        url = f"{HF}/{spec['repo']}/resolve/{revision}/inference.onnx"
        data = va.fetch(url, timeout=300)
        entry: dict[str, Any] = dict(spec, revision=revision, url=url,
                                     sha256=va.sha256_hex(data), bytes=len(data))
        if spec["kind"] == "rec":
            yml = va.fetch(f"{HF}/{spec['repo']}/resolve/{revision}/inference.yml")
            text = "\n".join(_model_dictionary(yml)) + "\n"
            fname = f"{key}.txt"
            (out_dir / fname).write_text(text, encoding="utf-8")
            files[fname] = va.sha256_hex(text.encode("utf-8"))
            entry["dict"] = fname
        models[key] = entry
        print(f"  {key}: {spec['repo']}@{revision[:12]}, {len(data):,} bytes")
    licence = va.fetch(OCR_MODELS_LICENCE)
    (out_dir / "LICENSE.txt").write_bytes(licence)
    files["LICENSE.txt"] = va.sha256_hex(licence)
    lock = read_lock_or_empty()
    lock[va.OCR_MODELS] = {
        "licence": "Apache-2.0 (LICENSE.txt): PaddleOCR models by PaddlePaddle",
        "models": models,
        "files": files,
    }
    write_lock(lock)
    print(f"pinned {len(models)} OCR models; dictionaries in {out_dir.relative_to(ROOT)}")


def check_ocr(lock: dict[str, Any]) -> str:
    """The sign reader's vendored files against the lock; '' when not pinned."""
    if va.OCR_ENGINE not in lock and va.OCR_MODELS not in lock:
        return ""
    va.vendored(va.OCR_ENGINE, lock)
    va.vendored(va.OCR_MODELS, lock)
    models = lock[va.OCR_MODELS]["models"]
    if sorted(models) != sorted(OCR_MODELS):
        raise SystemExit("lock file: the OCR models differ from OCR_MODELS "
                         "(run tools/pin_viewer_assets.py --ocr)")
    for key, m in models.items():
        for k, v in OCR_MODELS[key].items():
            if m.get(k) != v:
                raise SystemExit(f"lock file: OCR model {key}: {k} is {m.get(k)!r}, "
                                 f"OCR_MODELS says {v!r} (run --ocr)")
        if m["kind"] == "rec" and m.get("dict") not in lock[va.OCR_MODELS]["files"]:
            raise SystemExit(f"lock file: OCR model {key} has no vendored dictionary")
    return f"; the sign reader's engine and {len(models)} models"


def prefetch_ocr(dest: Path) -> None:
    lock = va.load_lock()
    wanted = [(f["url"], f["sha256"]) for f in [lock[va.OCR_ENGINE]["wasm"]]
              + list(lock[va.OCR_MODELS]["models"].values())]
    for url, digest in wanted:
        path = va.cache_path(dest, digest)
        if path.is_file() and va.sha256_hex(path.read_bytes()) == digest:
            continue
        data = va.fetch(url, timeout=300)
        if va.sha256_hex(data) != digest:
            raise SystemExit(f"prefetch failed: {url}: sha256 {va.sha256_hex(data)}, "
                             f"lock file says {digest}")
        va.store(digest, data, root=dest)
    print(f"ok: the OCR runtime and {len(wanted) - 1} models in {dest}")


def fallback_ranges(fb: dict[str, Any]) -> list[str]:
    from streetzim.glyph_fallback import parse_blocks, ranges_for
    return ranges_for([b for blocks in fb["scripts"].values() for b in parse_blocks(blocks)])


def write_lock(lock: dict[str, Any], path: Path = va.LOCK) -> None:
    lock["_comment"] = ("Written by tools/pin_viewer_assets.py; see "
                        "docs/viewer-supply-chain.md. Do not edit by hand.")
    ordered = {"_comment": lock["_comment"]}
    ordered.update((k, v) for k, v in lock.items() if k != "_comment")
    path.write_text(json.dumps(ordered, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def read_lock_or_empty() -> dict[str, Any]:
    return va.load_lock() if va.LOCK.exists() else {}


def check_npm_integrity(data: bytes, integrity: str) -> None:
    algo, _, b64 = integrity.partition("-")
    if algo != "sha512":
        raise SystemExit(f"unexpected npm integrity algorithm: {integrity}")
    got = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if got != b64:
        raise SystemExit(f"npm tarball sha512 {got} != registry's {b64}")


def pin_npm(name: str, version: str) -> None:
    """Vendor package ``name`` (a key of NPM_PACKAGES) at ``version`` from
    its npm tarball, checked against the registry's sha512."""
    package, licence, members = NPM_PACKAGES[name]
    meta = json.loads(va.fetch(f"{NPM_REGISTRY}/{package}/{version}"))
    tarball_url: str = meta["dist"]["tarball"]
    integrity: str = meta["dist"]["integrity"]
    tgz = va.fetch(tarball_url)
    check_npm_integrity(tgz, integrity)
    out_dir = va.VENDOR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        for member, fname in members.items():
            f = tar.extractfile(member)
            if f is None:
                raise SystemExit(f"{member} not in {tarball_url}")
            data = f.read()
            (out_dir / fname).write_bytes(data)
            files[fname] = va.sha256_hex(data)
            print(f"  {fname}: {len(data):,} bytes, sha256 {files[fname]}")
    lock = read_lock_or_empty()
    lock[name] = {
        "version": version,
        "licence": licence,
        "tarball": tarball_url,
        "integrity": integrity,
        "files": files,
    }
    write_lock(lock)
    print(f"vendored {package} {version} in {out_dir.relative_to(ROOT)}")


def pin_maplibre(version: str) -> None:
    pin_npm("maplibre-gl", version)
    print("run scripts/sync-drive-viewer.sh for the website copy")


def _pin_ranges(base_url: str, fontstacks: dict[str, str], ranges: list[str],
                old: dict[str, Any]) -> tuple[dict[str, dict[str, str | None]], int, int]:
    """Download each range of each stack: ({stack: {range: sha256 or None}},
    how many differ from ``old``, how many are absent)."""
    tasks = [(stack, src, r) for stack, src in fontstacks.items() for r in ranges]

    def one(task: tuple[str, str, str]) -> tuple[str, str, str | None]:
        stack, src, r = task
        url = f"{base_url.rstrip('/')}/{urllib.parse.quote(src)}/{r}.pbf"
        try:
            data = va.fetch(url)
        except va.DownloadError as e:
            if str(e).endswith("HTTP 404"):
                return stack, r, None
            raise SystemExit(f"not pinning fonts: {e}") from None
        if not is_glyph_pbf(data):
            # e.g. the font CDN answers an unknown fontstack with an HTML page
            raise SystemExit(f"not pinning fonts: {url} is not a glyph range "
                             f"({len(data)} bytes starting {data[:16]!r})")
        return stack, r, va.sha256_hex(data)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, tasks))
    out: dict[str, dict[str, str | None]] = {}
    changed = 0
    for stack, r, digest in results:
        out.setdefault(stack, {})[r] = digest
        if old.get(stack, {}).get(r, "?") != digest:
            changed += 1
    return out, changed, sum(1 for *_, d in results if d is None)


def settings_drift(lock: dict[str, Any]) -> list[str]:
    """Each font setting of ``lock`` that differs from this tool's defaults,
    as "fonts.fallback.scripts: <lock> -> <default>"."""
    fonts = lock.get("fonts", {})
    out: list[str] = []
    for prefix, defaults, current in (("fonts", DEFAULT_FONTS, fonts),
                                      ("fonts.fallback", DEFAULT_FALLBACK, fonts.get("fallback", {}))):
        for k, v in defaults.items():
            if current.get(k) != v:
                out.append(f"{prefix}.{k}: {json.dumps(current.get(k), ensure_ascii=False)}"
                           f" -> {json.dumps(v, ensure_ascii=False)}")
    return out


def pin_fonts(keep_lock: bool = False) -> None:
    """Re-pin every range. The settings (URLs, fontstacks, scripts, ...) are
    DEFAULT_FONTS and DEFAULT_FALLBACK, so editing those and re-pinning
    takes effect; ``keep_lock`` keeps the lock file's settings instead
    (e.g. a base_url changed there by hand) and only refreshes the hashes."""
    lock = read_lock_or_empty()
    old = lock.get("fonts", {})
    if not keep_lock:
        for line in settings_drift(lock) if lock else []:
            print(f"setting reset to the tool's default: {line}")

    def settings(defaults: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        return {k: (current.get(k, v) if keep_lock else v) for k, v in defaults.items()}

    fonts: dict[str, Any] = settings(DEFAULT_FONTS, old)
    fonts["ranges"], changed, empty = _pin_ranges(
        fonts["base_url"], fonts["fontstacks"], RANGES, old.get("ranges", {}))
    n = len(fonts["fontstacks"]) * len(RANGES)
    print(f"pinned {n} glyph ranges ({empty} absent on the CDN); "
          f"{changed} differ from the previous lock file")

    old_fb = old.get("fallback", {})
    fb: dict[str, Any] = settings(DEFAULT_FALLBACK, old_fb)
    fb_ranges = fallback_ranges(fb)
    fb["ranges"], changed, empty = _pin_ranges(
        fb["base_url"], fb["fontstacks"], fb_ranges, old_fb.get("ranges", {}))
    fb["licence_sha256"] = va.sha256_hex((ROOT / fb["licence_file"]).read_bytes())
    fonts["fallback"] = fb
    if keep_lock:
        # --check accepts settings that differ from the defaults only then
        fonts["kept_lock_settings"] = True
    lock["fonts"] = fonts
    write_lock(lock)
    print(f"pinned {len(fb['fontstacks']) * len(fb_ranges)} fallback glyph ranges "
          f"({empty} absent); {changed} differ from the previous lock file")


def all_ranges(lock: dict[str, Any] | None = None) -> list[va.FontRange]:
    """The pinned glyph ranges and the fallback ranges."""
    fb = va.font_fallback(lock)
    return va.font_ranges(lock) + (fb.ranges if fb else [])


def prefetch(dest: Path) -> None:
    ranges = [fr for fr in all_ranges() if fr.sha256]

    def one(fr: va.FontRange) -> None:
        assert fr.sha256
        path = va.cache_path(dest, fr.sha256)
        if path.is_file() and va.sha256_hex(path.read_bytes()) == fr.sha256:
            return
        data = va.fetch(fr.url)
        got = va.sha256_hex(data)
        if got != fr.sha256:
            raise va.IntegrityError(f"{fr.url}: sha256 {got}, lock file says {fr.sha256}")
        va.store(fr.sha256, data, root=dest)
        if not path.is_file():
            raise SystemExit(f"could not write {path}")

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(one, ranges))
    except (va.IntegrityError, va.DownloadError) as e:
        raise SystemExit(f"prefetch failed: {e}") from None
    print(f"ok: {len(ranges)} verified glyph ranges in {dest}")


def check() -> None:
    lock = va.load_lock()
    drift = settings_drift(lock)
    if drift and not lock["fonts"].get("kept_lock_settings"):
        raise SystemExit("lock file: font settings differ from tools/pin_viewer_assets.py's "
                         "defaults (edit DEFAULT_FONTS/DEFAULT_FALLBACK and run --fonts, or "
                         "pin with --fonts --keep-lock to keep them):\n  " + "\n  ".join(drift))
    for name in NPM_PACKAGES:
        va.vendored(name, lock)
    ranges = va.font_ranges(lock)
    stacks = lock["fonts"]["fontstacks"]
    for stack in stacks:
        if list(lock["fonts"]["ranges"].get(stack, {})) != RANGES:
            raise SystemExit(f"lock file: fontstack {stack} does not list all {len(RANGES)} ranges")
    fb = lock["fonts"].get("fallback")
    if fb:
        want = fallback_ranges(fb)
        for stack in fb["fontstacks"]:
            if list(fb["ranges"].get(stack, {})) != want:
                raise SystemExit(f"lock file: fallback fontstack {stack} does not list "
                                 f"the ranges of its scripts ({want})")
        for stack, fb_stack in fb["for"].items():
            if stack not in stacks or fb_stack not in fb["fontstacks"]:
                raise SystemExit(f"lock file: fallback {stack} -> {fb_stack} names an unknown fontstack")
        va.fallback_licence(lock)
    fb_count = len(all_ranges(lock)) - len(ranges)
    ranges = all_ranges(lock)
    bad = [fr for fr in ranges if fr.sha256 is not None and
           (len(fr.sha256) != 64 or any(c not in "0123456789abcdef" for c in fr.sha256))]
    if bad:
        raise SystemExit(f"lock file: malformed sha256 for {bad[0].stack}/{bad[0].range_key}")
    ocr = check_ocr(lock)
    print(f"ok: maplibre-gl {lock['maplibre-gl']['version']} and "
          f"{va.RTL_TEXT_PLUGIN} {lock[va.RTL_TEXT_PLUGIN]['version']} match the lock file; "
          f"{len(ranges) - fb_count} glyph ranges and {fb_count} fallback ranges pinned{ocr}")


def main() -> int:
    ap = argparse.ArgumentParser(description=next(iter((__doc__ or "").splitlines()), ""))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="verify vendored files (offline)")
    g.add_argument("--maplibre", metavar="VERSION", help="vendor this MapLibre GL JS version")
    g.add_argument("--rtl-text", metavar="VERSION",
                   help="vendor this @mapbox/mapbox-gl-rtl-text version")
    g.add_argument("--fonts", action="store_true", help="re-pin every glyph range from the CDN")
    g.add_argument("--prefetch", metavar="DIR", type=Path,
                   help="download the pinned glyph ranges into this cache directory")
    g.add_argument("--ocr", action="store_true",
                   help="re-pin the sign reader: bundle its engine, pin its models")
    g.add_argument("--prefetch-ocr", metavar="DIR", type=Path,
                   help="download the sign reader's runtime and models into this cache directory")
    ap.add_argument("--keep-lock", action="store_true",
                    help="with --fonts: keep the lock file's font settings (URLs, "
                         "fontstacks, scripts) instead of this tool's defaults")
    args = ap.parse_args()
    if args.keep_lock and not args.fonts:
        ap.error("--keep-lock only goes with --fonts")
    try:
        if args.check:
            check()
        elif args.maplibre:
            pin_maplibre(args.maplibre)
        elif args.rtl_text:
            pin_npm(va.RTL_TEXT_PLUGIN, args.rtl_text)
        elif args.fonts:
            pin_fonts(keep_lock=args.keep_lock)
        elif args.ocr:
            pin_ocr_engine()
            pin_ocr_models()
        elif args.prefetch_ocr:
            prefetch_ocr(args.prefetch_ocr)
        else:
            prefetch(args.prefetch)
    except va.IntegrityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
