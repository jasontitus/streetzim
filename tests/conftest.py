"""Suite-wide configuration.

Two tests replay golden routes through a real ZIM after rebuilding its graph:
test_spatial_chunking.py's spatial split (in memory) and test_v5_end_to_end.py's
v5 conversion. Both run once per golden corpus, and four corpora have their
source ZIM on this host. Measured on 2026-09-27: `pytest tests/` ran past a
40-minute timeout twice and never reported; with the spatial one deferred it
finished in 19 minutes, 17 of which were the four v5 corpora (colorado alone
502 s). A suite nobody can finish is a suite nobody runs, which is worse than
one that defers its heaviest checks. Without them the rest takes ~2 minutes.

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
