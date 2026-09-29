"""Moved to streetzim/routing/spatial_astar.py. This alias keeps
``tests.szrg_spatial_astar`` importable (same module object, so monkeypatching and
private names behave exactly as before)."""
import sys

from streetzim.routing import spatial_astar as _module

sys.modules[__name__] = _module
