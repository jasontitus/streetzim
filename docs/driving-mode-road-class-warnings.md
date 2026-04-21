# Road-class warnings for Walk / Bike driving mode

Design notes for the deferred feature: flag the user when they enter
Walk or Bike mode on a planned route that includes roads unsuitable for
that travel mode (e.g. an Interstate motorway).

Status: **not implemented**. This doc captures what would need to change
so it can be picked up later without re-deriving the plan.

---

## Why

The router in `create_osm_zim.py` uses a single graph with car speeds.
When a user asks for a route and hits **Walk** or **Bike**, the follow-
mode will happily lead them along a motorway / trunk road. That's at
minimum unpleasant and in many jurisdictions illegal (interstates in the
US, Autobahns in DE). We want a friendly up-front warning:

> *"This route uses motorway segments — not safe for walking."*

A full fix (separate walk/bike routing profiles) is out of scope; this
note covers only the **warning** pass, which is small and additive.

---

## Current state of the routing graph

Baked by `create_osm_zim.py::build_routing_graph` (around line 1720+),
written as **SZRG v3** (`resources/viewer/index.html::initRouting`
parses it around line ~2370+). Per-edge record:

```
struct EdgeV3 {
    uint32_t target;        // destination node index
    uint32_t dist_speed;    // (speed_kmh << 24) | dist_dm_24bit
    uint32_t geom_idx;      // 0xFFFFFFFF = no geometry
    uint32_t name_idx;      // index into name blob
};                          // 16 bytes
```

There is **no road-class field**. The baker knows the class at edge
construction time (`SPEED` dict keyed on the OSM `highway=*` tag, line
~1751), but throws it away after resolving the speed.

---

## Proposed format — SZRG v4

Add one u32 per edge:

```
struct EdgeV4 {
    uint32_t target;
    uint32_t dist_speed;    // unchanged
    uint32_t geom_idx;      // unchanged
    uint32_t name_idx;      // unchanged
    uint32_t class_access;  // NEW
};                          // 20 bytes
```

Bit layout of `class_access` (little-endian u32):

| bits   | meaning                                                    |
|--------|------------------------------------------------------------|
| 0..4   | road class ordinal (see table below) — 5 bits, 32 values   |
| 5..7   | access flags (bit 5=no-foot, bit 6=no-bicycle, bit 7=oneway) |
| 8..31  | reserved — zero-fill, room for future use                  |

Road-class ordinals (same 16 classes the speed table uses, packed):

| ord | class          | car ok | foot ok | bicycle ok |
|-----|----------------|:------:|:-------:|:----------:|
|  0  | unknown        |   ✓    |    ✓    |     ✓      |
|  1  | motorway       |   ✓    |    ✗    |     ✗      |
|  2  | motorway_link  |   ✓    |    ✗    |     ✗      |
|  3  | trunk          |   ✓    |    ⚠    |     ⚠      |
|  4  | trunk_link     |   ✓    |    ⚠    |     ⚠      |
|  5  | primary        |   ✓    |    ✓    |     ✓      |
|  6  | primary_link   |   ✓    |    ✓    |     ✓      |
|  7  | secondary      |   ✓    |    ✓    |     ✓      |
|  8  | secondary_link |   ✓    |    ✓    |     ✓      |
|  9  | tertiary       |   ✓    |    ✓    |     ✓      |
| 10  | tertiary_link  |   ✓    |    ✓    |     ✓      |
| 11  | residential    |   ✓    |    ✓    |     ✓      |
| 12  | living_street  |   ✓    |    ✓    |     ✓      |
| 13  | unclassified   |   ✓    |    ✓    |     ✓      |
| 14  | service        |   ✓    |    ✓    |     ✓      |
| 15  | track          |   ✓    |    ✓    |     ✓      |
| 16  | path           |   ✗    |    ✓    |     ✓      |
| 17  | footway        |   ✗    |    ✓    |     ⚠      |
| 18  | cycleway       |   ✗    |    ✓    |     ✓      |
| 19  | pedestrian     |   ✗    |    ✓    |     ⚠      |
| 20  | steps          |   ✗    |    ✓    |     ✗      |
| 21..31 | reserved    |        |         |            |

Legend: ✓ allowed, ✗ forbidden, ⚠ allowed but discouraged / warn.

The access bits (5..7) are explicit overrides derived from OSM tags
(`foot=no`, `bicycle=no`, `oneway=yes`) and take precedence over the
class defaults when set.

