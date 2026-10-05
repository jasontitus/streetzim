# Building a StreetZim in another language

`create_osm_zim.py --language fr` (or `streetzim --language fr`, a choice on
Zimfarm) builds the map for French readers. Codes are ISO 639-1; the list
is `streetzim/languages.py`. A `--name` with a language segment
(`osm_fr_luxembourg`) must agree with `--language`.

## What is in the language

| part | how |
|---|---|
| Map labels | OSM's `name:fr`, else the place's own name (`name_int` / `name:latin` as the tile profile writes them). The tilemaker profile adds `name:fr` to the tiles when `STREETZIM_TILE_LANGUAGES` asks for it, which the build sets. The viewer reads map-config `language` (`szLabelField` / `szLabelOf`). |
| Search | The record's `n` is the same name as the label; `nn` is the place's own name and `nl` the English one when they differ, both indexed ([search-records.md](search-records.md)). |
| Kiwix pages | Titled with `n` (and the place's own name after a "·"). |
| Wikipedia articles | Titles resolved to French Wikipedia through Wikidata; articles fetched from fr.wikipedia.org (or a French Wikipedia ZIM given as `--wiki-articles-source`), cached under `<cache>/lang/fr`. |
| Wikidata | Labels and descriptions in French first, frwiki sitelinks and extracts, cached under `<wikidata_cache>/lang/fr`. |
| Metadata | `Language` (ISO 639-3, `fra`), full-text stemming, meta.json `wikipediaLang`. |
| Viewer UI | Buttons, panels, messages, Find chips, turn-by-turn, place sheet and the Find page, when the language has a table in `resources/viewer/i18n/` (German today; others show English). See [i18n.md](i18n.md). |

## What is not (yet)

- The viewer UI is translated only into German so far; any other language
  shows it in English ([i18n.md](i18n.md)). Build-side text is English in
  every build: the Kiwix search pages ("Directions to here", "View on
  map"), the article back bar and footer, the default ZIM title and
  description.
- Location labels beside search results (`l`, "Monte-Carlo") and an admin
  area's type ("city") are English.
- Administrative-area names come from the boundary's `name` / `name:en`
  tags, not `name:fr`.
- Right-to-left languages render their text correctly (MapLibre's RTL
  plugin) in a left-to-right layout.
- A `--search-cache` must have been extracted with `name:fr`: its
  `<cache>.schema` lists the languages (`STREETZIM_TILE_LANGUAGES` at
  extraction), and a build in a language the cache lacks stops with an
  explanation. `derive-region-search.py` and `build_search_cache.py` carry
  the marker to the caches they cut.
- An `--mbtiles` made without `name:fr` labels the map in the places' own
  names (tiles are not checked).
- `--wikidata-title-map` (English titles) is ignored in another language;
  a `--wiki-articles-source` ZIM must be a Wikipedia in the build's language.
- `nb` uses Norwegian Wikipedia (`no`); Chinese articles are fetched in
  Simplified script.
