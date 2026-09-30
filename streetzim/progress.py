"""Zimfarm progress file (--stats-filename).

Zimfarm reads a small JSON file while a task runs and shows it as a progress
bar: {"done": N, "total": M}, the same shape maps2zim writes. The builder
already announces its phases as "[3/9] Building search index..." through
streetzim.common.print; StatsFile listens to those announcements, so no
build step needs to know about it.

Progress is per phase, so the bar moves in steps (6-10 per build) rather than
smoothly. Every write replaces the file atomically, so a reader never sees a
half-written file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from streetzim import common


class StatsFile:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.done = 0
        self.total = 1
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, done: int, total: int) -> None:
        # Never go backwards, and never claim more than the total.
        self.total = max(int(total), 1)
        self.done = min(max(int(done), self.done), self.total)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"done": self.done, "total": self.total}, indent=2))
        os.replace(tmp, self.path)

    def on_phase(self, number: int, total: int, title: str) -> None:
        """Phase N of M is starting, so N-1 phases are done."""
        self.write(number - 1, total)

    def attach(self) -> StatsFile:
        common.PHASE_LISTENERS.append(self.on_phase)
        return self

    def detach(self) -> None:
        if self.on_phase in common.PHASE_LISTENERS:
            common.PHASE_LISTENERS.remove(self.on_phase)

    def finish(self) -> None:
        self.write(self.total, self.total)