---

## Baker changes (`create_osm_zim.py`)

Near the existing `SPEED` dict at line ~1751, add a parallel
`CLASS_ORDINAL = {"motorway": 1, "motorway_link": 2, ...}` lookup.

In the edge-building pass that currently computes `speed` and appends to
`edges_dist_speed` (lines ~2040–2060), also:

```python
class_ord = CLASS_ORDINAL.get(highway_tag, 0)
access = 0
if tags.get("foot") == "no":     access |= 0x20  # bit 5
if tags.get("bicycle") == "no":  access |= 0x40  # bit 6
if tags.get("oneway") == "yes":  access |= 0x80  # bit 7
edges_class_access.append(class_ord | access)
```

Serialize a new column after `name_idx` in the edges array, and bump
the version byte in the header write (line ~2131) from `3` to `4`.

The per-edge overhead is **4 bytes × numEdges**. For a US-wide graph
with ~50M edges that's ~200 MB — meaningful but not blocking. If the
size matters, an alternative is to pack the 5-bit class into the top
bits of `name_idx` (which rarely exceeds 2^24 distinct names) and keep
the record at 16 bytes; this doc picks the simpler u32 column for
clarity.

---

## Viewer reader changes (`resources/viewer/index.html::initRouting`)

The binary parser at ~line 2370 already branches on the version byte.
Add a v4 branch:

```js
if (version === 4) {
  // Same as v3 but edges are 5 u32 per record instead of 4.
  edgeStride = 5;
  hasClassAccess = true;
}
```

Add an accessor:

```js
function edgeClassOrdinal(i) {
  return edges[i * edgeStride + 4] & 0x1F;
}
function edgeAccessFlags(i) {
  return (edges[i * edgeStride + 4] >> 5) & 0x07;
}
```

In `findRoute` (line ~2620), inside the reconstruction loop that walks
`prevEdge[n]`, collect per-edge class ordinals alongside the existing
`segRev`:

```js
segRev.push({ nameIdx, distM, classOrd: edgeClassOrdinal(ei),
              access: edgeAccessFlags(ei) });
```

Then emit a `classes` summary on the returned route object:

```js
return {
  coords, distance, time, roads,
  classes: summarizeClasses(segRev)
};

// returns { totalByClass: { 1: 4500, 5: 800, ... }, worst: 1, hasForbidden: {foot: true, bicycle: true} }
```

---

## Driving-mode UI changes (`resources/viewer/index.html`)

At the top of `driveMode.enter(mode)` (around line ~3410), after
resolving the mode preset, check the route's class summary:

```js
var warn = checkRouteForMode(lastRoute, state.mode);
if (warn) {
  setStatus(warn);      // yellow banner already in the HUD
}
```

Where `checkRouteForMode` returns a human string or null:

```js
function checkRouteForMode(route, mode) {
  if (!route.classes) return null;
  if (mode === 'walk' && route.classes.hasForbidden.foot) {
    return 'This route uses motorway / link roads — unsafe for walking.';
  }
  if (mode === 'bike' && route.classes.hasForbidden.bicycle) {
    return 'This route uses motorway / link roads — unsafe for biking.';
  }
  return null;
}
```

Optional niceties (small, additive):

- Also surface the warning on the routing panel at route-compute time,
  so the user sees it before they commit to a mode.
- Add a "Suggest an alternative" button that reruns `findRoute` with a
  higher cost multiplier on forbidden classes for the active mode. This
  is real work (A* weight overrides) but stays inside the viewer.

---

## Migration

Old ZIMs ship SZRG v3 → new viewer must keep reading v3 (no class info,
warnings silently skipped — acceptable fallback). The version byte in
the header already gates this cleanly.

New ZIMs ship v4 → old viewers (SZRG v3-only) will refuse to load the
graph. Acceptable since routing is gated behind a feature check in the
viewer config anyway. If we want graceful degradation on mixed versions,
add a v3-compatible writer mode behind a `--routing-graph-version` CLI
flag in `create_osm_zim.py`.

---

## Scope estimate

- Baker change: ~40 lines (dict + one column + version bump)
- Reader change: ~30 lines (v4 branch + two accessors + classes summary)
- UI warning:    ~20 lines (banner string + mode check)
- Tests:         regenerate a small-region ZIM, verify warnings fire for
                 a known motorway-traversing route

Rebuild cost: one ZIM per region (same as any graph change).
