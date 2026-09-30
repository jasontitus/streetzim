# Handoff, 30 September 2026

Handoff from the lead session to the build-host session (ot-hel1). The user now directs the work from the host session.

## Where things stand

- **main = `00c5cbb`.** It holds the ops split (PR #20), the builder (PR #21) and the full-feature Zimfarm build (PR #22), with CI green on each. That keeps the promise to openZIM ("all on main by 9/29").
- **Production (ot-hel1)** is at `671c4fb` (#20's merge). Its round is paused, and the host never runs `git pull`.
- **`claude/adoring-dijkstra-i2vge7-next`** = main plus administrative-area search, up to commit `395f790`. That code passes gates.sh (1081 passed), but it is **not on main**. It needs the fixes below first.

## 1. Host tests (in progress)

Continue `ops/TESTING-NEXT.md`:
- §2d: D.C. with Wikipedia images.
- §2e: Switzerland head-to-head (maps2zim 0.2.1 against StreetZim full and basic).
- Then §2f Alaska and §2g Massachusetts.

Build the image at `870b29f`, whose tree is identical to main `00c5cbb`. The §1.2 ancestry STOPs are expected (branch bookkeeping) and can be ignored.

## 2. Admin-area search: finish before a PR to main

The last review of `395f790` found these, all still open:

- **MAJOR, antimeridian:** `_encloses` (`streetzim/admin_areas.py` ~851-860) compares raw boxes. A US box `[172,18,294,72]` then never encloses Alaska's children.
  - Fix: when `outer[2] > 180` and `inner[0] < outer[0]`, shift inner by +360.
  - Add a test.
- **MAJOR, performance:** Douglas-Peucker at 50 m leaves country and state rings at 10k-50k vertices, and `point_in_flat` is pure Python.
  - Add a per-ring bbox pretest.
  - Cap rings at level ≤ 4 at about 4k vertices, checking that Luxembourg still matches the unsimplified run.
  - Vectorise if that is easy.
  - Time the admin step on a big input (Belgium, or Texas from Geofabrik) against `729d510`.
- **MINOR, `region_of_name`:** apply the whole-polygon filter that `place_clipped` applies (pass `grid`).
- **MINOR, label and admin_centre areas:** prefer the region from `region_of_name` over grid parents.
- **MINOR, same-level node check:** exempt a holder with the same name or the same wikidata.

`admin-search-WIP-uncommitted.patch` is the stopped agent's partial, untested work on these items. It applies to `395f790`: use it or drop it.

When they're done, run gates.sh, the node tests, the frozen D.C. ADM check (expect 4/4) and the Monaco checks. Then open a PR from `-next` to main.

## 3. Documents for the user (in this folder)

- `openzim-reply-draft.md`: the reply to openZIM's review. The user posts it; nobody else posts to openZIM.
- `head-to-head-maps2zim-dc.md`: the D.C. comparison write-up, corrected after the second review. The Switzerland results are to be added.
- `openzim-maps-area-pattern.patch` and `-PR.md`: the maps2zim regex fix, which the user files from a fork.

## Rules that still apply

- Change production only with the user's approval.
- Stop processes by PID with SIGTERM only.
- Never prune Docker.
- Wikimedia pacing defaults to 120 requests a minute (`STREETZIM_WIKI_MAX_PER_MIN`).
