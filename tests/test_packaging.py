"""The wheel's contents (pyproject.toml) against what the build reads at run time.

CI's wheel job builds and installs the wheel and runs
tools/check_wheel_install.py; these catch the same drift without a build.
"""
from __future__ import annotations

import re
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


def _cloud_imports(source: Path) -> set[str]:
    """cloud modules a file imports: `cloud.x`, `from cloud import x`, and
    (inside cloud/) sibling imports."""
    import ast
    found: set[str] = set()
    in_cloud = source.parent.name == "cloud"
    for node in ast.walk(ast.parse(source.read_text())):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "cloud":
                names = [a.name for a in node.names]
            elif node.module.startswith("cloud."):
                names = [node.module.split(".", 1)[1]]
            elif in_cloud and node.level == 0:
                names = [node.module]
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("cloud."):
                    names.append(a.name.split(".", 1)[1])
                elif in_cloud:
                    names.append(a.name)
        found |= {n for n in names if (ROOT / "cloud" / f"{n}.py").is_file()}
    return found


def test_cloud_modules_are_what_the_builder_imports():
    queue = [*(ROOT / "streetzim").rglob("*.py"),
             ROOT / "create_osm_zim.py", ROOT / "wikidata_cache.py"]
    seen: set[Path] = set()
    needed: set[str] = set()
    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        for name in _cloud_imports(f):
            needed.add(name)
            queue.append(ROOT / "cloud" / f"{name}.py")
    assert needed == set(paths.CLOUD_MODULES)


def _requirements(lines: list[str]) -> dict[str, str]:
    """{normalized name: spec without spaces or comments}."""
    out: dict[str, str] = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\s\[]", line, maxsplit=1)[0].lower().replace("_", "-")
        out[name] = re.sub(r"\s+", "", line).replace('"', "'")
    return out


def test_wheel_dependencies_match_requirements_txt():
    with open(ROOT / "pyproject.toml", "rb") as f:
        wheel = _requirements(tomllib.load(f)["project"]["dependencies"])
    reqs = _requirements((ROOT / "requirements.txt").read_text().splitlines())
    # Test and ops tools requirements.txt carries until it is split into
    # runtime / dev / ops files; never dependencies of the wheel.
    not_runtime = {"pytest", "internetarchive"}
    assert not (set(wheel) & not_runtime)
    assert {k: v for k, v in reqs.items() if k not in not_runtime} == wheel


def test_cache_root(monkeypatch, tmp_path):
    monkeypatch.setenv("STREETZIM_CACHE_DIR", str(tmp_path / "c"))
    assert paths.cache_root() == tmp_path / "c"
    monkeypatch.delenv("STREETZIM_CACHE_DIR")
    assert paths.cache_root() == ROOT            # a checkout: as always
    # Installed from a wheel: a user cache dir, never site-packages.
    monkeypatch.setattr(paths, "RESOURCES_DIR", paths.PACKAGED_RESOURCES)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert paths.cache_root() == tmp_path / "xdg" / "streetzim"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.cache_root() == tmp_path / "home" / ".cache" / "streetzim"
