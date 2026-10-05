"""A seeded shared cache must not become every region's in-memory data set."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import wikidata_cache as wc


@pytest.fixture
def seeded(tmp_path):
    data = {"Q110": {"label": "Local", "extract": "Local article", "population": 5},
            "Q111": {"label": "Same bucket, other region", "extract": "Keep me"},
            "Q99": {"label": "Another region", "description": "Still cached"},
            "Q3": {"label": "Single digit"}}
    wc.save_cache(tmp_path, data)
    return tmp_path, data


def test_selection_preserves_exact_compact_fields_and_default_api(seeded):
    path, entries = seeded
    full = wc.load_cache_for_zim(path)
    assert wc.load_cache(path) == entries
    assert wc.load_cache(path, qids={"Q110", "Q3", "Q404"}) == {
        q: entries[q] for q in ("Q110", "Q3")}
    assert wc.load_cache_for_zim(path, qids={"Q110", "Q3", "Q404"}) == {
        q: full[q] for q in ("Q110", "Q3")}
    assert wc.load_cache_for_zim(path, qids=set()) is None


def test_empty_selection_never_reads_shared_cache(seeded, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("empty selection read shared cache")

    monkeypatch.setattr(wc, "_cache_buckets", forbidden)
    assert wc.load_cache(seeded[0], qids=[]) == {}
    assert wc.load_cache_for_zim(seeded[0], qids=set()) is None


@pytest.mark.parametrize("source", ["pbf", "mbtiles"])
@pytest.mark.parametrize("features", [{}, {"Q1;Q2": {}}, {"Q110": {}}])
def test_build_returns_exact_selection_on_all_cached_and_empty_paths(
        seeded, monkeypatch, source, features):
    path, entries = seeded
    monkeypatch.setattr(wc, "extract_qids_from_pbf", lambda *a, **k: features)
    monkeypatch.setattr(wc, "extract_qids_from_mbtiles", lambda *a, **k: features)
    args = {source + "_path": "source", "cache_dir": path, "skip_extracts": True}
    returned, selected = wc.build_cache(**args, return_qids=True)
    assert returned == path
    assert selected == ({"Q110"} if "Q110" in features else set())
    assert wc.build_cache(**args) == path
    assert wc.load_cache(path) == entries


def test_regional_update_and_retry_preserve_unrelated_cache_and_manifest(seeded, monkeypatch):
    path, entries = seeded
    features = {"Q110": {}, "Q112": {}, "Q400": {}}
    monkeypatch.setattr(wc, "extract_qids_from_pbf", lambda *a, **k: features)
    requests = []

    def fetch(qids, **kwargs):
        requests.append(set(qids))
        # Q400 is unanswered; it must be tried again next run.
        return {"Q112": {"label": "New", "wikipedia_title": "New"}}

    monkeypatch.setattr(wc, "fetch_wikidata_batch", fetch)
    retries = []

    def extracts(data):
        retries.append(set(data))

    monkeypatch.setattr(wc, "fetch_wikipedia_extracts", extracts)
    for _ in range(2):
        result, selected = wc.build_cache(pbf_path="x", cache_dir=path, return_qids=True)
        assert result == path and selected == set(features)
    assert requests == [{"Q112", "Q400"}, {"Q400"}]
    assert retries == [{"Q112"}, {"Q112"}]
    cached = wc.load_cache(path)
    assert {q: cached[q] for q in entries} == entries
    assert set(cached) == set(entries) | {"Q112"}
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["total_entries"] == len(cached)
    assert manifest["buckets"] == 3
    assert set(wc.load_cache_for_zim(path, qids=selected)) == {"Q110", "Q112"}


@pytest.mark.parametrize("source", ["pbf", "mbtiles"])
def test_pipeline_uses_current_selection_without_reextracting(seeded, monkeypatch, source):
    import create_osm_zim as c
    path, _ = seeded
    calls = []

    def extract(*a, **k):
        calls.append(source)
        return {"Q110": {}}

    monkeypatch.setattr(wc, "extract_qids_from_pbf", extract)
    monkeypatch.setattr(wc, "extract_qids_from_mbtiles", extract)
    result, selection = c._build_wikidata(
        args=argparse.Namespace(pbf=None, wikidata_no_extracts=True),
        include_wikidata=True, pbf_path="x" if source == "pbf" else None,
        mbtiles_path="tiles", work_pbf=None, total_steps=9, wikidata_cache_dir=path)
    assert set(result) == {"Q110"} == selection
    assert calls == [source]


@pytest.mark.parametrize("fault", ["read", "decode"])
def test_regional_build_merge_failure_preserves_shared_bucket(tmp_path, monkeypatch, fault):
    entries = {"Q110": {"label": "Selected", "wikipedia_title": "Selected"},
               "Q111": {"label": "Foreign in same bucket"},
               "Q99": {"label": "Foreign in another bucket"}}
    wc.save_cache(tmp_path, entries)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    monkeypatch.setattr(wc, "extract_qids_from_pbf", lambda *a, **k: {"Q110": {}})

    def properties(*a, **k):
        pytest.fail("cached selected properties were fetched again")

    def extracts(data):
        assert set(data) == {"Q110"}
        data["Q110"]["extract"] = "Completed"

    monkeypatch.setattr(wc, "fetch_wikidata_batch", properties)
    monkeypatch.setattr(wc, "fetch_wikipedia_extracts", extracts)
    original_load = wc.json.load
    reads = 0
    exception = (OSError("transient merge read failure") if fault == "read"
                 else json.JSONDecodeError("transient partial decode", "{", 1))

    def fail_merge(stream, *a, **k):
        nonlocal reads
        if Path(stream.name).name == "11.json":
            reads += 1
            if reads == 2:  # initial selected load succeeds; the merge fails
                raise exception
        return original_load(stream, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(wc.json, "load", fail_merge)
        with pytest.raises(type(exception)):
            wc.build_cache(pbf_path="source", cache_dir=tmp_path, return_qids=True)
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before
    assert wc.load_cache(tmp_path) == entries
    assert not list(tmp_path.glob("*.tmp"))

    # A failed read remains retryable; the next regional build succeeds.
    assert wc.build_cache(pbf_path="source", cache_dir=tmp_path, return_qids=True) == (
        tmp_path, {"Q110"})
    cached = wc.load_cache(tmp_path)
    assert cached["Q110"]["extract"] == "Completed"
    assert cached["Q111"] == entries["Q111"] and cached["Q99"] == entries["Q99"]


@pytest.mark.parametrize("fault", ["partial_write", "interrupt", "replace"])
def test_failed_publication_preserves_cache_and_removes_stage(seeded, monkeypatch, fault):
    path, entries = seeded
    before = {p.name: p.read_bytes() for p in path.glob("*.json")}
    exception = (KeyboardInterrupt("interrupted write") if fault == "interrupt"
                 else OSError("publication failed"))

    def partial_write(value, stream, *a, **k):
        stream.write("{")
        stream.flush()
        raise exception

    def replace(*a, **k):
        raise exception

    with monkeypatch.context() as m:
        if fault == "replace":
            m.setattr(wc.os, "replace", replace)
        else:
            m.setattr(wc.json, "dump", partial_write)
        with pytest.raises(type(exception)):
            wc.save_cache(path, {"Q110": {"label": "Updated"}})
    assert {p.name: p.read_bytes() for p in path.glob("*.json")} == before
    assert wc.load_cache(path) == entries
    assert not list(path.glob("*.tmp"))


def test_cleanup_failure_preserves_primary_error_and_previous_cache(seeded, monkeypatch):
    path, entries = seeded
    before = {p.name: p.read_bytes() for p in path.glob("*.json")}
    primary = OSError("primary rename failure")
    original_unlink = Path.unlink

    def failed_replace(*a, **k):
        raise primary

    def failed_cleanup(candidate, *a, **k):
        if candidate.parent.parent == path and candidate.parent.name.startswith(".11.json."):
            raise PermissionError("cleanup unlink denied")
        return original_unlink(candidate, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(wc.os, "replace", failed_replace)
        m.setattr(Path, "unlink", failed_cleanup)
        with pytest.raises(OSError, match="primary rename failure") as raised:
            wc.save_cache(path, {"Q110": {"label": "Updated"}})
        assert raised.value is primary
    assert {p.name: p.read_bytes() for p in path.glob("*.json")} == before
    assert wc.load_cache(path) == entries
    # When cleanup itself fails, only an owned private stage remains.
    stages = list(path.glob("*.tmp"))
    assert len(stages) == 1
    staged_data = json.loads((stages[0] / "contents").read_text())
    assert staged_data["Q110"]["label"] == "Updated"
    assert staged_data["Q111"] == entries["Q111"]


@pytest.mark.parametrize("fault", ["read", "decode"])
def test_an_unreadable_unrelated_bucket_warns_and_is_kept(seeded, monkeypatch, capsys, fault):
    """The recount only feeds the manifest's totals, which no build reads:
    a bucket this build does not touch that cannot be read is a warning,
    left out of the totals and never rewritten."""
    path, entries = seeded
    before_manifest = (path / "manifest.json").read_bytes()
    before_foreign = (path / "99.json").read_bytes()
    original_load = wc.json.load
    exception = (OSError("unreadable untouched bucket") if fault == "read"
                 else json.JSONDecodeError("partial untouched bucket", "{", 1))

    def fail_recount(stream, *a, **k):
        if Path(stream.name).name == "99.json":
            raise exception
        return original_load(stream, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(wc.json, "load", fail_recount)
        wc.save_cache(path, {"Q112": {"label": "New local fact"}})
    assert "cannot read Wikidata cache bucket" in capsys.readouterr().out
    assert (path / "manifest.json").read_bytes() != before_manifest
    assert json.loads((path / "manifest.json").read_text())["total_entries"] == (
        len(entries) + 1 - 1)                       # Q99's bucket left out
    assert (path / "99.json").read_bytes() == before_foreign
    assert wc.load_cache(path) == {**entries, "Q112": {"label": "New local fact"}}
    assert not list(path.glob("*.tmp"))


def test_staging_collision_preserves_unowned_file_and_prior_cache(seeded, monkeypatch):
    path, entries = seeded
    token = wc.uuid.UUID(int=0)
    staging = path / f".11.json.{token.hex}.tmp"
    before = {p.name: p.read_bytes() for p in path.glob("*.json")}

    def colliding_uuid():
        # Appears as the name is chosen, after any dead-stage cleanup: an
        # exclusive create must not remove a file it did not make.
        if not staging.exists():
            staging.write_bytes(b"another invocation owns this staging file")
        return token

    with monkeypatch.context() as m:
        m.setattr(wc.uuid, "uuid4", colliding_uuid)
        with pytest.raises(FileExistsError):
            wc.save_cache(path, {"Q110": {"label": "Updated"}})
    assert staging.read_bytes() == b"another invocation owns this staging file"
    assert {p.name: p.read_bytes() for p in path.glob("*.json")} == before
    assert wc.load_cache(path) == entries


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shared-cache permission bits")
def test_replacing_shared_cache_preserves_existing_permissions(seeded):
    path, _ = seeded
    for name in ("11.json", "manifest.json"):
        (path / name).chmod(0o664)
    wc.save_cache(path, {"Q110": {"label": "Updated"}})
    assert (path / "11.json").stat().st_mode & 0o777 == 0o664
    assert (path / "manifest.json").stat().st_mode & 0o777 == 0o664


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX creation umask")
def test_new_cache_files_respect_normal_umask(tmp_path):
    # Set umask in an isolated process so this test cannot affect other
    # threads or tests in the pytest process.
    code = """
