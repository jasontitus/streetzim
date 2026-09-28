"""Moved to streetzim/routing/reader.py. This alias keeps
``tests.szrg_reader`` importable (same module object, so monkeypatching and
private names behave exactly as before)."""
import sys

from streetzim.routing import reader as _module

sys.modules[__name__] = _module
