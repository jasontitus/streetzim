"""Packaging hook; the metadata is in pyproject.toml (docs/packaging.md).

Only one thing here: of cloud/, the sdist and the wheel carry just the modules
the builder imports (streetzim/paths.py CLOUD_MODULES). The rest of cloud/
is operations code, much of it symlinks into ops/, and stays in the checkout,
where the build host imports it as before. This covers every way of building
(python -m build, pip install ., pip wheel .), which MANIFEST.in would not.
"""
from __future__ import annotations

import runpy
from typing import Any

from setuptools import setup
from setuptools.command.build_py import build_py

CLOUD_MODULES = set(runpy.run_path("streetzim/paths.py")["CLOUD_MODULES"])


class BuildPy(build_py):
    def find_package_modules(self, package: str, package_dir: str) -> list[Any]:
        modules = super().find_package_modules(package, package_dir)
        if package != "cloud":
            return modules
        # Entries are (package, module, filename).
        return [m for m in modules if m[1] in CLOUD_MODULES]


setup(cmdclass={"build_py": BuildPy})
