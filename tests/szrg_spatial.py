"""Moved to streetzim/routing/spatial.py. This alias keeps
``tests.szrg_spatial`` importable (same module object, so monkeypatching and
private names behave exactly as before)."""
import sys

from streetzim.routing import spatial as _module

sys.modules[__name__] = _module
