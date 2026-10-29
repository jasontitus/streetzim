# International sign-reader photo collection

**100 real, labelled original photos across 15 countries; all priority-country numerical minima met.** Image bytes total **220,912,186 bytes** (about 221 MB). All nine manifest kinds are represented, including the eight requested sign kinds and negatives. This is a photo collection, not a scored benchmark: map-specific ground truth is still pending.

## Location and layout

The corpus is committed at `tests/fixtures/ocr-signs/`. These are actual files, not external downloads or symlinks. The fixture is excluded from the Docker build context and is not bundled into maps.

- `manifest.jsonl`: 100 accepted-photo records, relative image paths, SHA-256, licence and attribution, exact target-panel text, proposed search query, and map-region ID.
- `<country>/manifest.jsonl`: country-specific records. Original JPEGs and `.source.json` licence/provenance evidence sit beside it.
- [ATTRIBUTIONS.md](ATTRIBUTIONS.md): human-readable photo-by-photo credits. The photos retain their individual licences, not the repository's MIT software licence.

Discovery candidates, previews, rejected photos, raw scraped pages and machine-local collection scripts are not included.

## Coverage by country

Required minima apply to the priority countries. Negatives are excluded when checking positive-photo minima; GB and GE are additional coverage.

| Country | Required positives | Accepted originals | Positive photos | Negative photos |
|---|---:|---:|---:|---:|
| BH | 10 | 11 | 10 | 1 |
| CH | 5 | 5 | 5 | 0 |
| CN | 5 | 5 | 5 | 0 |
| EG | 5 | 5 | 5 | 0 |
| GB | — | 5 | 5 | 0 |
| GE | — | 3 | 1 | 2 |
| GR | 5 | 6 | 6 | 0 |
| IN | 5 | 5 | 5 | 0 |
| IR | 5 | 6 | 6 | 0 |
| JP | 10 | 15 | 15 | 0 |
| KR | 5 | 6 | 6 | 0 |
| RU | 5 | 5 | 5 | 0 |
| TH | 5 | 6 | 6 | 0 |
| TW | 10 | 12 | 12 | 0 |
| UA | 5 | 5 | 5 | 0 |

BH has ten positive photos plus one out-of-region destination candidate. GE has two Georgian-only unsupported-script negatives and one house-number plate. Unsupported-script negatives are not labelled “no text”: visible text exists, but the target script is unsupported by the stated recognizer list. The source photos can contain other, non-target signs.

## Coverage by sign kind

| Kind | Photos |
|---|---:|
| address plate | 1 |
| bilingual | 12 |
| building tag | 1 |
| business sign | 47 |
| hard | 1 |
| landmark | 16 |
| negative | 3 |
| street sign | 16 |
| transit | 3 |

The `building tag` example is a Taiwanese lane/alley plate, as allowed by the collection definition. The dedicated `hard` example is backlit wooden calligraphy. Additional hard cases retain their underlying kind and use `difficulty: hard`. The address-plate example labels a clearly delimited number **60**; its faded lower street sublabel is explicitly outside the partial transcription.

## Coverage by script and difficulty

Script counts overlap for multilingual photos. `Hani` includes Japanese kanji and Chinese; country/language labels distinguish them. `Zyyy` is ISO 15924 Common, used for the number-only plate.

| ISO 15924 script | Photos containing script |
|---|---:|
| Arab | 19 |
| Cyrl | 9 |
| Geor | 2 |
| Grek | 5 |
| Hang | 5 |
| Hani | 24 |
| Hira | 4 |
| Kana | 6 |
| Latn | 73 |
| Taml | 1 |
| Thai | 4 |
| Zyyy | 1 |

| Difficulty | Photos |
|---|---:|
| easy | 27 |
| hard | 32 |
| medium | 41 |

## Sources and original-file provenance

| Source | Accepted originals |
|---|---:|
| commons | 8 |
| flickr | 39 |
| geograph | 5 |
| kartaview | 15 |
| panoramax | 33 |

- **Flickr:** explicitly published `sizes.o` originals, not large thumbnails. Licence verified on each live source photo page; original URL, source metadata, author and source-requested credit retained.
- **Commons / archived Panoramio:** Commons supplies the photo-specific licence and author evidence; Internet Archive supplies captured upstream `/photos/original/` JPEGs. Decoded dimensions match Commons original metadata. Capture records and retrieval URLs are retained in sidecars. No Commons thumbnails substituted.
- **Panoramax:** source-published highest-resolution masters, not SD/thumb or `/derivates/` assets. `/images/`, permanent masters and data-role HD assets are used; HD capture dimensions are evidenced in metadata. These are provider privacy-anonymized public masters, not private raw uploads. Some JPEGs contain MPF/MPO metadata, which the validator recognizes as JPEG primary images.
- **KartaView:** direct `fileurlProc` public privacy-blurred storage masters, not thumbnail paths or resized CDN images. Decoded dimensions match API width/height. Each attribution contains the required **© Grab and KartaView Contributors**, plus photographer credit. Licence evidence: https://kartaview.org/terms, section 4.
- **Geograph:** exact source-page-linked signed `_original.jpg` assets, not `_1024x1024.jpg` derivatives. Photo-specific licence, photographer, signed asset URL and extracted image-tag evidence are retained in `.source.json` sidecars.