import json
import os
from pathlib import Path
import sys
import wikidata_cache as wc
os.umask(0o027)
path = Path(sys.argv[1])
wc.save_cache(path, {'Q110': {'label': 'New'}})
print(json.dumps({name: (path / name).stat().st_mode & 0o777
                  for name in ('11.json', 'manifest.json')}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], cwd=Path(wc.__file__).resolve().parent,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True, timeout=10)
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "11.json": 0o640, "manifest.json": 0o640}


_OVERLAPPING_WRITER = r"""
import json
from pathlib import Path
import sys
import time
import wikidata_cache as wc

cache, markers, role = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
original_load = json.load

def wait_for(name, timeout=10):
    deadline = time.monotonic() + timeout
    while not (markers / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('writer handshake timed out: ' + name)
        time.sleep(0.005)

def overlapping_merge(stream, *a, **k):
    value = original_load(stream, *a, **k)
    if Path(stream.name).name == '11.json':
        (markers / (role + '_read')).write_text('read completed')
        if role == 'first':
            wait_for('second_entered')
            deadline = time.monotonic() + 0.3
            while not (markers / 'second_read').exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            if (markers / 'second_read').exists():
                # With no serialization, force the second writer to finish
                # before the first publishes its stale in-memory bucket.
                wait_for('second_done')
    return value

json.load = overlapping_merge
(markers / (role + '_entered')).write_text('saving')
qid = 'Q112' if role == 'first' else 'Q113'
wc.save_cache(cache, {qid: {'label': qid}})
(markers / (role + '_done')).write_text('saved')
"""


