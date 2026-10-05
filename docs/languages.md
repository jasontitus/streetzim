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

## What is not (yet)

- The viewer's own buttons, panels and messages are English.
- Location labels beside search results (`l`, "Monte-Carlo") and an admin
  area's type ("city") are English.
- Administrative-area names come from the boundary's `name` / `name:en`
  tags, not `name:fr`.
- Right-to-left languages render their text correctly (MapLibre's RTL
  plugin) in a left-to-right layout.
- A `--search-cache` or `--mbtiles` made without `name:fr` gives labels and
  results in the places' own names (the build warns about a search cache
  without the schema marker; tiles are not checked).
