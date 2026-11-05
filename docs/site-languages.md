# The site in other languages

The site (https://streetzim.web.app) has a version in every language the
builder page (/build/) can make maps in. Each version has:

- a home page at `/<lang>/` (English: `/`) that lists every map in that
  language's words;
- a page per map at `/<lang>/maps/<id>/` (English: `/maps/<id>/`) with the
  map's downloads, what's inside, how to open it in Kiwix on each platform,
  and links to the same page in the other languages.

`web/generate.py` writes all of them at every site build, with `web/site.css`
(the home page's stylesheet, shared by the map pages) and `web/sitemap.xml`
(every page, each with its versions in the other languages). None of these
outputs is tracked in git except `web/de/index.html`. A language whose
translation files are removed loses its pages at the next build.

## Downloads in a language

On an English map page, the main download is the map's own file, as on the
home page, with a link to the builder page for other languages. On a page in
another language, a map's main download is the map in that language: its
labels, search and Wikipedia in that language, as one .zim file. The build worker serves it (the `webseed` of the region's
`web/build/recipes/<id>/region.json`, which needs its `<lang>.ws.json`
published). When the worker cannot serve it, the page's download script
falls back to the builder page, which builds the file in the browser
(`/build/?region=<id>&lang=<lang>&go=1`). The original file, with place names
in the local language and an English interface, sits beside it.

A map with no version in a language offers the original file, with a note
saying so. That covers a map not on the builder page, one over its size
limit, a light map, or one whose packs are not built yet.

## The text

`web/i18n/site.en.json` holds the page text, keyed. Its `_notes` tell
translators how to treat HTML, `{placeholders}` and names. `web/template.html`
uses those keys as `{{t:key}}` (HTML) and `{{a:key}}` (inside an attribute).
The map pages' text is built in `generate.py` from the same keys.

`web/i18n/regions.en.json` holds the maps' names and descriptions. It is
exported from `REGIONS` in `generate.py`; translations go in
`regions.<lang>.json`.

A language is on the site when both `site.<lang>.json` and
`regions.<lang>.json` exist.

### Changing or adding English text

1. Edit `site.en.json` and use the key in the template or the generator.
   `tests/test_site_languages.py` fails on a key nothing uses, and on a key
   used that does not exist.
2. Until a language has the new key, its pages show it in English. The build
   prints how many keys each language lacks.
3. Translate the key into every `site.<lang>.json`, then run
   `python3 tools/check_site_i18n.py`. It checks that each translation has the
   same keys, the same `{placeholders}` and the same markup as the English
   (every tag and attribute, as a browser parses them). `generate.py` runs
   the same check and stops on a translation whose markup differs: the text
   goes into the pages as HTML.

### A new map

A map added to `REGIONS` shows its English name and description on the other
languages' pages until `regions.<lang>.json` has it. Add it to
`regions.en.json`, then to each translation.

### A new language

Add `site.<lang>.json` and `regions.<lang>.json`. If the language is written
right to left, add it to `RTL_LANGS` in `generate.py`. Then add it to `LANGS`
in `tests/test_site_languages.py`.

## Search engines

Every page carries its canonical URL, `hreflang` links to all its language
versions plus `x-default` (English), and its title and description in its
language. The sitemap lists the same links. `robots.txt` names the sitemap;
submit it once in Google Search Console (the site is verified there through
`web/googlec2f20a1e7fb53c4a.html`).
