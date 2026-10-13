"""Every streetzim-* listing asks archive.org for up to 1000 items and stops
when archive.org says more matched (numFound) than it listed; a region past
the cap would otherwise drop out silently (the cap was 100, with 78 items,
until 2026-10-13). The pruner's `ia search` takes no rows parameter, which
would make internetarchive list one page only."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(monkeypatch, mod, found, listed):
    docs = [{"identifier": f"streetzim-r{i}"} for i in range(listed)]
    resp = {"response": {"docs": docs}}
    if found is not None:
        resp["response"]["numFound"] = found
    seen = {}

    def urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return io.BytesIO(json.dumps(resp).encode())
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    return seen


@pytest.mark.parametrize("found, listed, ok", [(78, 78, True), (101, 100, False), (None, 3, True)])
@pytest.mark.parametrize("which", ["torrents", "stats"])
def test_a_listing_cut_short_stops(monkeypatch, which, found, listed, ok):
    if which == "torrents":
        mod = _load("ops/cloud/generate_all_torrents.py", "generate_all_torrents_t")
        call = mod.list_streetzim_items
    else:
        mod = _load("web/stats.py", "stats_t")
        call = mod.fetch_all_items
    seen = _stub(monkeypatch, mod, found, listed)
    if ok:
        assert len(call()) == listed
    else:
        with pytest.raises(SystemExit, match=f"listed {listed} of {found} streetzim"):
            call()
    assert "&rows=1000&" in seen["url"]


def test_the_pruner_lists_every_page(monkeypatch):
    mod = _load("ops/cloud/cleanup_old_zims.py", "cleanup_old_zims_t")
    calls = []

    class Done:
        returncode = 0
        stdout = '{"identifier": "streetzim-a"}\n{"identifier": "streetzim-b"}\n'
        stderr = ""
    monkeypatch.setattr(mod, "ia", lambda args: calls.append(args) or Done())
    assert mod.list_all_streetzim_items() == ["streetzim-a", "streetzim-b"]
    # Any --parameters/-p (rows, page) makes internetarchive send one request.
    assert not any(a == "-p" or a.startswith(("--parameters", "-p=")) or "rows" in a
                   for a in calls[0]), calls[0]
