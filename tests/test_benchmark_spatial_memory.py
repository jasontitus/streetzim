"""Benchmark reports must never overwrite an input or measured output."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import benchmark_spatial_memory as benchmark


def _graph(path):
    benchmark.generate(path, nodes=5, edges=12, geom_size=48, cells=3)
    return path.read_bytes()


def _command(graph, output, report, *, copy_input=False, reference=False):
    args = [sys.executable, benchmark.__file__, "run", "--graph", str(graph),
            "--output", str(output), "--report", str(report)]
    if copy_input:
        args.append("--copy-input")
    if reference:
        args.append("--reference")
    return args


@pytest.mark.parametrize("copy_input", [False, True])
@pytest.mark.parametrize("alias", ["input", "hardlink", "symlink", "dangling",
                                   "prior-report", "cell", "index"])
def test_cli_rejects_report_collision_before_conversion(tmp_path, alias, copy_input):
    graph, output = tmp_path / "graph.bin", tmp_path / "cells"
    original = _graph(graph)
    report = tmp_path / "report.json"
    if alias == "input":
        report = graph
    elif alias == "hardlink":
        os.link(graph, report)
    elif alias == "symlink":
        report.symlink_to(graph)
    elif alias == "dangling":
        report.symlink_to(tmp_path / "missing")
    elif alias == "prior-report":
        report.write_bytes(b"prior evidence")
    elif alias == "cell":
        report = output / "graph-cell-00000.bin"
    else:
        report = output / "graph-cells-index.bin"
    before = report.read_bytes() if report.exists() else None
    result = subprocess.run(_command(graph, output, report, copy_input=copy_input),
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "report path already exists" in result.stderr or "report must be outside" in result.stderr
    assert graph.read_bytes() == original
    assert not output.exists()
    if before is not None:
        assert report.read_bytes() == before
    if alias in ("symlink", "dangling"):
        assert report.is_symlink()
    if alias == "dangling":
        assert not (tmp_path / "missing").exists()


def test_report_path_through_output_alias_is_rejected(tmp_path):
    output = tmp_path / "cells"
    alias = tmp_path / "alias"
    alias.symlink_to(output, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the output directory"):
        benchmark._validate_report_destination(output, alias / "report.json")
    assert not output.exists()


@pytest.mark.parametrize("created", ["regular", "symlink", "hardlink"])
def test_exclusive_publication_preserves_path_created_after_preflight(tmp_path, monkeypatch, created):
    graph, output, report = tmp_path / "graph.bin", tmp_path / "cells", tmp_path / "report.json"
    original = _graph(graph)
    real_open = Path.open

    def create_before_publication(path, mode="r", *args, **kwargs):
        if path == report and mode == "x":
            if created == "regular":
                with real_open(report, "wb") as stream:
                    stream.write(b"concurrent evidence")
            elif created == "symlink":
                report.symlink_to(graph)
            else:
                os.link(graph, report)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", create_before_publication)
    with pytest.raises(FileExistsError):
        benchmark.run(graph, output, report, False, .1)
    assert graph.read_bytes() == original
    assert report.read_bytes() == (b"concurrent evidence" if created == "regular" else original)
    if created == "symlink":
        assert report.is_symlink()


@pytest.mark.parametrize("copy_input", [False, True])
@pytest.mark.parametrize("reference", [False, True])
def test_successful_cli_keeps_source_and_reports_actual_output(tmp_path, copy_input, reference):
    graph, output, report = tmp_path / "graph.bin", tmp_path / "cells", tmp_path / "report.json"
    original = _graph(graph)
    result = subprocess.run(_command(graph, output, report, copy_input=copy_input,
                                     reference=reference), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(report.read_text())
    digest = hashlib.sha256()
    for path in sorted(output.glob("*.bin")):
        digest.update(path.name.encode() + b"\0")
        digest.update(path.read_bytes())
    assert data["output_sha256"] == digest.hexdigest()
    assert data["output_bytes"] == sum(p.stat().st_size for p in output.glob("*.bin"))
    assert graph.read_bytes() == original
    assert data["input_preparation"]["private_input_copy"] == copy_input
    if copy_input:
        assert not Path(data["input_preparation"]["disk_directory"]).exists()
