"""Fixtures for the language-pack pipeline tests (tools/langpack/,
docs/language-pack-pipeline.md): a small base and three Wikipedia packs
(de, fr, th) written once per session; tests copy what they change."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "langpack"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def tool(name: str, *args: object, env: dict[str, str] | None = None,
         timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run tools/langpack/<name> (a .py with this Python, else directly)."""
    path = TOOLS / name
    cmd = ([sys.executable, str(path)] if name.endswith(".py") else [str(path)]) + [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})


@pytest.fixture(scope="session", autouse=True)
def _no_memory_pause() -> Iterator[None]:
    """ops/langpack/host-env.sh pauses pack builds and gates while
    MemAvailable is below LANGPACK_MIN_FREE_GB (40 by default, the build
    host's margin): on a machine with less memory a test would wait forever.
    A test of the pause sets its own value."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LANGPACK_MIN_FREE_GB", "0")
        yield


@pytest.fixture(scope="session")
def lp(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    pytest.importorskip("libzim")
    from tests.langpack import lpzims
    d = tmp_path_factory.mktemp("langpack")
    base = d / "osm-testland-2026-10-13.zim"
    base_uuid = lpzims.write_base(base)
    packs = {lang: d / f"osm_testland_wiki-{lang}_2026-10-13.zim" for lang in ("de", "fr", "th")}
    uuids = {lang: lpzims.write_pack(p, lang, base_uuid) for lang, p in packs.items()}
    return SimpleNamespace(dir=d, base=base, base_uuid=base_uuid, packs=packs, uuids=uuids)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh $LANGPACK_WORK for this test (verdicts, recipes, caches)."""
    w = tmp_path / "work"
    w.mkdir()
    monkeypatch.setenv("LANGPACK_WORK", str(w))
    return w
