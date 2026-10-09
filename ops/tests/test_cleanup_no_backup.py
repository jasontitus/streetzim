"""cloud/cleanup_old_zims.py deletes pruned ZIMs without archive.org's
backup: with it, every keep-2 prune left the deleted ZIM under
history/files/ for good (2.1 TB of them by 2026-10-09)."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("cleanup_old_zims", ROOT / "ops" / "cloud" / "cleanup_old_zims.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prune_deletes_without_a_backup(monkeypatch):
    m = _load()
    calls = []
    monkeypatch.setattr(m, "item_zim_files", lambda item: [
        {"date": "2026-09-14", "name": "osm-x-2026-09-14.zim"},
        {"date": "2026-09-20", "name": "osm-x-2026-09-20.zim"},
        {"date": "2026-10-07", "name": "osm-x-2026-10-07.zim"}])
    monkeypatch.setattr(m, "torrent_protected_name", lambda item: None)
    monkeypatch.setattr(m, "ia", lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    assert m.prune("streetzim-x", keep=2, dry_run=False) == (2, 1)
    assert calls == [["delete", "--no-backup", "-H", "x-archive-keep-old-version:0",
                      "streetzim-x", "osm-x-2026-09-14.zim"]]


def test_a_dry_run_deletes_nothing(monkeypatch):
    m = _load()
    calls = []
    monkeypatch.setattr(m, "item_zim_files", lambda item: [
        {"date": "2026-09-14", "name": "a.zim"}, {"date": "2026-09-20", "name": "b.zim"},
        {"date": "2026-10-07", "name": "c.zim"}])
    monkeypatch.setattr(m, "torrent_protected_name", lambda item: None)
    monkeypatch.setattr(m, "ia", lambda args: calls.append(args))
    m.prune("streetzim-x", keep=2, dry_run=True)
    assert calls == []
