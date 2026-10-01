"""Independent caller lifecycle and source-checkout invocation probes."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cloud import manifest_writer as mw


def entry():
    return SimpleNamespace(
        _path="index.html",
        _title="Map",
        _mimetype="text/html",
        _data=b"<html>map</html>",
        _file_path=None,
        _is_front=True,
    )


def test_checkout_import_from_other_working_directory_packs_successfully(tmp_path):
    repo = Path(mw.__file__).resolve().parent.parent
    program = f"""import sys\nsys.path.insert(0,{str(repo)!r})\nfrom cloud.manifest_writer import ManifestCreator\nfrom types import SimpleNamespace\nc=ManifestCreator({str(tmp_path / "archive.zim")!r})\nc.config_nbworkers(1)\nwith c:\n c.add_item(SimpleNamespace(_path='index.html',_title='Map',_mimetype='text/html',_data=b'<html>map</html>',_file_path=None,_is_front=True))\n"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("STREETZIM_PACK_BIN", None)
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "archive.zim").is_file()


def test_flush_error_preserves_original_producer_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
    monkeypatch.setenv("STREETZIM_KEEP_PACK_STAGE", "1")
    creator = mw.ManifestCreator(str(tmp_path / "archive.zim"))
    original_stream = creator._mf

    class BrokenFlush:
        def write(self, text):
            return original_stream.write(text)

        def close(self):
            original_stream.close()
            raise OSError("injected final flush failure")

    creator._mf = BrokenFlush()
    with pytest.raises(RuntimeError, match="producer failure"):
        with creator:
            creator.add_item(entry())
            raise RuntimeError("producer failure")
    assert creator._stage_dir.exists()
    assert not (tmp_path / "archive.zim").exists()


@pytest.mark.parametrize("keep", [False, True])
def test_failed_enter_preserves_initial_write_exception(tmp_path, monkeypatch, keep):
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
    if keep:
        monkeypatch.setenv("STREETZIM_KEEP_PACK_STAGE", "1")
    else:
        monkeypatch.delenv("STREETZIM_KEEP_PACK_STAGE", raising=False)
    creator = mw.ManifestCreator(str(tmp_path / "archive.zim"))
    original_stream = creator._mf

    class BrokenStream:
        def write(self, text):
            raise OSError("initial manifest write failure")

        def close(self):
            original_stream.close()
            raise RuntimeError("secondary close failure")

    creator._mf = BrokenStream()
    with pytest.raises(OSError, match="initial manifest write failure"):
        with creator:
            raise AssertionError("must not enter body")
    assert creator._stage_dir.exists() is keep


