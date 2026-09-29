"""cloud/finish_pending_uploads.sh drops a pending row once archive.org lists a
LATER build of the same region.

Completing a superseded row rebuilds web/torrents/<id>.torrent from the older
file, which makes the site drop the region's Torrent button, and then
cleanup_old_zims --keep 2 deletes the very file the row was about (observed
2026-09-27, southeast-asia 09-17 behind 09-20). The rule is a Python snippet
inside the shell script; these tests run that snippet, taken from the script
itself, on sample `ia metadata` output.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cloud" / "finish_pending_uploads.sh"


def _snippet() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"python3 -c '\n(.*?)\n' \"\$zim\"", text, re.S)
    assert m, "the superseded check moved or changed shape; update this test"
    return m.group(1)


def _state(want: str, names: list[str]) -> tuple[str, str]:
    meta = json.dumps({"files": [{"name": n} for n in names] + [{"source": "x"}]})
    out = subprocess.run([sys.executable, "-c", _snippet(), want], input=meta,
                         capture_output=True, text=True, check=True).stdout
    listed, _, newer = out.rstrip("\n").partition("\t")
    return listed, newer


def test_not_superseded_when_it_is_the_newest():
    assert _state("osm-japan-2026-09-19.zim",
                  ["osm-japan-2026-09-12.zim", "osm-japan-2026-09-19.zim"]) == ("yes", "")


def test_not_listed_yet():
    assert _state("osm-japan-2026-09-19.zim", ["osm-japan-2026-09-12.zim"]) == ("no", "")


def test_same_day_letter_suffix_supersedes():
    assert _state("osm-japan-2026-09-19.zim",
                  ["osm-japan-2026-09-19.zim", "osm-japan-2026-09-19b.zim"]) \
        == ("yes", "osm-japan-2026-09-19b.zim")


def test_newest_by_date_not_by_listing_order():
    # archive.org lists files in no particular order: take the newest stamp,
    # not the last entry.
    names = ["osm-japan-2026-09-25.zim", "osm-japan-2026-09-20.zim",
             "osm-japan-2026-09-22b.zim", "osm-japan-2026-09-19.zim"]
    assert _state("osm-japan-2026-09-19.zim", names) == ("yes", "osm-japan-2026-09-25.zim")


@pytest.mark.parametrize("want,names", [
    ("osm-japan-2026-09-19.zim", ["osm-japan-v3.zim", "osm-japan-2026-09-19.zim", "README.md"]),
    ("osm-japan-v3.zim", ["osm-japan-v3.zim", "osm-japan-2026-09-25.zim"]),
])
def test_names_without_a_date_stamp_are_ignored(want, names):
    listed, newer = _state(want, names)
    assert listed == "yes" and newer == ""
