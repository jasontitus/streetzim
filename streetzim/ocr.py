"""The sign reader (--ocr): photograph a street sign, a shop front or a
landmark's name and search the map for it, offline (docs/ocr.md).

The viewer's camera button (305-ocr-search.js) runs PaddleOCR models with
ONNX Runtime Web in a worker (resources/viewer/ocr-worker.js). This module
picks the models a map needs and writes them into the ZIM:

- one text detector, whatever the script;
- a recognizer for each script the map's own names are written in
  (Latin and Chinese/Japanese share one): `choose_models` counts the
  scripts of the search records' native names (`name_native`, else `name`).
  A script without a recognizer (Hebrew, Georgian, Khmer, ...) is logged;
  a sign's Latin line is still read.

Everything is pinned in resources/viewer-assets.lock.json
(tools/pin_viewer_assets.py --ocr): the engine and the models'
dictionaries are vendored, the WebAssembly runtime and the models are
fetched at build time by URL and SHA-256 and cached
(streetzim/viewer_assets.py). The ZIM gets them under ocr/, with
ocr/manifest.json naming them, and map-config.json's `ocr` key pointing at
the manifest. A ZIM without the key (every older one: in-place viewer
patching cannot add files) shows no camera button.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from streetzim import viewer_assets as va
from streetzim.common import VIEWER_DIR, print

ENTRY_DIR = "ocr"
MANIFEST_ENTRY = f"{ENTRY_DIR}/manifest.json"
WORKER_ENTRY = f"{ENTRY_DIR}/ocr-worker.js"
LICENCES_ENTRY = f"{ENTRY_DIR}/LICENSES.txt"
MANIFEST_VERSION = 1

# ISO 15924 script -> its letters' Unicode ranges (inclusive). Recognizers
# exist for the first group; the rest are only counted, to say which
# scripts a map's signs will not be read in.
SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "Latn": ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F), (0x1E00, 0x1EFF)),
    "Hani": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F)),
    "Hira": ((0x3040, 0x309F),),
    "Kana": ((0x30A0, 0x30FF), (0x31F0, 0x31FF)),
    "Hang": ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)),
    "Thai": ((0x0E00, 0x0E7F),),
    "Arab": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF),
             (0xFE70, 0xFEFF)),
    "Cyrl": ((0x0400, 0x052F), (0x1C80, 0x1C8F), (0xA640, 0xA69F)),
    "Grek": ((0x0370, 0x03FF), (0x1F00, 0x1FFF)),
    "Deva": ((0x0900, 0x097F), (0xA8E0, 0xA8FF)),
    "Taml": ((0x0B80, 0x0BFF),),
    "Telu": ((0x0C00, 0x0C7F),),
    "Hebr": ((0x0590, 0x05FF), (0xFB1D, 0xFB4F)),
    "Geor": ((0x10A0, 0x10FF), (0x1C90, 0x1CBF), (0x2D00, 0x2D2F)),
    "Armn": ((0x0530, 0x058F),),
    "Ethi": ((0x1200, 0x139F), (0x2D80, 0x2DDF)),
    "Beng": ((0x0980, 0x09FF),),
    "Guru": ((0x0A00, 0x0A7F),),
    "Gujr": ((0x0A80, 0x0AFF),),
    "Orya": ((0x0B00, 0x0B7F),),
    "Knda": ((0x0C80, 0x0CFF),),
    "Mlym": ((0x0D00, 0x0D7F),),
    "Sinh": ((0x0D80, 0x0DFF),),
    "Laoo": ((0x0E80, 0x0EFF),),
    "Tibt": ((0x0F00, 0x0FFF),),
    "Mymr": ((0x1000, 0x109F),),
    "Khmr": ((0x1780, 0x17FF),),
    "Thaa": ((0x0780, 0x07BF),),
    "Tfng": ((0x2D30, 0x2D7F),),
}
# A script gets its recognizer when at least this share of the named
# records sampled use it, or when it is the most used script. Every other
# recognizer also reads unaccented Latin, so Latin needs more beside
# another script: brand names in Bangkok do not add 21 MB to Thailand.
MIN_SHARE = 0.05
MIN_SHARE_LATIN = 0.25
SAMPLE = 200_000


def _script_table() -> list[tuple[int, int, str]]:
    return sorted((lo, hi, s) for s, ranges in SCRIPT_RANGES.items() for lo, hi in ranges)


_TABLE = _script_table()


def scripts_of(text: str) -> set[str]:
    """The scripts (of SCRIPT_RANGES) whose letters appear in ``text``."""
    out: set[str] = set()
    for ch in text:
        cp = ord(ch)
        if cp < 0x41:
            continue
        for lo, hi, script in _TABLE:
            if cp < lo:
                break
            if cp <= hi:
                out.add(script)
                break
    return out


def native_names(features: str | Iterable[dict[str, Any]] | None,
                 limit: int = SAMPLE) -> Iterator[str]:
    """The first ``limit`` records' own-script names (``name_native``, else
    ``name``), from a search-features JSONL file or a list of records."""
    if not features:
        return
    if isinstance(features, str):
        with open(features, encoding="utf-8") as f:
            for n, line in enumerate(f):
                if n >= limit:
                    return
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                name = rec.get("name_native") or rec.get("name")
                if isinstance(name, str) and name:
                    yield name
        return
    for n, rec in enumerate(features):
        if n >= limit:
            return
        name = rec.get("name_native") or rec.get("name")
        if isinstance(name, str) and name:
            yield name


def count_scripts(names: Iterable[str]) -> tuple[Counter[str], int]:
    """How many names use each script, and how many names there were."""
    counts: Counter[str] = Counter()
    total = 0
    for name in names:
        total += 1
        counts.update(scripts_of(name))
    return counts, total


def ocr_lock(lock: dict[str, Any] | None = None) -> dict[str, Any]:
    """The lock file's OCR entries; IntegrityError when they are not pinned."""
    lock = lock or va.load_lock()
    if va.OCR_ENGINE not in lock or va.OCR_MODELS not in lock:
        raise va.IntegrityError("the lock file pins no OCR engine or models; "
                                "run tools/pin_viewer_assets.py --ocr")
    return lock