@pytest.mark.parametrize("phase", ["construction", "start"])
def test_monitor_failure_reaps_launched_child(tmp_path, monkeypatch, phase):
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
    monkeypatch.setenv("STREETZIM_KEEP_PACK_STAGE", "1")
    monkeypatch.setattr(mw, "_TERM_GRACE_S", 0.5)
    monkeypatch.setattr(
        mw,
        "resolve_pack_command",
        lambda: [sys.executable, "-c", "import time;time.sleep(60)"],
    )
    processes = []
    original_popen = mw.subprocess.Popen

    def capture(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(mw.subprocess, "Popen", capture)

    def broken_monitor(*args, **kwargs):
        raise RuntimeError(f"injected monitor {phase} failure")

    if phase == "construction":
        monkeypatch.setattr(mw.threading, "Thread", broken_monitor)
    else:
        monkeypatch.setattr(mw.threading.Thread, "start", broken_monitor)
    creator = mw.ManifestCreator(str(tmp_path / "archive.zim"))
    try:
        with pytest.raises(RuntimeError, match=f"monitor {phase} failure"):
            with creator:
                creator.add_item(entry())
        assert len(processes) == 1 and processes[0].poll() is not None
        assert creator._stage_dir.exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def _controlled_creator(tmp_path, monkeypatch, program, *, force_fork=False):
    script = tmp_path / "child.py"
    script.write_text(program)
    monkeypatch.setattr(
        mw, "resolve_pack_command", lambda: [sys.executable, str(script)]
    )
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
    output = tmp_path / "archive.zim"
    output.write_bytes(b"prior")
    processes = []
    popen = mw.subprocess.Popen

    def capture(*args, **kwargs):
        if force_fork:
            kwargs["preexec_fn"] = lambda: None
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(mw.subprocess, "Popen", capture)
    return mw.ManifestCreator(str(output)), output, processes


def add_entry(c):
    c.add_item(
        SimpleNamespace(
            _path="index.html",
            _title="Map",
            _mimetype="text/html",
            _data=b"map",
            _file_path=None,
        )
    )


def _assert_child_reaped(process):
    assert process.poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


class _Timer:
    def __init__(self):
        self.metrics = []

    def record_subphase(self, *args, **kwargs):
        pass

    def record_metric(self, *args):
        self.metrics.append(args)


def _peak_metric(timer):
    (metric,) = [row for row in timer.metrics if row[0] == "zim-pack: process peak RSS"]
    return float(metric[1]) * 1e6  # bytes


def _install_timer(monkeypatch):
    from streetzim import common

    timer = _Timer()
    monkeypatch.setattr(common, "PHASE_TIMER", timer)
    return timer


def test_reported_peak_is_the_packers_own_stats(tmp_path, monkeypatch):
    # The parent logs what the packer reports for itself, not wait4's figure.
    reported = 123_456 * 1024
    program = (
        "import json,os,sys\nfrom pathlib import Path\n"
        f"Path(os.environ['STREETZIM_PACK_STATS_FILE']).write_text(json.dumps({{'peak_rss_bytes': {reported}}}))\n"
        "Path(sys.argv[2]).write_bytes(b'completed fixture')\n"
    )
    c, output, processes = _controlled_creator(tmp_path, monkeypatch, program)
    timer = _install_timer(monkeypatch)
    with c:
        add_entry(c)
    assert output.read_bytes() == b"completed fixture"
    _assert_child_reaped(processes[0])
    (metric,) = [row for row in timer.metrics if row[0] == "zim-pack: process peak RSS"]
    assert metric == ("zim-pack: process peak RSS", f"{reported / 1e6:.1f}", "MB")


def _fat_parent_experiment(tmp_path, parent_mib, child):
    """Run in a fresh interpreter: hold parent_mib of touched memory, then pack
    through a regular fork (so the child starts with the parent's RSS, as a
    late-build parent's child does), and return the logged packer peak."""
    heap = bytearray(parent_mib << 20)
    for page in range(0, len(heap), 4096):
        heap[page] = 1
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
        monkeypatch.delenv("STREETZIM_PACK_BIN", raising=False)
        output = tmp_path / "archive.zim"
        if child == "python-packer":
            command = mw.resolve_pack_command()
        else:  # an executable that writes no stats file: /proc polling
            script = tmp_path / "child.py"
            script.write_text(
                "import sys,time\nfrom pathlib import Path\ndata=bytearray(64<<20)\n"
                "for page in range(0,len(data),4096):data[page]=1\n"
                "time.sleep(1.5)\nPath(sys.argv[2]).write_bytes(b'completed fixture')\n")
            command = [sys.executable, str(script)]
        monkeypatch.setattr(mw, "resolve_pack_command", lambda: command)
        popen = mw.subprocess.Popen
        monkeypatch.setattr(mw.subprocess, "Popen", lambda *args, **kwargs: popen(
            *args, **kwargs, preexec_fn=lambda: None))  # force a regular fork
        timer = _install_timer(monkeypatch)
        creator = mw.ManifestCreator(str(output), keep_stage=True)
        creator.config_nbworkers(1)
        with creator:
            add_entry(creator)
        stats = creator._stage_dir / "pack-stats.json"
        reported = json.loads(stats.read_text())["peak_rss_bytes"] if stats.exists() else None
    del heap
    return {"logged": _peak_metric(timer), "reported": reported}


@pytest.mark.parametrize("child", ["python-packer", "no-stats-executable"])
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fork/exec RSS inheritance")
def test_large_parent_does_not_inflate_reported_packer_peak(tmp_path, child):
    # wait4's ru_maxrss for this child would be at least the parent's 384 MiB.
    repo = Path(mw.__file__).resolve().parent.parent
    program = (
        "import json,sys\nfrom pathlib import Path\n"
        "from tests.test_manifest_writer_lifecycle import _fat_parent_experiment\n"
        "print(json.dumps(_fat_parent_experiment(Path(sys.argv[1]),384,sys.argv[2])))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), child],
        cwd=repo, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout.splitlines()[-1])
    assert 0 < report["logged"] < 192 << 20
    if child == "python-packer":
        # The logged figure is the packer's own VmHWM from its stats file.
        assert report["reported"] is not None
        assert abs(report["logged"] - report["reported"]) <= 0.1e6
    else:
        assert report["reported"] is None
        assert report["logged"] >= 64 << 20  # the child's own allocation


@pytest.mark.parametrize("exit_code", [7, -9])
def test_child_exit_status_and_prior_archive_are_preserved(
    tmp_path, monkeypatch, exit_code
):
    monkeypatch.setenv("STREETZIM_KEEP_PACK_STAGE", "1")
    program = "import os,signal,sys\n" + (
        "os.kill(os.getpid(),signal.SIGKILL)\n" if exit_code < 0 else "sys.exit(7)\n"
    )
    c, output, processes = _controlled_creator(tmp_path, monkeypatch, program)
    with pytest.raises(RuntimeError, match="KILLED" if exit_code < 0 else "exit 7"):
        with c:
            add_entry(c)
    assert output.read_bytes() == b"prior"
    assert len(processes) == 1 and processes[0].returncode == exit_code
    _assert_child_reaped(processes[0])
    assert c._stage_dir.exists()


@pytest.mark.parametrize("handles_sigterm", [True, False])
def test_interrupt_sends_sigterm_then_sigkill_after_grace(
    tmp_path, monkeypatch, handles_sigterm
):
    monkeypatch.setenv("STREETZIM_KEEP_PACK_STAGE", "1")
    monkeypatch.setattr(mw, "_TERM_GRACE_S", 1.0)
    marker = tmp_path / "terminated"
    ready = tmp_path / "ready"
    handler = (f"lambda *a: (Path({str(marker)!r}).write_text('x'), sys.exit(3))"
               if handles_sigterm else "signal.SIG_IGN")
    program = (
        "import signal,sys,time\nfrom pathlib import Path\n"
        f"signal.signal(signal.SIGTERM, {handler})\n"
        f"Path({str(ready)!r}).write_text('x')\n"
        "time.sleep(60)\n"
    )
    c, output, processes = _controlled_creator(tmp_path, monkeypatch, program)
    previous = signal.getsignal(signal.SIGALRM)

    def interrupt(signum, frame):
        if not ready.exists():  # wait for the child's handler
            signal.setitimer(signal.ITIMER_REAL, 0.05)
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGALRM, interrupt)
    popen = mw.subprocess.Popen

    def arm(*args, **kwargs):
        process = popen(*args, **kwargs)
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        return process

    monkeypatch.setattr(mw.subprocess, "Popen", arm)
    try:
        with pytest.raises(KeyboardInterrupt):
            with c:
                add_entry(c)
        assert output.read_bytes() == b"prior"
        assert len(processes) == 1
        _assert_child_reaped(processes[0])
        if handles_sigterm:
            assert marker.exists() and processes[0].returncode == 3
        else:
            assert processes[0].returncode == -signal.SIGKILL
        assert c._stage_dir.exists()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
