"""Suite-wide configuration.

Two tests replay golden routes through a real ZIM after rebuilding its graph:
test_spatial_chunking.py's spatial split (in memory) and test_v5_end_to_end.py's
v5 conversion. Both run once per golden corpus, and four corpora have their
source ZIM on this host. Measured 2026-09-27/28, both green:

    spatial split   43m39s  baltics 1720 s, colorado 739 s, sv 129 s, hisp 31 s
    v5 conversion   16m04s  colorado 433 s, baltics 332 s, sv 158 s, hisp 42 s

Each is 4 passed, 3 skipped — the two japan corpora and washington-dc have no
source ZIM on this host.

So `pytest tests/` needed an hour, and it ran past a 40-minute timeout twice
and reported nothing at all. A suite nobody can finish is a suite nobody runs,
which is worse than one that defers its heaviest checks. Without these two the
rest takes 32 seconds.

So it is marked `slow` and deselected by default. Run it deliberately:

    pytest tests/ --runslow
    STREETZIM_RUNSLOW=1 pytest tests/test_spatial_chunking.py

They are the checks to run before touching routing, the v5 format or the
spatial format — not on every edit. Nothing else in the suite is marked slow.

Note that one test fails at HEAD independently of any of this:
test_validator_regression.py::test_known_good_sv_structural_checks_pass, on
terrain_edge_stripe against the known-good Silicon Valley baseline. It has
failed since before 2026-09-15 and is not related to the slow markers.
"""
import os

import pytest


def pytest_addoption(parser):
    parser.addoption("--runslow", action="store_true", default=False,
                     help="also run tests marked slow (real-ZIM routing identity)")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: minutes-long; needs a real ZIM. Opt in with --runslow.")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow") or os.environ.get("STREETZIM_RUNSLOW") == "1":
        return
    skip = pytest.mark.skip(reason="slow: run with --runslow or STREETZIM_RUNSLOW=1")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
