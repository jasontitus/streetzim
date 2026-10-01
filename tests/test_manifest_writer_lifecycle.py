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


def test_failed_enter_preserves_initial_write_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
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
    assert creator._stage_dir.exists()


@pytest.mark.parametrize("phase", ["construction", "start"])
def test_monitor_failure_reaps_launched_child(tmp_path, monkeypatch, phase):
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
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


def _controlled_creator(tmp_path, monkeypatch, program):
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


@pytest.mark.skipif(not hasattr(os, "wait4"), reason="POSIX wait4 probe")
def test_wait4_reports_exact_short_child_peak_without_previous_child_pollution(
    tmp_path, monkeypatch
):
    subprocess.run([sys.executable, "-c", "data=b'x'*(256<<20)"], check=True)
    reference = tmp_path / "reference.json"
    program = f"""import json,resource,sys\nfrom pathlib import Path\ndata=bytearray(64<<20)\nfor page in range(0,len(data),4096):data[page]=1\ndel data\nPath({str(reference)!r}).write_text(json.dumps(dict(maxrss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)))\nPath(sys.argv[2]).write_bytes(b'completed fixture')\n"""
    c, output, processes = _controlled_creator(tmp_path, monkeypatch, program)

    class Timer:
        def __init__(self):
            self.metrics = []

        def record_subphase(self, *args, **kwargs):
            pass

        def record_metric(self, *args):
            self.metrics.append(args)

    from streetzim import common

    timer = Timer()
    monkeypatch.setattr(common, "PHASE_TIMER", timer)
    real_wait4 = os.wait4
    reaped = []

    def capture_wait4(pid, options):
        result = real_wait4(pid, options)
        reaped.append(result)
        return result

    monkeypatch.setattr(os, "wait4", capture_wait4)
    with c:
        add_entry(c)
    assert output.read_bytes() == b"completed fixture"
    assert len(processes) == 1 and processes[0].returncode == 0
    _assert_child_reaped(processes[0])
    raw = json.loads(reference.read_text())["maxrss"]
    (reaped_pid, _status, usage), = reaped
    assert reaped_pid == processes[0].pid
    factor = 1 if sys.platform == "darwin" else 1024
    self_peak = raw * factor
    expected = usage.ru_maxrss * factor
    assert 64 << 20 <= self_peak <= expected < 192 << 20
    # The reference is written before exit; final file I/O can add a few
    # pages. Compare the reported metric exactly with the lifetime wait4
    # peak, while retaining an independent child-reported sanity check.
    assert expected - self_peak <= 1 << 20
    (metric,) = [row for row in timer.metrics if row[0] == "zim-pack: process peak RSS"]
    assert metric == ("zim-pack: process peak RSS", f"{expected / 1e6:.1f}", "MB")


@pytest.mark.parametrize("exit_code", [7, -9])
@pytest.mark.skipif(not hasattr(os, "wait4"), reason="POSIX wait4 probe")
def test_wait4_preserves_real_child_exit_status_and_prior_archive(
    tmp_path, monkeypatch, exit_code
):
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


@pytest.mark.skipif(not hasattr(os, "wait4"), reason="POSIX wait4 probe")
def test_interrupting_real_wait4_kills_and_reaps_only_launched_child(
    tmp_path, monkeypatch
):
    c, output, processes = _controlled_creator(
        tmp_path, monkeypatch, "import time\ntime.sleep(60)\n"
    )
    previous = signal.getsignal(signal.SIGALRM)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    wait4 = mw.os.wait4

    def interrupt_wait4(pid, flags):
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        return wait4(pid, flags)

    monkeypatch.setattr(mw.os, "wait4", interrupt_wait4)
    signal.signal(signal.SIGALRM, interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            with c:
                add_entry(c)
        assert output.read_bytes() == b"prior"
        assert len(processes) == 1
        _assert_child_reaped(processes[0])
        assert c._stage_dir.exists()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
