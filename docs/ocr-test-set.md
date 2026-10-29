# Sign-reader test set: what to collect

The map viewer's sign reader (docs/ocr.md) turns a photo of a sign into a
map search: a street-name sign, an address plate, a building number, a shop
front, a landmark plaque. Today it is tested on synthetic signs only
(tools/ocr_check.mjs draws them). This set is **real photos, labelled**, so
every viewer change can be scored per country, script and kind of sign: did
the right place come up first, or in the top three?

This file says what to collect and how to describe it; [Scoring](#scoring)
says how the set is run through the viewer of each photo's map, as a phone
does.

## What to collect

Real photos of public signage in the places our maps cover. Per country (or
region map) aim for **10-20 photos, at least 5**, mixing these kinds:

| kind | examples | what the reader must find |
|---|---|---|
| street sign | a street-name plate on a pole or wall | the street |
| address plate | a house or building number plate, often with the street (Bahrain: building over "road N"; Taiwan: district, street, number; Japan: chome-ban-go) | the street, ideally the building |
| building tag | a building or block number, a lane/alley plate | the street or block |
| business sign | a shop front, restaurant, hotel, pharmacy, bank branch | the business (a POI) |
| landmark | a museum, station, temple, school, park entrance name board | the place |
| transit | a bus stop, metro station, tram stop sign | the stop or station |
| bilingual | any of the above in two scripts (Arabic + Latin, Chinese + Latin, ...) | either name |
| hard | far away, at an angle, partly hidden, at night, several signs in one photo, handwritten | as above; tells us the limits |

Also a few **negatives**: signs in a script our maps cannot read yet
(Hebrew, Georgian, Armenian, Khmer, Lao, Burmese, Sinhala, ...) and signs of
places not on any map, to check the reader says "no text" or finds nothing
rather than something wrong.

### Countries and scripts, in priority order

1. **Bahrain** (Arabic + Latin; address plates: building number, road
   number, block).
2. **Taiwan / Taipei** (Traditional Chinese, vertical text, address plates:
   district, road, section, lane, alley, number).
3. **Japan** (Kanji, kana; chome-ban-go plates, station names).
4. **South Korea** (Hangul; road-name address plates with building number).
5. **Thailand** (Thai; soi and road signs, shop fronts).
6. **Greece** (Greek + Latin street signs).
7. **Switzerland** (German, French, Italian, Romansh).
8. **Iran** (Persian, Persian digits), **Egypt / North Africa** (Arabic,
   French, Arabic-Indic digits).
9. **Russia, Ukraine** (Cyrillic: ул., просп., д.).
10. **India / Himalayas** (Devanagari, Tamil, Telugu, Latin).
11. **China** (Simplified Chinese).
12. The rest of the maps: Western Europe, the Nordics, the Baltics, Turkey,
    Southeast Asia (Vietnamese diacritics), Mexico and Central America, the
    US (California, New York, Texas, ...).

Which maps carry the reader, and with which recognizers: a map's
`ocr/manifest.json` (`rec[].name`: latin-cjk, arabic, korean, thai,
cyrillic, greek, devanagari, tamil, telugu). For example:

    python3 -c "import json,sys; from libzim.reader import Archive; \
      print([r['name'] for r in json.loads(bytes(Archive(sys.argv[1]).get_entry_by_path('ocr/manifest.json').get_item().content))['rec']])" osm-bahrain-2026-10-28.zim

A map without the file has no camera button.

## Where photos may come from

Only photos we may republish with attribution:

- **Wikimedia Commons** (first choice): CC0, public domain, CC BY, CC BY-SA.
  Useful categories: "Street name signs in <country>", "House numbering in
  <country>", "Address plates in <country>", "Shop signs in <country>",
  "Bus stop signs in <country>". The API gives the licence, author and often
  the coordinates.
- **Mapillary** (CC BY-SA 4.0, street-level imagery with exact coordinates;
  crop the sign). Needs a free API token.
- **Flickr** photos under CC BY / CC BY-SA / CC0 only.
- Photos you or volunteers take yourselves (say so in the licence field).

Not allowed: Google Street View or Maps imagery, stock photo sites (the
watermark, "alamy" and the like, is read as text and skews the test),
anything "all rights reserved", screenshots of other apps.

Privacy: no photo where a person's face, a car's number plate or a home's
occupants are the subject. A house number plate on a public facade is
fine; crop out people.

## Each photo

- The **original file** at its full resolution (at least 1000 px on the long
  side; phone photos are typical), JPEG or PNG, not re-compressed, not
  cropped unless the crop removes people or watermarks (then say so).
- Keep the EXIF orientation. Keep GPS coordinates if present (they help the
  ground truth); they go in the metadata too.
- One sign the photo is "about" (others may be in the frame).

## Metadata: one record per photo

A JSON Lines file, `manifest.jsonl`, one object per photo:

```json
{
  "id": "bh-0007",
  "file": "bh/bh-0007.jpg",
  "sha256": "...",
  "source": "commons",
  "source_url": "https://commons.wikimedia.org/wiki/File:...",
  "author": "Name as the source credits it",
  "license": "CC BY-SA 4.0",
  "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
  "attribution": "Photo: Name, CC BY-SA 4.0, via Wikimedia Commons",
  "country": "BH",
  "region": "bahrain",
  "kind": "address plate",
  "scripts": ["Arab", "Latn"],
  "lines": [
    {"text": "460", "lang": "und"},
    {"text": "طريق 2912", "lang": "ar", "latin": "Road 2912"}
  ],
  "expect": {
    "query": "Road 2912",
    "record": "Road 2912",
    "type": "street",
    "near": [26.169, 50.556],
    "in_map": true
  },
  "location": [26.1693, 50.5561],
  "location_source": "exif",
  "difficulty": "easy",
  "notes": "Bahraini plate: building 460 on road 2912; road numbers repeat across blocks"
}
```

Fields:

- `id`: country code + number; `file`: path under the set's folder;
  `sha256` of the file.
- `source`, `source_url`, `author`, `license`, `license_url`,
  `attribution`: enough to credit the photo as its licence asks.
- `country` (ISO 3166-1 alpha-2) and `region`: our map's id (cloud/
  region-places.tsv; the smallest map that covers the place).
