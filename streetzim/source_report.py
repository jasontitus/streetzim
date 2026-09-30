"""What each external data source delivered to this build, in one line.

The builder notes each source as it finishes with it (`note`); `streetzim`
prints `summary()` at the end, e.g.

    sources: Overture addresses 4087 rows (4012 added); Overture places 36
    enriched, 3028 added; Wikipedia titles 0/95 Q-IDs resolved; Wikidata
    45 entries; Wikipedia articles 16/53 (37 not fetched, 37 rate-limited)

Failure policy (docs/zimfarm.md): Overture failing is fatal, so a ZIM that
says it has Overture data has it. Wikidata and Wikipedia degrade: what the
APIs did not answer is left out, the ZIM's flags and License describe what
it holds, and this line says how much was missed.
"""
from __future__ import annotations

REPORT: dict[str, str] = {}


def note(source: str, text: str) -> None:
    REPORT[source] = text


def reset() -> None:
    REPORT.clear()


def summary() -> str:
    return "sources: " + ("; ".join(f"{k} {v}" for k, v in REPORT.items())
                          or "none besides OSM")