No local resizing, cropping, re-encoding, orientation change or EXIF removal was applied to accepted originals. Source-master dimensions range from 1,000 to 10,904 pixels on the long side. Provider privacy processing is disclosed per record. Contact sheets and temporary inspection crops are outside the accepted set.

## Licences and redistribution

| Licence | Photos |
|---|---:|
| CC BY 2.0 | 27 |
| CC BY 3.0 | 6 |
| CC BY-SA 2.0 | 15 |
| CC BY-SA 3.0 | 2 |
| CC BY-SA 4.0 | 48 |
| CC0 1.0 | 2 |

Keep the manifest, [ATTRIBUTIONS.md](ATTRIBUTIONS.md) and source sidecars with every copy. Each photo retains its own licence; the repository's MIT licence does not replace these image licences. Public photographer credits are retained as required. Accepted manifests do not contain source-account email addresses. Only the accepted CC BY, CC BY-SA and CC0 photos are included.

## Label scope and ground truth

One main sign or explicitly identified name panel is the target; other text is a distractor. `lines` preserves visible target text in reading order, with language and a Latin reading/translation for non-Latin lines. Panels with unreadable incidental text are explicitly scoped in `notes`; no missing characters or house numbers were invented.

`expect.query` and type are supplied for every photo. **All 100 map verifications are pending; zero `expect.record` values are claimed as map-verified.** One independently matched Thai business has target `near` coordinates, with its public directory evidence in notes. Other target coordinates and map membership remain empty/null rather than guessed. Camera GPS is never silently copied into target coordinates.

Location provenance uses the requested `exif`, `commons`, `mapillary`, `manual` values, or empty when unknown. `exif` means embedded GPS was checked against the recorded location; provider/API coordinates recorded outside EXIF use `manual` with their actual source described in notes. Original EXIF, including GPS when present, remains untouched.

Map IDs were checked against [cloud/region-places.tsv](../../../cloud/region-places.tsv). The smallest known appropriate region is used; an uncertain Taiwanese city remains on the country map instead of claiming Taipei. No viewer search/scoring harness has been run.

## Offline validation

From the repository root:

```sh
python tools/check_ocr_corpus.py
```

Requires Pillow, already in `requirements.txt`. The tool resolves the fixture relative to its own location, so it also works from another working directory. `--dataset DIR` checks a different copy. It never downloads files, changes labels or claims map-search success.

Checks: original-image decoding, dimensions and minimum resolution, SHA-256, unique IDs/images, image inventory, per-country/combined manifest agreement, source sidecars, licence allowlist, canonical metadata, non-Latin review readings, priority-country positive minima and all nine sign kinds. The original images were visually reviewed during transcription, with spot audits of restored and multilingual photos.

When adding a photo, keep the source-published original bytes and EXIF, add a `.source.json` evidence sidecar, update both manifests and its attribution entry, and refresh these coverage counts. Do not add thumbnails, discovery candidates or unverified map matches.

## Remaining quality and coverage gaps

- **Map ground truth/scoring:** still needs map lookups to fill `record`, target `near`, and `in_map`; historical businesses, chain branches and renamed streets need particular care. The Bahrain negative is geographically out of its selected region, but actual search absence has not been verified. No definitive “not on any map” result is claimed.
- **Address formats:** Bahrain building/road/block plates, Taiwanese building-number address plates, Japanese chome-ban-go plates and station-name examples, Korean road-name building-number plates, and Arabic-Indic/Persian-digit address examples remain sparse or absent. The number-60 plate has no readable street context; partial scope is disclosed.
- **Scripts/languages:** Tamil is represented, but Devanagari and Telugu originals are not yet present. Swiss French, Italian and Romansh are absent. Unsupported-script negatives currently cover Georgian only, not Hebrew/Armenian/Khmer/Lao/Burmese/Sinhala.
- **Distribution:** many photos are business signs or wide street-level views, and not every country covers every kind. Additional close, frontal address plates would improve balance. Lower-priority regions beyond GB/GE have not been systematically collected.

Only the 100 manifest records with actual accepted original files count in this collection.
