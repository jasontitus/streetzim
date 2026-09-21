# The in-ZIM viewer on iOS Kiwix: three bugs, and why no gate caught them

All three shipped to users. None was visible to `validate_zim.py`, the
overlap check, the browser smoke, or the Kiwix gate — and none reproduces in
headless Chromium at any viewport. Every one was found by instrumenting the
viewer inside a real ZIM on a real iPhone.

## 1. Content under the status bar / Dynamic Island

Kiwix serves the in-ZIM viewer from a custom **`zim:` URL scheme**. WKWebView
gives a custom-scheme context **no safe-area insets**, so
`env(safe-area-inset-top)` is hard `0` — while the web view itself spans the
whole screen. Top chrome at `top: 10px` therefore rendered underneath the
clock.

Measured in-ZIM, iPhone 16 Pro Max / iOS 18.7:

```
env top (probe): 0.0      --top-inset: 0px
search top: 10.0          search bottom: 62.0
innerHeight: 956  ===  screen.height: 956  ===  docEl.clientHeight: 956
location.protocol: zim:   mm browser: true   nav.standalone: false
```

`innerHeight === screen.height` is the proof the view is full-screen; `env`
reading 0 is the proof the platform will not tell us about the cutout. Three
CSS paths existed for this (`:root`, `@media (display-mode: standalone)`,
`html.sz-standalone`) and **all three computed to zero**. `sz-standalone` was
dead code besides — nothing in the tree ever added that class.

**Fix** (`resources/viewer/index.html`, head): derive the inset when the
platform refuses to report it.

- **Trigger is measured**: `zim:` scheme + coarse pointer + `env()` zero +
  `innerHeight === screen.height`. Only the *magnitude* is estimated.
- **Magnitude is classified by aspect ratio**, not a table of screen heights:
  `hi/lo >= 1.95` catches every notch / Dynamic Island / punch-hole phone
  including unreleased ones; 16:9 legacy is 1.78, tablets ~1.33. An
  enumerated table silently mis-handles any device not listed.
- Android (28/24) and desktop Kiwix (excluded via coarse-pointer — it serves
  `zim:` too and can be maximised to exactly `screen.height`) are handled.
- Landscape is tested **before** the full-screen test: iOS keeps
  `screen.height` at the portrait value, so a landscape view always looks
  "inset" by height and would otherwise lose its bottom inset.
- If a future Kiwix does propagate insets, `env()` reads non-zero and the
  shim **removes its own overrides** so the stylesheet wins. No double-count.

After, same device: `--top-inset: 62px`, `search top: 72.0`.

## 2. Place popups painted under the search UI

`.maplibregl-popup` had **no z-index rule anywhere in the file**, so popups
painted at default stacking — below `#search-container` (`z-index: 2`). The
place card's title, its icon row, its "Directions to here" button and its
close **X** all rendered underneath the search box and the chip rail.

This is almost certainly also the 2026-09-19 report *"there's no way to close
that because the little closing box is not visible, so there's no X to
click on"*. The X was never missing; it was underneath the chips.

**Fix**: `.maplibregl-popup { z-index: 4 }` — clears `#search-container` and
`#attr-btn` (both 2), far below the modal overlays (100 / 1500+).

## 3. Chip taps were cancelled, not dead

Reported as "chips aren't clickable or swipeable". Device trace after one tap
and one swipe:

```
touchstart 1   touchmove 7   pointercancel 1
chip click handler fired: 0   loadChipOnMap called: 0   chip.on: 0
scrollLeft max: 590            (so the rail DOES scroll)
```

The tap reaches the chip and iOS then cancels it: on an `overflow-x` scroller
a tap that drifts a few px is treated as the start of a scroll, `pointercancel`
fires and `click` is never delivered.

**Fix**, two parts:
- `touch-action: pan-x` on `#find-chips`, so vertical drift is no longer a
  candidate scroll gesture.
- A `touchend` tap fallback (≤ 10 px drift, ≤ 600 ms) that invokes the chip's
  action directly. The action moved into a named function that both the click
  listener and the fallback call, with a 500 ms guard against double-firing.

