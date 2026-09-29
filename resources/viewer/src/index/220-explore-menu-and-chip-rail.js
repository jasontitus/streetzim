// --- Explore menu ---
// Floating top-right pill. Click to expand a list of common chips;
// click a chip to navigate to places.html with that chip preselected
// + map-area toggle on. Hidden when the carousel is up so it doesn't
// fight that affordance for screen real estate.
//
// Chip list is hard-coded here (mirrors a subset of places.html
// CATEGORIES). The build still emits chip files keyed by these IDs;
// regions without a particular category just show "0 matches" on
// places.html — graceful degrade.
// Chip IDs MUST match the build's cloud/chip_rules.py —
// tests/chip_rules_js.test.mjs fails CI when they drift. Used by both
// initFindChips (on-map rail) and initExplore (corner menu).
// BEGIN chip-rail  (ids + labels must equal cloud/chip_rules.py CHIP_RULES;
// enforced by tests/chip_rules_js.test.mjs)
var EXPLORE_CHIPS = [
  // Must cover every chip cloud/chip_rules.py can emit, or the map rail
  // silently drops categories the ZIM contains: Health, Landmarks and
  // Libraries were missing here while present in every build and in
  // places.html, so the map offered two food chips and no hospitals.
  //
  // Restaurants + Cafés merged into Food & Drink 2026-09-16; ZIMs built
  // before that ship chip-restaurants.json + chip-cafes.json and no
  // chip-food.json. The rail still lists only the merged chip.
  // _findChipsReconcile() hides the chips the manifest does not list (or
  // lists with count 0), keeping Food & Drink when either old file exists.
  // ONE food chip on the rail. A pre-merge ZIM has no chip-food.json, so
  // _findResolveChipDef() maps this button onto that ZIM's restaurants +
  // cafes and loadChipOnMap merges them — the rail reads the same whatever
  // vintage the ZIM is, instead of showing two food buttons on a
  // retrofitted region and one on a freshly built one.
  { id: 'food',        label: 'Food & Drink', emoji: '🍴' },
  { id: 'bars',        label: 'Bars',        emoji: '🍺' },
  { id: 'shops',       label: 'Shops',       emoji: '🛍️' },
  { id: 'health',      label: 'Health',      emoji: '🏥' },
  { id: 'museums',     label: 'Museums',     emoji: '🏛️' },
  { id: 'landmarks',   label: 'Landmarks',   emoji: '🗽' },
  { id: 'libraries',   label: 'Libraries',   emoji: '📚' },
  { id: 'parks',       label: 'Parks',       emoji: '🌳' },
  { id: 'fuel',        label: 'Gas',         emoji: '⛽' },
  { id: 'hotels',      label: 'Hotels',      emoji: '🏨' },
];
// END chip-rail

// --- On-map Find UI: chip rail beneath the search input ---
// Tapping a chip fetches its category-index file (with sub-bucket
// fan-out when split), filters to the current viewport, and drops
// pins + carousel directly on the map via the existing
// renderFindResultsFromStash path. No navigation to places.html.
//
// Cache lives in module scope so a quick second tap on the same
// chip doesn't re-fetch the same MB+ JSON.
