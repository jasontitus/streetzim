"""The wheel's contents (pyproject.toml) against what the build reads at run time.

CI's wheel job builds and installs the wheel and runs
tools/check_wheel_install.py; these catch the same drift without a build.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streetzim import paths  # noqa: E402

tomllib = pytest.importorskip("tomllib")


def _setuptools() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["tool"]["setuptools"]


def test_package_data_is_exactly_the_runtime_files():
    st = _setuptools()
    assert st["package-dir"] == {"streetzim.resources": "resources"}
    shipped: set[str] = set()
    for pkg, globs in st["package-data"].items():
        if not pkg.startswith("streetzim.resources"):
            continue
        sub = pkg[len("streetzim.resources"):].lstrip(".").replace(".", "/")
        base = ROOT / "resources" / sub
        for pattern in globs:
            shipped |= {p.relative_to(ROOT / "resources").as_posix()
                        for p in base.glob(pattern) if p.is_file()}
    assert shipped == set(paths.RUNTIME_FILES)


def test_every_streetzim_package_is_listed():
    listed = set(_setuptools()["packages"])
    on_disk = {".".join(p.parent.relative_to(ROOT).parts)
               for p in (ROOT / "streetzim").rglob("__init__.py")}
    assert on_disk <= listed, on_disk - listed


def test_checkout_uses_its_own_resources():
    assert paths.RESOURCES_DIR == ROOT / "resources"
    assert paths.missing_runtime_files() == []


def test_missing_runtime_files_names_what_is_absent(tmp_path):
    (tmp_path / "viewer").mkdir()
    (tmp_path / "viewer" / "index.html").write_text("x")
    missing = paths.missing_runtime_files(tmp_path)
    assert "viewer/index.html" not in missing
    assert len(missing) == len(paths.RUNTIME_FILES) - 1


def test_cli_refuses_to_start_without_its_files(monkeypatch, capsys):
    from streetzim import cli
    monkeypatch.setattr(cli, "missing_runtime_files", lambda: ["viewer/index.html"])
    rc = cli.main(["--name", "x", "--title", "X", "--description", "d", "--area", "monaco"])
    assert rc == 2
    assert "viewer/index.html" in capsys.readouterr().err


def test_shapefile_script_wrapper_runs_the_packaged_copy():
    wrapper = (ROOT / "scripts" / "fetch-shapefiles.sh").read_text()
    assert "resources/tilemaker/fetch-shapefiles.sh" in wrapper
    assert (ROOT / "resources" / "tilemaker" / "fetch-shapefiles.sh").is_file()
