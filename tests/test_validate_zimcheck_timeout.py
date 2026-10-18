"""validate_zim's zimcheck time limit: 1800 s unless STREETZIM_ZIMCHECK_TIMEOUT
gives a positive number of seconds (a busy host ran zimru on the 35 GB and
61 GB maps past 1800 s, 2026-10-18)."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("validate_zim", ROOT / "cloud" / "validate_zim.py")
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)


@pytest.mark.parametrize("value, want", [(None, 1800.0), ("", 1800.0), ("7200", 7200.0),
                                         ("90.5", 90.5), ("0", 1800.0), ("-5", 1800.0),
                                         ("abc", 1800.0)])
def test_the_limit(monkeypatch, value, want):
    if value is None:
        monkeypatch.delenv("STREETZIM_ZIMCHECK_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("STREETZIM_ZIMCHECK_TIMEOUT", value)
    assert V._zimcheck_timeout() == want


def test_the_check_uses_it(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    zimcheck = tmp_path / "zimcheck"
    zimcheck.write_text("#!/bin/sh\nexit 0\n")
    zimcheck.chmod(0o755)
    monkeypatch.setenv("ZIMRU_ZIMCHECK", str(zimcheck))
    monkeypatch.setenv("STREETZIM_ZIMCHECK_TIMEOUT", "4321")
    monkeypatch.setattr(subprocess, "run", fake_run)   # imported inside the check
    status, msg = V._chk_zimcheck_external(str(tmp_path / "x.zim"))
    assert seen["timeout"] == 4321.0
    assert (status, msg) == ("fail", "zimcheck timed out after 4321 s")