def recognizers_by_script(lock: dict[str, Any] | None = None) -> dict[str, str]:
    """Script -> the pinned recognizer reading it."""
    models = ocr_lock(lock)[va.OCR_MODELS]["models"]
    return {s: key for key, m in models.items() if m["kind"] == "rec" for s in m["scripts"]}


def detector(lock: dict[str, Any] | None = None) -> str:
    models = ocr_lock(lock)[va.OCR_MODELS]["models"]
    dets = [key for key, m in models.items() if m["kind"] == "det"]
    if len(dets) != 1:
        raise va.IntegrityError(f"the lock file pins {len(dets)} OCR detectors, not one")
    return dets[0]


@dataclass(frozen=True)
class Plan:
    """The models a ZIM gets: its detector and recognizers (lock keys), and
    the scripts its names use that no recognizer reads."""
    detector: str
    recognizers: tuple[str, ...]
    unread: tuple[str, ...] = ()


def check_scripts(scripts: Iterable[str], lock: dict[str, Any] | None = None) -> list[str]:
    """``scripts`` (--ocr-scripts), each one a recognizer reads; ValueError otherwise."""
    by_script = recognizers_by_script(lock)
    wanted = list(scripts)
    unknown = [s for s in wanted if s not in by_script]
    if not wanted or unknown:
        raise ValueError(f"--ocr-scripts: no recognizer for {', '.join(unknown) or 'nothing'} "
                         f"(known: {', '.join(sorted(by_script))})")
    return wanted


def choose_models(counts: Counter[str], total: int, *,
                  scripts: Iterable[str] | None = None,
                  lock: dict[str, Any] | None = None) -> Plan:
    """The recognizers for the scripts in ``counts`` (or ``scripts``, when
    given: --ocr-scripts): each script used by at least MIN_SHARE of the
    ``total`` names, and the most used one, mapped to its recognizer;
    Latin's when nothing else qualifies."""
    by_script = recognizers_by_script(lock)
    if scripts is not None:
        wanted = check_scripts(scripts, lock)
    else:
        ranked = [s for s, _ in counts.most_common()]
        wanted = [s for i, s in enumerate(ranked)
                  if i == 0 or (total and counts[s] / total
                                >= (MIN_SHARE_LATIN if s == "Latn" else MIN_SHARE))]
    recs: list[str] = []
    unread: list[str] = []
    for s in wanted:
        key = by_script.get(s)
        if key is None:
            unread.append(s)
        elif key not in recs:
            recs.append(key)
    if not recs:
        recs.append(by_script["Latn"])
    return Plan(detector(lock), tuple(recs), tuple(unread))


def plan_for(features: str | Iterable[dict[str, Any]] | None, *,
             scripts: Iterable[str] | None = None,
             lock: dict[str, Any] | None = None) -> Plan:
    """The models for a map whose search records are ``features``."""
    counts, total = count_scripts(native_names(features))
    plan = choose_models(counts, total, scripts=scripts, lock=lock)
    shares = ", ".join(f"{s} {100 * c / total:.0f}%" for s, c in counts.most_common(6)) if total else "none"
    print(f"  OCR: names' scripts: {shares}; recognizers: {', '.join(plan.recognizers)}")
    if plan.unread:
        print(f"  OCR: no recognizer for {', '.join(plan.unread)}; "
              f"signs in it are read only where they also carry Latin text")
    return plan


