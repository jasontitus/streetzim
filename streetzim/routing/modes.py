"""Travel modes: which edges each may use and what they cost.

One table for the Python readers; resources/viewer/routing-worker.js
(``edgeCostFor``) mirrors it exactly, and tests compare the two.

Edge bits (class_access; docs/formats.md): 0-4 class ordinal, 5 no
walking, 6 no cycling, 9 no cars, 10 a record against a one-way (speed
0), 12 push the bike, 13 cycle lane, 14 cycle track, 15-16 surface (1
paved, 2 firm, 3 rough), 17 no sidewalk, 18 no walking in this direction.

``edge_cost`` returns (cost_s, time_s): the search minimises cost, the
route shows time. Every multiplier is >= 1 and every speed is at most
the mode's ``HEURISTIC_KPH``, so the straight-line heuristic stays
admissible.
"""
from __future__ import annotations

MODES = ("drive", "walk", "bike")
HEURISTIC_KPH = {"drive": 100.0, "walk": 5.0, "bike": 18.0}

NO_MOTOR_BIT = 0x200
NO_MOTOR_ORD_MIN, NO_MOTOR_ORD_MAX = 16, 20
_MOTORWAY = (1, 2)
_TRUNK = (3, 4)
_PRIMARY = (5, 6)
_SECONDARY = (7, 8)
_TRACK, _PATH, _STEPS = 15, 16, 20

WALK_KPH = 5.0
WALK_STEPS_KPH = 2.5
WALK_ROUGH_KPH = 4.5
BIKE_PAVED_KPH = 18.0
BIKE_FIRM_KPH = 14.0
BIKE_ROUGH_KPH = 8.0
BIKE_TRACK_KPH = 12.0          # track / path of unknown surface
BIKE_PUSH_KPH = 4.0
BIKE_PUSH_PENALTY = 1.5
BIKE_STEPS_PENALTY = 3.0        # carrying the bike


def is_no_motor(ca: int) -> bool:
    if ca & NO_MOTOR_BIT:
        return True
    return NO_MOTOR_ORD_MIN <= (ca & 0x1F) <= NO_MOTOR_ORD_MAX


def edge_cost(mode: str, speed_dist: int, ca: int) -> tuple[float, float] | None:
    """(cost_s, time_s) of an edge for `mode`, or None if not allowed."""
    dist_m = (speed_dist & 0xFFFFFF) / 10.0
    ordv = ca & 0x1F
    if mode == "drive":
        speed = speed_dist >> 24
        if speed == 0 or is_no_motor(ca):
            return None
        t = dist_m / (speed / 3.6)
        return t, t
    surface = (ca >> 15) & 3
    no_sidewalk = bool(ca & 0x20000)
    if mode == "walk":
        if ca & 0x20 or ca & 0x40000 or ordv in _MOTORWAY:
            return None
        kph = (WALK_STEPS_KPH if ordv == _STEPS
               else WALK_ROUGH_KPH if surface in (2, 3) else WALK_KPH)
        t = dist_m / (kph / 3.6)
        mult = 1.0
        if ordv in _TRUNK:
            mult = 1.5
        if no_sidewalk and ordv in _TRUNK + _PRIMARY + _SECONDARY:
            mult *= 1.5
        return t * mult, t
    if mode == "bike":
        if ca & 0x40 or ordv in _MOTORWAY:
            return None
        if ca & 0x1000 or ordv == _STEPS:
            t = dist_m / (BIKE_PUSH_KPH / 3.6)
            return t * (BIKE_STEPS_PENALTY if ordv == _STEPS else BIKE_PUSH_PENALTY), t
        if surface == 3:
            kph = BIKE_ROUGH_KPH
        elif surface == 2:
            kph = BIKE_FIRM_KPH
        elif surface == 0 and ordv in (_TRACK, _PATH):
            kph = BIKE_TRACK_KPH
        else:
            kph = BIKE_PAVED_KPH
        t = dist_m / (kph / 3.6)
        mult = 1.0
        if not ca & 0x6000:                       # no cycle lane or track
            if ordv in _TRUNK:
                mult = 1.6
            elif ordv in _PRIMARY:
                mult = 1.4
            elif ordv in _SECONDARY:
                mult = 1.2
        return t * mult, t
    raise ValueError(f"unknown travel mode {mode!r}; one of {', '.join(MODES)}")