## 3b. Popups opened flush against the chrome

Fixed by #2 for painting, but a popup anchors *above* its marker, so it still
opened with its top edge inside the chip row. `_szPopupGap()` hooks the
popup's `open` event, measures the card against `#search-container`'s bottom
**inside `requestAnimationFrame`** (measuring before paint reads the
pre-position), and pans by exactly the shortfall plus 10 px. It no-ops when
the card already clears.

`panBy` positive-y moves features **up**, so the delta is negated — getting
that backwards pushes the card further under the chrome.

## The gate that was missing

`tmp/device-matrix.mjs` runs the shipped viewer at seven real iPhone logical
viewports (375x667 … 440x956) with `--top-inset` forced to what the shim
derives, and asserts what nothing else did:

- every control is hit-testable **at its own centre** via `elementFromPoint`
  — "covered by an invisible element" is precisely what a user means by
  "not clickable", and no structural validator can see it
- a synthetic `.maplibregl-popup` must out-paint `#search-container`
- `touch-action: auto` on the chip rail is a hard fail
- chips render, are hit-testable, and activate (`.on`)
- the rail scrolls; search returns rows; the first row is hit-testable
- nothing renders above the inset or off the sides; no pairwise overlap

## Four wrong identifiers, and what they cost

A gate that asserts the wrong identifier does not fail loudly. It passes or
fails for reasons unrelated to the product — it **manufactures evidence**.

| asserted | actual | presented as |
|---|---|---|
| kiwix book = catalog `<name>` | **filename stem** | 404 on every fetch → canvas never appeared → read as a 90 s timeout, blamed on I/O load and then on the shim |
| chip active = `.active` | `.on` | "chip tap did nothing" |
| results = `#search-results li` | `div.search-result` | "search returned 0" on all 7 devices |
| `pgrep -f "[u]pload_validated.sh switzerland"` | matched the **wrapper shell**, whose cmdline contains the whole script text | "switzerland is uploading, leave it alone" |

Two were reported as findings before being caught. The counter-practice:
read the identifier out of the source first, and **print the discriminator
next to the result** — `idx=200` beside each device row is what finally
exposed the 404.

## Other traps hit

- **Kiwix dedupes books by UUID.** `patch_viewer_inplace.py` preserves the
  UUID by design (a patched region is an *update*). So a test build made by
  `cp old.zim new.zim && patch` shows the **old** viewer on the device even
  though the user downloaded the new URL. Twice this looked like the fix
  failing. For anything the user must verify: mint a fresh UUID with
  `swap_viewer_rust.py`, assert the UUIDs differ, and carry a visible BUILD
  stamp.
- **In-place patching is destructive** — it overwrites the only copy, so
  `verify_slot_integrity.py` (source vs patched) cannot run afterwards.
- **Locale-dependent sort.** `ls | sort | tail -1` returned
  `osm-switzerland-2026-09-20.zim` rather than `…-20c.zim`: the default
  locale ignores punctuation, so `…20b` collates before `…20`. Use
  `LC_ALL=C sort` or `ls -t`. Cost one needless 10-minute re-pack, and left
  three switzerland ZIMs in the item (prune pending).
- **Diagnostics can destroy their own evidence.** The first chip diag kept a
  rolling 14-entry event log; scroll-bounce flushed the touch events it
  existed to capture. Count by type instead.

## Rollout

`.allzims-v2.sh` picks the cheap path per region: a build that already has
slots is patched in place (~10 s on 7 GB) and keeps its UUID; anything else
gets a full `swap_viewer_rust` re-pack, which also adds slots for next time.
Gates before every upload, no exceptions: markers read back **out of the
archive** (all six fix markers present, no diag panel leaked), libzim
`check()`, overlap at 320/390/430, the in-ZIM Kiwix gate, and the device
matrix.

`port busy` and `kiwix-serve never came up` are logged explicitly as **not**
geometry failures — that ambiguity previously cost two regions their upload
slot when leaked `kiwix-serve` processes squatted on the gate ports.
