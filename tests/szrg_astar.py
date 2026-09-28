"""Moved to streetzim/routing/astar.py. This alias keeps
``tests.szrg_astar`` importable (same module object, so monkeypatching and
private names behave exactly as before)."""
import sys

from streetzim.routing import astar as _module

sys.modules[__name__] = _module
