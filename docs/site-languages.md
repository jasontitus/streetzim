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

## Languages of the maps alone

A language of a region's maps that has no full site (Tamil, Telugu, Kannada,
Malayalam, Bengali, Assamese, Odia, Nepali, Marathi, Gujarati, Punjabi and
Urdu, so far) gets a site of its own maps: its `site.<lang>.json` says
`"_maps_only": true`, and its `regions.<lang>.json` names only the maps of
its region (`tools/check_site_i18n.py` asks no other map of it). Its home
page lists only the maps that have a version in it (the map in that
language, with its size: the build worker's file, or the builder page when
the worker cannot serve it, as on any other page) and the maps made in it; it has pages for those maps
alone. Nothing links a page it does not have: the language picker, the
`hreflang` links and the sitemap leave it out of every other map's page.
While no map has a version in it, it is in no picker, `hreflang` link or
sitemap, and a language that never had one has no pages; one whose maps are
out a while (archive.org down) keeps the pages it had, unlinked, rather than
losing them. A map whose version is gone loses its page at the next build.
A listed map with no name in `regions.<lang>.json` is listed in English, with
a warning in the build's output
(`web/generate.py` `MAPS_ONLY`, `has_page`; `tests/test_site_maps_only.py`).
People who read those languages search in them, where little else offers an
offline map: the page's title and description are in the language, with the
maps' names in its script.

## Clips and screenshots

The home page shows clips and screenshots of real maps: the clip and the
phone at the top, the clips under "See it in action", the gallery, and the
phone screens of the language versions. The files are in `web/media/`, each with its content hash
in its name, because the site serves `/media/` as never changing
(`firebase.json`). `web/media/media.json` lists them under the names the
pages use, with each picture's size, caption and credits; the host keeps
that file to itself.

`tools/site_media.py` makes the folder and the manifest from capture output
(its docstring has the arguments), and with `--readme` the README's pictures
in `docs/readme/`. Run it again after new captures and commit what it
changed; a file no page shows any more is removed.

`web/template.html` names a file as `{{m:name}}` (its URL) and a clip's
credit as `{{mc:name}}`; the generator writes the gallery, the sign clips
and the language screens from the manifest. A name the manifest lacks stops
the build.

A caption is a key of `site.en.json` (`gal_*`), so every language shows it in
its own words, followed by the map's name in that language.
`GALLERY_CAPTIONS` in `generate.py` lists the keys; a caption that is not
among them stops the build.

Whoever's photo or imagery a picture shows is named with it: under a
gallery picture and under the clip that plays in the page, in the viewer for
a clip or picture that opens there, and in the list at the foot of the page
(`credits_*`, `credit_*`). The satellite imagery's credit is the same in every language
(`VERBATIM` in `tools/check_site_i18n.py`). `docs/media-credits.md` is the
same list for the repository. `tests/test_site_media.py` keeps the folder,
the manifest, the pages and that list in step.

## Search engines

Every page carries its canonical URL, `hreflang` links to all its language
versions plus `x-default` (English), and its title and description in its
language. The sitemap lists the same links. `robots.txt` names the sitemap;
submit it once in Google Search Console (the site is verified there through
`web/googlec2f20a1e7fb53c4a.html`).