@pytest.mark.skipif(sys.platform == "win32", reason="shared cache locking uses POSIX flock")
def test_overlapping_shared_bucket_writers_preserve_both_regions(seeded, tmp_path):
    path, entries = seeded
    markers = tmp_path / "markers"
    markers.mkdir()
    children = []

    def start(role):
        child = subprocess.Popen(
            [sys.executable, "-c", _OVERLAPPING_WRITER, str(path), str(markers), role],
            cwd=Path(wc.__file__).resolve().parent, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        children.append(child)

    try:
        start("first")
        deadline = time.monotonic() + 10
        while not (markers / "first_read").exists():
            if time.monotonic() > deadline:
                pytest.fail("first writer did not reach its shared bucket merge")
            time.sleep(0.01)
        start("second")
        for child in children:
            output, _ = child.communicate(timeout=15)
            assert child.returncode == 0, output
    finally:
        for child in children:
            if child.returncode is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait()

    assert wc.load_cache(path) == {**entries, "Q112": {"label": "Q112"},
                                 "Q113": {"label": "Q113"}}
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["total_entries"] == len(entries) + 2 and manifest["buckets"] == 3
    assert not list(path.glob("*.tmp"))


def _dead_stages(path):
    """What writers killed mid-publication leave: a bucket's private stage
    folder with its contents, a manifest stage file, and lock stage files,
    one fresh (maybe a live writer's) and one old."""
    import os
    import time
    stage_dir = path / f".11.json.{'1' * 32}.tmp"
    stage_dir.mkdir()
    (stage_dir / "contents").write_text("{}")
    manifest_stage = path / f".manifest.json.{'2' * 32}.tmp"
    manifest_stage.write_text("{}")
    fresh_lock = path / f".manifest.json.lock.{'3' * 32}.tmp"
    fresh_lock.write_text("")
    old_lock = path / f".manifest.json.lock.{'4' * 32}.tmp"
    old_lock.write_text("")
    old = time.time() - 2 * wc._LOCK_STAGE_MAX_AGE_S
    os.utime(old_lock, (old, old))
    stranger = path / ".notes.tmp"
    stranger.write_text("not the cache's")
    return {"dead": [stage_dir, manifest_stage, old_lock], "kept": [fresh_lock, stranger]}


@pytest.mark.parametrize("when", ["build start", "next save"])
def test_stages_left_by_killed_writers_are_removed(seeded, capsys, when):
    path, entries = seeded
    stages = _dead_stages(path)
    if when == "build start":
        wc.clean_dead_stages(path)
    else:
        wc.save_cache(path, {"Q110": {"label": "Updated"}})
    assert not any(p.exists() for p in stages["dead"])
    assert all(p.exists() for p in stages["kept"])
    assert "Removed 3 staging file(s)" in capsys.readouterr().out
    assert wc.load_cache(path)["Q111"] == entries["Q111"]


def test_build_start_does_not_lock_a_clean_cache(seeded, monkeypatch):
    path, _ = seeded
    monkeypatch.setattr(wc, "_prepare_cache_lock",
                        lambda *a: (_ for _ in ()).throw(AssertionError("locked")))
    wc.clean_dead_stages(path)


def test_a_build_clears_dead_stages_before_it_starts(seeded, monkeypatch):
    path, _ = seeded
    stages = _dead_stages(path)
    monkeypatch.setattr(wc, "extract_qids_from_pbf", lambda *a, **k: {})
    wc.build_cache(pbf_path="x.pbf", cache_dir=path)
    assert not any(p.exists() for p in stages["dead"])


def test_a_symlink_named_like_a_stage_is_removed_not_followed(seeded):
    path, _ = seeded
    target = path.parent / "not-the-cache"
    target.mkdir()
    (target / "keep").write_text("someone's file")
    link = path / f".11.json.{'5' * 32}.tmp"
    link.symlink_to(target, target_is_directory=True)
    wc.clean_dead_stages(path)
    assert not link.is_symlink() and (target / "keep").read_text() == "someone's file"


@pytest.mark.parametrize("name", [
    f".11.json.{'a' * 31}.tmp", f".11.jsonx.{'a' * 32}.tmp", f".abc.json.{'a' * 32}.tmp",
    f".111.json.{'a' * 32}.tmp", f".11.json.{'A' * 32}.tmp", f".11.json.{'a' * 32}.tmp.bak",
    f".11.json.{'a' * 32}.tmp.old.tmp"])
def test_only_this_modules_stage_names_are_removed(seeded, name):
    path, _ = seeded
    (path / f".11.json.{'6' * 32}.tmp").write_text("{}")    # a real stage: triggers cleanup
    near_miss = path / name
    near_miss.write_text("not ours")
    wc.clean_dead_stages(path)
    assert near_miss.read_text() == "not ours"
    assert not (path / f".11.json.{'6' * 32}.tmp").exists()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX, non-root")
def test_a_stage_that_cannot_be_removed_is_a_warning(seeded, capsys):
    path, _ = seeded
    stuck = path / f".11.json.{'7' * 32}.tmp"
    stuck.mkdir()
    (stuck / "contents").write_text("{}")
    stuck.chmod(0o000)
    other = path / f".manifest.json.{'8' * 32}.tmp"
    other.write_text("{}")
    try:
        wc.clean_dead_stages(path)
        wc.save_cache(path, {"Q110": {"label": "Updated"}})
    finally:
        stuck.chmod(0o700)
    out = capsys.readouterr().out
    assert "cannot remove" in out and not other.exists()
    assert wc.load_cache(path)["Q110"]["label"] == "Updated"


def test_build_start_does_not_wait_for_a_busy_lock(seeded):
    import subprocess
    import time
    path, _ = seeded
    stage = path / f".11.json.{'9' * 32}.tmp"
    stage.write_text("{}")
    wc._prepare_cache_lock(path)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, sys, time\n"
        f"f = open({str(path / 'manifest.json.lock')!r}, 'a')\n"
        "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        t = time.monotonic()
        wc.clean_dead_stages(path)
        assert time.monotonic() - t < 2
        assert stage.exists()                    # left for the lock's holder
    finally:
        holder.kill()
        holder.wait()


def test_a_cache_it_cannot_lock_is_a_warning(seeded, monkeypatch, capsys):
    path, _ = seeded
    (path / f".11.json.{'b' * 32}.tmp").write_text("{}")

    def read_only(*a):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(wc, "_prepare_cache_lock", read_only)
    wc.clean_dead_stages(path)
    assert "cannot take the cache's lock" in capsys.readouterr().out


def test_a_bucket_it_must_add_to_but_cannot_read_stops_with_a_reason(seeded, capsys):
    path, entries = seeded
    (path / "11.json").write_text("{cut sho")
    with pytest.raises(json.JSONDecodeError):
        wc.save_cache(path, {"Q110": {"label": "Updated"}})
    out = capsys.readouterr().out
    assert "must add entries to" in out and "would drop the entries" in out
    assert (path / "11.json").read_text() == "{cut sho"


@pytest.mark.parametrize("bad", ["decode", "permission"])
def test_the_unreadable_bucket_warning_suits_the_cause(seeded, monkeypatch, capsys, bad):
    path, _ = seeded
    original = wc.json.load

    def failing(stream, *a, **k):
        if Path(stream.name).name == "99.json":
            if bad == "decode":
                raise json.JSONDecodeError("cut short", "{", 1)
            raise PermissionError(13, "Permission denied")
        return original(stream, *a, **k)

    monkeypatch.setattr(wc.json, "load", failing)
    wc.load_cache(path)
    out = capsys.readouterr().out
    if bad == "decode":
        assert "deleting it has its Q-IDs fetched again" in out
    else:
        assert "do not delete it" in out and "deleting it" not in out
