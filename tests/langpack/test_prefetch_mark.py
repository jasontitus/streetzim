"""ops/langpack/prefetch_mark.py: a viewer-swapped base takes its parent's
prefetch mark (ship_packs.sh step 2): the same Wikipedia, in the cache
already. Not without a sidecar or a parent mark, for other pack languages,
from a mark left for an older parent file of the same name, or with a
sidecar that names another file (left beside a rebuilt one)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.langpack.conftest import ROOT

TOOL = ROOT / "ops" / "langpack" / "prefetch_mark.py"


def _id(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size} {st.st_mtime_ns // 10**9}.{st.st_mtime_ns % 10**9:09d}"


def _run(base: Path, root: Path, *more: str) -> int:
    return subprocess.run([sys.executable, str(TOOL), str(base), str(root), *more], capture_output=True, text=True,
                          timeout=60).returncode


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    maps, root = tmp_path / "maps", tmp_path / "packs"
    maps.mkdir()
    parent, child = maps / "osm-x-2026-10-23.zim", maps / "osm-x-2026-10-26.zim"
    parent.write_bytes(b"ZIM\x04" + b"\0" * 4 + bytes(range(16)) + b"parent")
    child.write_bytes(b"ZIM\x04" + b"\0" * 4 + bytes(range(16, 32)) + b"child, the viewer swapped")
    (root / parent.name[:-4]).mkdir(parents=True)
    (root / parent.name[:-4] / "prefetched").write_text(f"de,fr\n{_id(parent)}\n")
    Path(f"{child}.parent.json").write_text(json.dumps({"parent_file": parent.name, "child_uuid": bytes(range(16, 32)).hex()}))
    return parent, child, root


def test_takes_the_parents_mark(tmp_path: Path) -> None:
    parent, child, root = _setup(tmp_path)
    assert _run(child, root, "--langs", "de,fr") == 0
    assert (root / child.name[:-4] / "prefetched").read_text() == f"de,fr\n{_id(child)}\n"


def test_not_without_what_it_needs(tmp_path: Path) -> None:
    parent, child, root = _setup(tmp_path)
    mark = root / child.name[:-4] / "prefetched"
    assert _run(child, root, "--langs", "de,fr,th") == 1 and not mark.exists()            # other languages
    os.utime(parent, (1, 1))                                                               # the parent rewritten
    assert _run(child, root) == 1 and not mark.exists()
    parent.unlink()                                                                        # gone: its mark counts
    assert _run(child, root) == 0 and mark.exists()
    mark.unlink()
    (root / parent.name[:-4] / "prefetched").unlink()
    assert _run(child, root) == 1 and not mark.exists()                                    # no parent mark
    (root / parent.name[:-4] / "prefetched").write_text("de,fr\n1 1.000000000\n")
    side = json.loads(Path(f"{child}.parent.json").read_text())
    Path(f"{child}.parent.json").write_text(json.dumps({**side, "child_uuid": "ab" * 16}))
    assert _run(child, root) == 1 and not mark.exists()                                    # names another file
    Path(f"{child}.parent.json").write_text(json.dumps({**side, "child_uuid": side["child_uuid"].upper()}))
    assert _run(child, root) == 0 and mark.exists()                                        # case does not matter
    mark.unlink()
    Path(f"{child}.parent.json").unlink()
    assert _run(child, root) == 1                                                          # no sidecar
    Path(f"{child}.parent.json").write_text(json.dumps({**side, "parent_file": child.name}))
    assert _run(child, root) == 1                                                          # names itself
