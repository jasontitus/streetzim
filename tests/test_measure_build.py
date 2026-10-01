"""Resource reports must include descendants and never confuse unknown with zero."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import measure_build as measure


def test_macos_tree_includes_grandchildren_and_excludes_other_jobs(monkeypatch):
    monkeypatch.setattr(measure.sys, "platform", "darwin")
    monkeypatch.setattr(measure.subprocess, "check_output", lambda *a, **kw:
                        " 10 1 100\n 11 10 200\n 12 11 300\n 13 1 999\n")
    assert measure.tree_mem(10) == (600 * 1024, None)


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf"])
def test_invalid_interval_does_not_launch_command(tmp_path, monkeypatch, interval):
    def launch(*a, **kw):
        pytest.fail("must validate before launching a command")
    monkeypatch.setattr(measure.subprocess, "Popen", launch)
    with pytest.raises(SystemExit, match="2"):
        measure.main(["--json", str(tmp_path / "r.json"), "--interval", interval,
                      "--", "anything"])


def test_report_measures_real_process_tree_and_bounds_samples(tmp_path):
    report = tmp_path / "r.json"
    # Two resident byte arrays alive concurrently, held long enough to sample.
    child = "import time; x=bytearray(32*1024**2); time.sleep(.8)"
    parent = ("import subprocess,sys,time; x=bytearray(24*1024**2); "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); p.wait()")
    rc = measure.main(["--json", str(report), "--interval", ".05",
                       "--max-samples", "2", "--", sys.executable, "-c", parent])
    data = json.loads(report.read_text())
    assert rc == data["exit_code"] == 0
    assert data["peak_rss_bytes"] >= 56 * 1024**2
    assert data["peak_child_rss_bytes"] > 24 * 1024**2
    assert data["cpu_s"] > 0 and data["wall_s"] >= .8
    assert data["sample_count"] > 2
    assert len(data["samples"]) == 2
    assert data["samples_dropped"] == data["sample_count"] - 2
    assert data["sampling_error_count"] == 0
    if sys.platform == "darwin":
        assert data["peak_pss_bytes"] is None and data["peak_pss_gb"] is None
    else:
        assert data["peak_pss_bytes"] > 0


def test_failed_measurements_are_reported(tmp_path, monkeypatch):
    def unavailable(pid):
        raise OSError("cannot read processes")
    monkeypatch.setattr(measure, "tree_mem", unavailable)
    report = tmp_path / "r.json"
    assert measure.main(["--json", str(report), "--interval", ".01", "--",
                         sys.executable, "-c", "import time; time.sleep(.1)"]) == 0
    data = json.loads(report.read_text())
    assert data["peak_rss_bytes"] is None
    assert data["sampling_error_count"] > 0
    assert data["sampling_errors"] and data["sample_count"] == 0


def test_exit_code_and_final_disk_and_overlapping_watches(tmp_path):
    report = tmp_path / "r.json"
    watched = tmp_path / "out"
    watched.mkdir()
    child = watched / "child"
    child.mkdir()
    payload = child / "data"
    cmd = f"from pathlib import Path; Path({str(payload)!r}).write_bytes(b'x'*10000); exit(7)"
    assert measure.main(["--json", str(report), "--watch", str(watched),
                         "--watch", str(child), "--", sys.executable, "-c", cmd]) == 7
    data = json.loads(report.read_text())
    assert data["exit_code"] == 7
    assert data["watched"] == [str(watched.resolve())]
    assert data["peak_disk_bytes"] == measure.du(watched) > 0


def test_cli_reports_missing_command_without_traceback(tmp_path):
    result = subprocess.run([sys.executable, str(Path(measure.__file__)),
                             "--json", str(tmp_path / "r.json")], capture_output=True)
    assert result.returncode == 2 and b"no command" in result.stderr


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_monitor_cancellation_stops_descendant_that_ignores_term(tmp_path, stop_signal):
    ready = tmp_path / "child.pid"
    child = ("import os,signal,time; from pathlib import Path; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)")
    parent = f"import sys,subprocess; subprocess.Popen([sys.executable,'-c',{child!r}]).wait()"
    monitor = subprocess.Popen([sys.executable, str(Path(measure.__file__)), "--json",
                                str(tmp_path / "r.json"), "--", sys.executable, "-c", parent],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "command never became ready"
        pid = int(ready.read_text())
        monitor.send_signal(stop_signal)
        assert monitor.wait(timeout=10) != 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if sys.platform == "linux":
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except FileNotFoundError:
                    state = ""
            else:
                state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                       capture_output=True, text=True).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(.02)
        else:
            pytest.fail("cancelled monitor left a live descendant")
    finally:
        if monitor.poll() is None:
            monitor.kill()
            monitor.wait()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