- `kind`: one of the kinds in the table above (or "negative").
- `scripts`: ISO 15924 codes of the text on the sign.
- `lines`: the sign's text, line by line in reading order, exactly as
  written (keep Arabic-Indic digits as they are), with a language code and,
  for non-Latin text, a romanisation or translation in `latin` (helps the
  person checking the labels).
- `expect`: what the reader should find.
  - `query`: what a person would type to find it.
  - `record`: the name the map's search shows for it, when it is on the map.
  - `type`: street, poi, place, addr, or stop.
  - `near`: its coordinates, to tell apart places that share a name (road
    numbers repeat across Bahrain's blocks).
  - `in_map`: false when the map does not have it, for example the Tainan
    plate on the Taipei map.
- `location`: where the photo was taken, if known, and `location_source`
  (exif, commons, mapillary, manual).
- `difficulty`: easy, medium or hard. `notes`: anything else, such as the
  address format or a watermark that was cropped.

To find `record` and `near`, search the map in the viewer
(https://streetzim.web.app/drive/ opens any map from archive.org) or ask the
main session for a lookup script. Leave them empty when unsure: the person
checking the labels fills them in.

## Folder and size

    tests/fixtures/ocr-signs/
      manifest.jsonl     every photo's record
      README.md          counts per country, kind and script; the gaps
      ATTRIBUTIONS.md    each photo's credit, as its licence asks
      bh/ tw/ jp/ ...    one folder per country code: the photos, each with
                         its .source.json (licence, provenance), and the
                         country's own manifest.jsonl

The photos keep their own licences (ATTRIBUTIONS.md), not the repository's.
They are a few hundred MB: .dockerignore keeps them out of the build
context, and no map carries them. Keep personal data out of the manifest.

## Done means

- At least 5 photos for each of the first 11 priority countries, 10 or more
  for Bahrain, Taiwan and Japan, and every kind of sign in the table
  represented somewhere.
- Every record has a licence that allows republishing, an attribution,
  `lines` and `kind`; `expect` filled where the answer is known.
- README.md lists the counts and what is missing.

## Scoring

`tools/ocr_photos.py` runs every photo through the sign reader of its
region's map: the newest local `osm-<region>-<date>.zim`, served with
today's viewer and the reader's models on top (`cloud/serve_zim_entries.py
--overlay`), so a map built before the reader is scored as a map built now
would be. A map's own recognizers are used when it has the reader; otherwise
the Latin/CJK one and those of its photos' scripts.

    python3 tools/ocr_photos.py --set tests/fixtures/ocr-signs --maps <maps folder> --out <out>
    python3 tools/ocr_photos.py ... --regions bahrain,taipei      # some maps
    python3 tools/ocr_photos.py --score <out>                     # score again

Per photo: `read` (the share of the labelled `lines` read, each 80% of its
letters in order), `query` (the line searched for is the labelled one),
`found@1` and `found@3` (the labelled place among the first results, by
`expect.record` or `expect.query`). `<out>/report.md` has the table per map,
per kind and per photo; `<out>/raw.jsonl` what the reader read and found.
Compare two viewers by running both and reading the reports side by side.
