"""Interrupted external indexing must not survive removal of build scratch."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from streetzim import zim_writer as writer


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    source = tmp_path / 'search.jsonl'
    source.write_text('{"name":"Test Place","type":"city","lat":1,"lon":2}\n')
    monkeypatch.setattr(writer, '_resolve_xapianbuilder_binary', lambda override: sys.executable)
    monkeypatch.setattr(writer, '_XAPIAN_TERMINATE_TIMEOUT', .1)
    return source


def sleeping_child(tmp_path, mode, real_popen, *, ignore_term=False, output_path=None):
    ready = tmp_path / f'{mode}.ready'
    code = ('import signal,time; from pathlib import Path; '
            + ('signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if ignore_term else '')
            + (f'Path({output_path!r}).write_bytes(b"partial index"); ' if output_path else '')
            + f'Path({str(ready)!r}).write_text("ready"); time.sleep(30)')
    child = real_popen([sys.executable, '-c', code])
    deadline = time.monotonic() + 5
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
    if not ready.exists():
        child.kill()
        child.wait()
        pytest.fail('test child did not start')
    return child


@pytest.mark.parametrize('failure', [KeyboardInterrupt(), SystemExit(143)])
@pytest.mark.parametrize('ignore_term', [False, True])
def test_interrupted_wait_terminates_and_reaps_both_children(
        tmp_path, monkeypatch, corpus, failure, ignore_term):
    real_popen = subprocess.Popen
    children = []

    class InterruptFirstWait:
        def __init__(self, child):
            self.child = child
            self.interrupted = False

        def poll(self):
            return self.child.poll()

        def terminate(self):
            self.child.terminate()

        def kill(self):
            self.child.kill()

        def wait(self, timeout=None):
            if timeout is None and not self.interrupted:
                self.interrupted = True
                raise failure
            return self.child.wait(timeout=timeout)

    def launch(cmd):
        path = cmd[cmd.index('--output') + 1]
        child = sleeping_child(tmp_path, cmd[1], real_popen,
                               ignore_term=ignore_term, output_path=path)
        children.append(child)
        return InterruptFirstWait(child) if cmd[1] == 'fulltext' else child

    monkeypatch.setattr(subprocess, 'Popen', launch)
    try:
        with pytest.raises(type(failure)) as error:
            writer._build_xapian_via_xapianbuilder(str(corpus), str(tmp_path / 'work'))
        assert error.value is failure  # cleanup preserves the original interrupt
        assert len(children) == 2
        expected = -signal.SIGKILL if ignore_term else -signal.SIGTERM
        assert [child.returncode for child in children] == [expected, expected]
        assert not list((tmp_path / 'work').glob('*.glass'))
        for child in children:
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)  # already reaped, not a zombie
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_second_launch_failure_stops_already_running_indexer(tmp_path, monkeypatch, corpus):
    real_popen = subprocess.Popen
    children = []
    failure = OSError(13, 'second indexer cannot start')

    def launch(cmd):
        if cmd[1] == 'title':
            raise failure
        path = cmd[cmd.index('--output') + 1]
        child = sleeping_child(tmp_path, cmd[1], real_popen, output_path=path)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, 'Popen', launch)
    try:
        with pytest.raises(OSError) as error:
            writer._build_xapian_via_xapianbuilder(str(corpus), str(tmp_path / 'work'))
        assert error.value is failure
        assert len(children) == 1 and children[0].returncode == -signal.SIGTERM
        assert not list((tmp_path / 'work').glob('*.glass'))
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize('fulltext_rc', [0, 7])
def test_normal_indexers_keep_outputs_and_failed_indexes_are_retryable(
        tmp_path, monkeypatch, corpus, fulltext_rc):
    real_popen = subprocess.Popen
    children = []

    def launch(cmd):
        mode = cmd[1]
        path = cmd[cmd.index('--output') + 1]
        rc = fulltext_rc if mode == 'fulltext' else 0
        code = (f'from pathlib import Path; Path({path!r}).write_bytes({mode.encode()!r}); '
                f'exit({rc})')
        child = real_popen([sys.executable, '-c', code])
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, 'Popen', launch)
    if fulltext_rc:
        with pytest.raises(RuntimeError, match='fulltext: rc=7'):
            writer._build_xapian_via_xapianbuilder(str(corpus), str(tmp_path / 'work'))
        assert not list((tmp_path / 'work').glob('*.glass'))
    else:
        fulltext, title = writer._build_xapian_via_xapianbuilder(str(corpus), str(tmp_path / 'work'))
        assert Path(fulltext).read_bytes() == b'fulltext'
        assert Path(title).read_bytes() == b'title'
    assert len(children) == 2
    assert [child.returncode for child in children] == [fulltext_rc, 0]
    if fulltext_rc:
        # A retry must actually rebuild both jobs instead of accepting the
        # nonempty output the failed indexer wrote before its nonzero exit.
        fulltext_rc = 0
        fulltext, title = writer._build_xapian_via_xapianbuilder(str(corpus), str(tmp_path / 'work'))
        assert len(children) == 4
        assert Path(fulltext).read_bytes() == b'fulltext'
        assert Path(title).read_bytes() == b'title'


@pytest.mark.parametrize('cached_mode', ['fulltext', 'title'])
def test_failed_new_index_preserves_preexisting_reused_database(
        tmp_path, monkeypatch, corpus, cached_mode):
    real_popen = subprocess.Popen
    workdir = tmp_path / 'work'
    workdir.mkdir()
    cached = workdir / f'X-{cached_mode}-xapian.glass'
    cached.write_bytes(b'existing complete index')
    launched = []

    def launch(cmd):
        mode = cmd[1]
        launched.append(mode)
        path = cmd[cmd.index('--output') + 1]
        code = f'from pathlib import Path; Path({path!r}).write_bytes(b"partial index"); exit(7)'
        return real_popen([sys.executable, '-c', code])

    monkeypatch.setattr(subprocess, 'Popen', launch)
    with pytest.raises(RuntimeError, match='rc=7'):
        writer._build_xapian_via_xapianbuilder(str(corpus), str(workdir))
    missing_mode = 'title' if cached_mode == 'fulltext' else 'fulltext'
    assert launched == [missing_mode]
    assert cached.read_bytes() == b'existing complete index'
    assert not (workdir / f'X-{missing_mode}-xapian.glass').exists()