def entries(plan: Plan, lock: dict[str, Any] | None = None) -> list[tuple[str, str, str, bytes]]:
    """The ZIM entries (path, title, mimetype, content): the worker, the
    engine, the WebAssembly runtime, the models, their dictionaries, the
    licences and the manifest. Downloads what is not cached; raises
    IntegrityError / DownloadError."""
    lock = ocr_lock(lock)
    engine = va.vendored(va.OCR_ENGINE, lock)
    dicts = va.vendored(va.OCR_MODELS, lock)
    models = lock[va.OCR_MODELS]["models"]
    wasm = lock[va.OCR_ENGINE]["wasm"]
    out: list[tuple[str, str, str, bytes]] = [
        (WORKER_ENTRY, "Sign reader worker", "application/javascript",
         (VIEWER_DIR / "ocr-worker.js").read_bytes()),
        (f"{ENTRY_DIR}/{va.OCR_ENGINE_FILE}", "Sign reader engine", "application/javascript",
         engine[va.OCR_ENGINE_FILE].read_bytes()),
        (f"{ENTRY_DIR}/{wasm['file']}", "ONNX Runtime WebAssembly", "application/wasm",
         va.fetch_verified(wasm["url"], wasm["sha256"])),
    ]
    det = models[plan.detector]
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "worker": WORKER_ENTRY,
        "engine": f"{ENTRY_DIR}/{va.OCR_ENGINE_FILE}",
        "wasm": f"{ENTRY_DIR}/{wasm['file']}",
        "licences": LICENCES_ENTRY,
        "det": {"model": f"{ENTRY_DIR}/{plan.detector}.onnx", "preset": det["preset"],
                "options": det.get("options", {})},
        "rec": [],
    }
    out.append((manifest["det"]["model"], "Text detection model", "application/octet-stream",
                va.fetch_verified(det["url"], det["sha256"])))
    for key in plan.recognizers:
        m = models[key]
        rec = {"name": key, "scripts": m["scripts"], "model": f"{ENTRY_DIR}/{key}.onnx",
               "dict": f"{ENTRY_DIR}/{key}.txt", "reverse": bool(m.get("reverse"))}
        manifest["rec"].append(rec)
        out.append((rec["model"], f"Text recognition model ({key})", "application/octet-stream",
                    va.fetch_verified(m["url"], m["sha256"])))
        out.append((rec["dict"], f"Text recognition characters ({key})", "text/plain",
                    dicts[m["dict"]].read_bytes()))
    licences = [
        "The sign reader (docs/ocr.md in the StreetZim repository) ships:",
        f"- ONNX Runtime Web and paddleocr ({lock[va.OCR_ENGINE]['version']}), "
        f"bundled as {manifest['engine']}, with {manifest['wasm']};",
        "- PaddleOCR models by PaddlePaddle: " + ", ".join(
            f"{models[k]['repo']}@{models[k]['revision'][:12]}"
            for k in (plan.detector, *plan.recognizers)) + ".",
        "",
    ]
    texts = [engine["LICENSE-onnxruntime.txt"], engine["LICENSE-paddleocr.txt"], dicts["LICENSE.txt"]]
    body = "\n".join(licences) + "\n\n".join(
        f"==== {p.name} ====\n{p.read_text(encoding='utf-8')}" for p in texts)
    out.append((LICENCES_ENTRY, "Sign reader licences", "text/plain", body.encode("utf-8")))
    out.append((MANIFEST_ENTRY, "Sign reader manifest", "application/json",
                json.dumps(manifest, indent=1).encode("utf-8")))
    return out


def prefetch(plan: Plan | None = None, lock: dict[str, Any] | None = None) -> None:
    """Download (or find cached) the runtime and the detector -- and the
    recognizers of ``plan`` -- before the long phases of a build, so a
    missing network fails in seconds, not at the end."""
    lock = ocr_lock(lock)
    wasm = lock[va.OCR_ENGINE]["wasm"]
    models = lock[va.OCR_MODELS]["models"]
    keys = [detector(lock)] + (list(plan.recognizers) if plan else [])
    va.vendored(va.OCR_ENGINE, lock)
    va.vendored(va.OCR_MODELS, lock)
    va.fetch_verified(wasm["url"], wasm["sha256"])
    for key in keys:
        va.fetch_verified(models[key]["url"], models[key]["sha256"])
