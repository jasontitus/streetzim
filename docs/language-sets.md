# StreetZim language sets

Every map is published as one **base** ZIM (map tiles, search, English Wikipedia) plus **packs**: one Wikipedia pack per language and a search pack with place names in other languages and scripts. A **recipe** per language lists which byte ranges of the base and packs make up that language's ZIM, plus a few small inline pieces (its own header, directory and checksum). The site's builder page, a single-file torrent, or a webseed Worker that splices the same ranges assembles it into an ordinary, valid ZIM with its own MD5. Readers need no changes.

Measured from the recipes deployed on 2026-11-10 (74 maps, 1,735 language versions). The 16 maps not yet split into language versions are estimated below.

## Languages

### Global set: 22 languages

Every map is offered in these (`CORE_LANGS` in `tools/langpack/region_meta.py`): English plus the languages the map tiles carry labels in. Maps published before the set grew to 22 may still lack some of them until they are rebuilt.

| Code | Name | Live maps offering it |
|---|---|---:|
| ar | العربية | 74 of 74 |
| de | Deutsch | 74 of 74 |
| el | Ελληνικά | 74 of 74 |
| en | English | 74 of 74 |
| es | Español | 74 of 74 |
| fa | فارسی | 74 of 74 |
| fr | Français | 74 of 74 |
| he | עברית | 74 of 74 |
| hi | हिन्दी | 73 of 74 |
| id | Bahasa Indonesia | 73 of 74 |
| it | Italiano | 74 of 74 |
| ja | 日本語 | 74 of 74 |
| ko | 한국어 | 73 of 74 |
| nl | Nederlands | 74 of 74 |
| pl | Polski | 73 of 74 |
| pt | Português | 73 of 74 |
| ru | Русский | 74 of 74 |
| th | ไทย | 74 of 74 |
| tr | Türkçe | 70 of 74 |
| uk | Українська | 74 of 74 |
| vi | Tiếng Việt | 73 of 74 |
| zh | 中文 | 73 of 74 |

### Regional languages: 76 more

A map also gets the languages spoken in its area. A country map keeps all of them; a continent or multi-country map carries Wikipedia packs for its few most spoken ones in every version, and gives the others a version of their own when the viewer can show its interface in that language.

| Code | Name | Maps |
|---|---|---|
| af | Afrikaans | Southern Africa |
| am | አማርኛ | East Africa, Horn of Africa |
| as | অসমীয়া | Northeast India, Northeast India (Light) |
| av | Авар | Caucasus |
| az | Azərbaycanca | Caucasus, Central Asia, Iran |
| bg | Български | Romania, Hungary & Bulgaria |
| bi | Bislama | Pacific Islands |
| bn | বাংলা | East India, East India (Light), Northeast India, Northeast India (Light) |
| bs | Bosanski | Balkans |
| ca | Català | Iberia |
| ce | Нохчийн | Caucasus |
| cs | Čeština | Austria, Czechia & Slovakia |
| cy | Cymraeg | Britain & Ireland |
| da | Dansk | Nordics |
| din | Thuɔŋjäŋ | East Africa, Horn of Africa |
| et | Eesti | Baltics |
| eu | Euskara | Iberia |
| ff | Fulfulde | Central Africa, West Africa |
| fi | Suomi | Nordics |
| fj | Na Vosa Vakaviti | Pacific Islands |
| fy | Frysk | Benelux |
| ga | Gaeilge | Britain & Ireland |
| gl | Galego | Iberia |
| glk | گیلکی | Iran |
| gu | ગુજરાતી | West India, West India (Light) |
| ha | Hausa | West Africa |
| haw | ʻŌlelo Hawaiʻi | Hawaii |
| hif | Fiji Baat | Pacific Islands |
| hr | Hrvatski | Balkans |
| ht | Kreyòl ayisyen | Central America & Caribbean, Hispaniola |
| hu | Magyar | Austria, Czechia & Slovakia, Romania, Hungary & Bulgaria |
| hy | Հայերեն | Caucasus, Central Asia |
| ig | Igbo | West Africa |
| is | Íslenska | Iceland |
| jam | Patwa | Central America & Caribbean |
| ka | ქართული | Caucasus, Central Asia |
| kg | Kikongo | Central Africa |
| kn | ಕನ್ನಡ | South India, South India (Light) |
| ku | Kurdî | Caucasus, Iran, Turkey |
| lb | Lëtzebuergesch | Benelux |
| ln | Lingála | Central Africa |
| lt | Lietuvių | Baltics |
| lv | Latviešu | Baltics |
| mk | Македонски | Balkans |
| ml | മലയാളം | South India, South India (Light) |
| mr | मराठी | West India, West India (Light) |
| mzn | مازِرونی | Iran |
| ne | नेपाली | Northeast India, Northeast India (Light) |
| no | Norsk | Nordics |
| ny | Chichewa | Central Africa, Southern Africa |
| om | Afaan Oromoo | East Africa, Horn of Africa |
| or | ଓଡ଼ିଆ | East India, East India (Light) |
| pa | ਪੰਜਾਬੀ | Northwest India, Northwest India (Light) |
| pap | Papiamentu | Central America & Caribbean |
| pcm | Naijá | West Africa |
| rm | Rumantsch | Switzerland & the Alps (Light), Switzerland and the Alps |
| ro | Română | Romania, Hungary & Bulgaria, Ukraine, Moldova & Western Russia |
| sc | Sardu | Italy |
| sk | Slovenčina | Austria, Czechia & Slovakia |
| sl | Slovenščina | Balkans |
| sn | chiShona | Southern Africa |
| so | Soomaali | East Africa, Horn of Africa |
| sq | Shqip | Balkans |
| sr | Српски | Balkans |
| sv | Svenska | Nordics |
| sw | Kiswahili | Central Africa, East Africa, Horn of Africa |
| ta | தமிழ் | South India, South India (Light) |
| te | తెలుగు | South India, South India (Light) |
| ti | ትግርኛ | East Africa, Horn of Africa |
| tpi | Tok Pisin | Pacific Islands |
| ty | Reo Tahiti | East Polynesia |
| ur | اردو | Bahrain, East India, East India (Light), Northwest India, Northwest India (Light), South India, South India (Light), Uttar Pradesh & Central India, Uttar Pradesh & Central India (Light), West India, West India (Light) |
| xh | isiXhosa | Southern Africa |
| yo | Yorùbá | West Africa |
| zgh | ⵜⴰⵎⴰⵣⵉⵖⵜ | North Africa (Maghreb) |
| zu | isiZulu | Southern Africa |

## Storage totals

| | Maps | Language versions | Composable (bases + packs + recipes) | Every version as its own file | Ratio |
|---|---:|---:|---:|---:|---:|
| Measured (live now) | 74 | 1,735 | 399.9 GB | 3,911.5 GB | 9.8× |
| Estimated (not yet split) | 16 | ~364 | ~690.9 GB | ~6,764.0 GB | 9.8× |
| **Total when rebuilt** | **90** | **~2,099** | **~1.1 TB** | **~10.7 TB** | **9.8×** |

Composable breakdown (measured): bases 148.3 GB, packs 235.2 GB, recipes and webseed splice pieces 16.4 GB.

The estimate uses ratios measured on live maps of the same kind: US regional maps (packs 0.88 × base, one version 1.04 × base, 22 versions) for the United States, East Coast US, Canada and Australia & NZ; other maps over 4 GB (packs 1.89 × base, one version 1.16 × base, 23 versions) for the rest. Individual maps vary widely (packs run from about 0.7 × base on US maps to 3 × on some European ones), so per-map figures are rough; Europe will probably have more versions than 23, so its figure is if anything low. Some of these maps overlap others (Africa and Africa light, South America with Brazil and Argentina, the United States with the East Coast and the US regional maps, Europe with its country maps); they are counted because each is published on its own.

## Block-level deduplication (ZFS / btrfs)

Every version stored as its own file, deduplicated in fixed-size blocks, for the 74 live maps. Modelled from the recipes: a block is shared only when it is wholly copied from the same offset of the same base or pack as a block already counted; everything else, including blocks that straddle two pieces, counts as unique.

| Block size | All versions | After dedup | Composable |
|---:|---:|---:|---:|
| 16 KiB | 3,911.5 GB | 439.9 GB | 399.9 GB |
| 128 KiB (ZFS default recordsize) | 3,911.5 GB | 440.8 GB | 399.9 GB |
| 1,024 KiB | 3,911.5 GB | 446.6 GB | 399.9 GB |

Checked against real bytes: all 22 Kyiv versions assembled and hashed in 128 KiB blocks give 2,401.7 MB → 601.4 MB (4.0×), against 3.8× from the model, so the model slightly understates the saving; 4 KiB blocks give 4.02×.

It works because the merger keeps the base's bytes at their original offsets in every version, so most of each file lines up block for block with the base. Each version's packs and its own directory sit at shifting offsets and are stored again. Extrapolated to all 90 maps (×1.10 of composable): ~10.7 TB of separate files comes to ~1.2 TB deduplicated, against ~1.1 TB composable.

Limits: this saves disk only on a host that runs dedup. ZFS dedup keeps a table in RAM, about 320 bytes per unique block: ~3 GB at 128 KiB for the full set. btrfs has no inline dedup (duperemove or bees run after the write), so a btrfs host needs full-size room while syncing. ZFS compression gains almost nothing, as ZIM clusters are already zstd-compressed. Hosts without dedup, downloads, mirror syncs (rsync sends a new file name whole by default) and torrent seeding still carry the full ~10.7 TB.

## Maps not yet split into language versions

| Map | Base | Est. composable | Est. all versions |
|---|---:|---:|---:|
| Europe | 60.7 GB | ~175.4 GB | ~1,616.1 GB |
| United States | 34.9 GB | ~65.5 GB | ~799.2 GB |
| Russia | 22.0 GB | ~63.7 GB | ~586.9 GB |
| South America | 20.8 GB | ~60.2 GB | ~555.0 GB |
| Canada | 18.2 GB | ~34.2 GB | ~417.5 GB |
| Africa | 17.5 GB | ~50.5 GB | ~465.0 GB |
| Brazil | 12.6 GB | ~36.6 GB | ~336.9 GB |
| East Coast US | 11.1 GB | ~20.9 GB | ~255.0 GB |
| Africa (light) | 10.6 GB | ~30.6 GB | ~282.2 GB |
| Indian Subcontinent | 10.0 GB | ~28.8 GB | ~265.2 GB |
| China | 9.8 GB | ~28.5 GB | ~262.3 GB |
| West Asia | 9.3 GB | ~26.9 GB | ~247.8 GB |
| France | 8.4 GB | ~24.3 GB | ~223.7 GB |
| Southeast Asia | 7.9 GB | ~22.7 GB | ~209.3 GB |
| Australia & NZ | 6.7 GB | ~12.7 GB | ~154.6 GB |
| Argentina | 3.3 GB | ~9.5 GB | ~87.4 GB |

## Language versions per map (live)

Wikipedia: the languages whose Wikipedia articles that version carries.

### Athens (`athens`)

Base `osm-athens-2026-11-09.zim` 0.1 GB · packs 0.1 GB · 22 versions, 2.1 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 96 MB | 10 MB | ar, el, en |
| de | Deutsch | 97 MB | 10 MB | de, el, en |
| el | Ελληνικά | 95 MB | 8 MB | el, en |
| en | English | 94 MB | 8 MB | el, en |
| es | Español | 97 MB | 10 MB | es, el, en |
| fa | فارسی | 95 MB | 9 MB | fa, el, en |
| fr | Français | 99 MB | 13 MB | fr, el, en |
| he | עברית | 95 MB | 9 MB | he, el, en |
| hi | हिन्दी | 95 MB | 8 MB | hi, el, en |
| id | Bahasa Indonesia | 95 MB | 9 MB | id, el, en |
| it | Italiano | 98 MB | 11 MB | it, el, en |
| ja | 日本語 | 96 MB | 9 MB | ja, el, en |
| ko | 한국어 | 95 MB | 9 MB | ko, el, en |
| nl | Nederlands | 96 MB | 9 MB | nl, el, en |
| pl | Polski | 96 MB | 10 MB | pl, el, en |
| pt | Português | 95 MB | 9 MB | pt, el, en |
| ru | Русский | 98 MB | 11 MB | ru, el, en |
| th | ไทย | 95 MB | 8 MB | th, el, en |
| tr | Türkçe | 96 MB | 9 MB | tr, el, en |
| uk | Українська | 97 MB | 11 MB | uk, el, en |
| vi | Tiếng Việt | 95 MB | 9 MB | vi, el, en |
| zh | 中文 | 96 MB | 10 MB | zh, el, en |

### Austria, Czechia & Slovakia (`austria-czech`)

Base `osm-austria-czech-2026-10-26b.zim` 3.4 GB · packs 10.8 GB · 25 versions, 112.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 4,459 MB | 1,066 MB | ar, de, cs, sk, en |
| de | Deutsch | 4,440 MB | 1,055 MB | de, cs, sk, en |
| el | Ελληνικά | 4,448 MB | 1,060 MB | el, de, cs, sk, en |
| en | English | 4,443 MB | 1,055 MB | de, cs, sk, en |
| es | Español | 4,555 MB | 1,162 MB | es, de, cs, sk, en |
| fa | فارسی | 4,495 MB | 1,102 MB | fa, de, cs, sk, en |
| fr | Français | 4,611 MB | 1,216 MB | fr, de, cs, sk, en |
| he | עברית | 4,449 MB | 1,061 MB | he, de, cs, sk, en |
| hi | हिन्दी | 4,441 MB | 1,055 MB | hi, de, cs, sk, en |
| id | Bahasa Indonesia | 4,466 MB | 1,073 MB | id, de, cs, sk, en |
| it | Italiano | 4,534 MB | 1,155 MB | it, de, cs, sk, en |
| ja | 日本語 | 4,452 MB | 1,063 MB | ja, de, cs, sk, en |
| ko | 한국어 | 4,452 MB | 1,064 MB | ko, de, cs, sk, en |
| nl | Nederlands | 4,522 MB | 1,138 MB | nl, de, cs, sk, en |
| pl | Polski | 4,637 MB | 1,251 MB | pl, de, cs, sk, en |
| pt | Português | 4,556 MB | 1,166 MB | pt, de, cs, sk, en |
| ru | Русский | 4,519 MB | 1,136 MB | ru, de, cs, sk, en |
| th | ไทย | 4,443 MB | 1,057 MB | th, de, cs, sk, en |
| tr | Türkçe | 4,454 MB | 1,065 MB | tr, de, cs, sk, en |
| uk | Українська | 4,524 MB | 1,129 MB | uk, de, cs, sk, en |
| vi | Tiếng Việt | 4,518 MB | 1,128 MB | vi, de, cs, sk, en |
| zh | 中文 | 4,492 MB | 1,097 MB | zh, de, cs, sk, en |
| cs | Čeština | 4,441 MB | 1,055 MB | de, cs, sk, en |
| sk | Slovenčina | 4,441 MB | 1,055 MB | de, cs, sk, en |
| hu | Magyar | 4,575 MB | 1,195 MB | hu, de, cs, sk, en |

### Bahrain (`bahrain`)

Base `osm-bahrain-2026-11-09.zim` 0.0 GB · packs 0.0 GB · 23 versions, 1.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 51 MB | 2 MB | ar, fa, ur, en |
| de | Deutsch | 51 MB | 3 MB | de, ar, fa, ur, en |
| el | Ελληνικά | 51 MB | 3 MB | el, ar, fa, ur, en |
| en | English | 51 MB | 2 MB | ar, fa, ur, en |
| es | Español | 51 MB | 3 MB | es, ar, fa, ur, en |
| fa | فارسی | 51 MB | 2 MB | ar, fa, ur, en |
| fr | Français | 51 MB | 3 MB | fr, ar, fa, ur, en |
| he | עברית | 51 MB | 3 MB | he, ar, fa, ur, en |
| hi | हिन्दी | 51 MB | 2 MB | hi, ar, fa, ur, en |
| id | Bahasa Indonesia | 51 MB | 3 MB | id, ar, fa, ur, en |
| it | Italiano | 51 MB | 3 MB | it, ar, fa, ur, en |
| ja | 日本語 | 51 MB | 3 MB | ja, ar, fa, ur, en |
| ko | 한국어 | 51 MB | 2 MB | ko, ar, fa, ur, en |
| nl | Nederlands | 51 MB | 3 MB | nl, ar, fa, ur, en |
| pl | Polski | 51 MB | 3 MB | pl, ar, fa, ur, en |
| pt | Português | 51 MB | 2 MB | pt, ar, fa, ur, en |
| ru | Русский | 51 MB | 3 MB | ru, ar, fa, ur, en |
| th | ไทย | 51 MB | 2 MB | th, ar, fa, ur, en |
| tr | Türkçe | 51 MB | 2 MB | tr, ar, fa, ur, en |
| uk | Українська | 51 MB | 3 MB | uk, ar, fa, ur, en |
| vi | Tiếng Việt | 51 MB | 2 MB | vi, ar, fa, ur, en |
| zh | 中文 | 51 MB | 3 MB | zh, ar, fa, ur, en |
| ur | اردو | 51 MB | 2 MB | ar, fa, ur, en |

### Balkans (`balkans`)

Base `osm-balkans-2026-10-26b.zim` 2.4 GB · packs 5.3 GB · 28 versions, 76.8 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,729 MB | 377 MB | ar, sr, hr, sq, en |
| de | Deutsch | 2,759 MB | 419 MB | de, sr, hr, sq, en |
| el | Ελληνικά | 2,728 MB | 378 MB | el, sr, hr, sq, en |
| en | English | 2,716 MB | 366 MB | sr, hr, sq, en |
| es | Español | 2,754 MB | 401 MB | es, sr, hr, sq, en |
| fa | فارسی | 2,739 MB | 387 MB | fa, sr, hr, sq, en |
| fr | Français | 2,809 MB | 469 MB | fr, sr, hr, sq, en |
| he | עברית | 2,719 MB | 370 MB | he, sr, hr, sq, en |
| hi | हिन्दी | 2,715 MB | 367 MB | hi, sr, hr, sq, en |
| id | Bahasa Indonesia | 2,728 MB | 377 MB | id, sr, hr, sq, en |
| it | Italiano | 2,762 MB | 410 MB | it, sr, hr, sq, en |
| ja | 日本語 | 2,724 MB | 374 MB | ja, sr, hr, sq, en |
| ko | 한국어 | 2,723 MB | 374 MB | ko, sr, hr, sq, en |
| nl | Nederlands | 2,736 MB | 397 MB | nl, sr, hr, sq, en |
| pl | Polski | 2,808 MB | 467 MB | pl, sr, hr, sq, en |
| pt | Português | 2,750 MB | 399 MB | pt, sr, hr, sq, en |
| ru | Русский | 2,749 MB | 398 MB | ru, sr, hr, sq, en |
| th | ไทย | 2,716 MB | 368 MB | th, sr, hr, sq, en |
| tr | Türkçe | 2,730 MB | 380 MB | tr, sr, hr, sq, en |
| uk | Українська | 2,795 MB | 456 MB | uk, sr, hr, sq, en |
| vi | Tiếng Việt | 2,722 MB | 374 MB | vi, sr, hr, sq, en |
| zh | 中文 | 2,733 MB | 382 MB | zh, sr, hr, sq, en |
| sr | Српски | 2,714 MB | 366 MB | sr, hr, sq, en |
| hr | Hrvatski | 2,714 MB | 366 MB | sr, hr, sq, en |
| sq | Shqip | 2,714 MB | 366 MB | sr, hr, sq, en |
| bs | Bosanski | 2,764 MB | 425 MB | bs, sr, hr, sq, en |
| sl | Slovenščina | 2,811 MB | 467 MB | sl, sr, hr, sq, en |
| mk | Македонски | 2,758 MB | 404 MB | mk, sr, hr, sq, en |

### Baltics (`baltics`)

Base `osm-baltics-2026-10-26b.zim` 1.0 GB · packs 3.6 GB · 25 versions, 32.3 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,287 MB | 300 MB | ar, lt, lv, et, en |
| de | Deutsch | 1,304 MB | 315 MB | de, lt, lv, et, en |
| el | Ελληνικά | 1,283 MB | 299 MB | el, lt, lv, et, en |
| en | English | 1,281 MB | 297 MB | lt, lv, et, en |
| es | Español | 1,299 MB | 313 MB | es, lt, lv, et, en |
| fa | فارسی | 1,288 MB | 304 MB | fa, lt, lv, et, en |
| fr | Français | 1,307 MB | 322 MB | fr, lt, lv, et, en |
| he | עברית | 1,284 MB | 299 MB | he, lt, lv, et, en |
| hi | हिन्दी | 1,281 MB | 297 MB | hi, lt, lv, et, en |
| id | Bahasa Indonesia | 1,285 MB | 299 MB | id, lt, lv, et, en |
| it | Italiano | 1,288 MB | 303 MB | it, lt, lv, et, en |
| ja | 日本語 | 1,284 MB | 299 MB | ja, lt, lv, et, en |
| ko | 한국어 | 1,284 MB | 299 MB | ko, lt, lv, et, en |
| nl | Nederlands | 1,295 MB | 307 MB | nl, lt, lv, et, en |
| pl | Polski | 1,324 MB | 334 MB | pl, lt, lv, et, en |
| pt | Português | 1,282 MB | 299 MB | pt, lt, lv, et, en |
| ru | Русский | 1,329 MB | 336 MB | ru, lt, lv, et, en |
| th | ไทย | 1,281 MB | 297 MB | th, lt, lv, et, en |
| tr | Türkçe | 1,285 MB | 300 MB | tr, lt, lv, et, en |
| uk | Українська | 1,306 MB | 318 MB | uk, lt, lv, et, en |
| vi | Tiếng Việt | 1,282 MB | 298 MB | vi, lt, lv, et, en |
| zh | 中文 | 1,290 MB | 304 MB | zh, lt, lv, et, en |
| lt | Lietuvių | 1,277 MB | 297 MB | lt, lv, et, en |
| lv | Latviešu | 1,280 MB | 297 MB | lt, lv, et, en |
| et | Eesti | 1,280 MB | 297 MB | lt, lv, et, en |

### Benelux (`benelux`)

Base `osm-benelux-2026-10-26b.zim` 2.7 GB · packs 4.4 GB · 24 versions, 81.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 3,387 MB | 648 MB | ar, nl, fr, de, en |
| de | Deutsch | 3,371 MB | 637 MB | nl, fr, de, en |
| el | Ελληνικά | 3,383 MB | 645 MB | el, nl, fr, de, en |
| en | English | 3,372 MB | 637 MB | nl, fr, de, en |
| es | Español | 3,404 MB | 666 MB | es, nl, fr, de, en |
| fa | فارسی | 3,400 MB | 661 MB | fa, nl, fr, de, en |
| fr | Français | 3,372 MB | 637 MB | nl, fr, de, en |
| he | עברית | 3,378 MB | 640 MB | he, nl, fr, de, en |
| hi | हिन्दी | 3,374 MB | 637 MB | hi, nl, fr, de, en |
| id | Bahasa Indonesia | 3,385 MB | 646 MB | id, nl, fr, de, en |
| it | Italiano | 3,405 MB | 665 MB | it, nl, fr, de, en |
| ja | 日本語 | 3,383 MB | 644 MB | ja, nl, fr, de, en |
| ko | 한국어 | 3,379 MB | 641 MB | ko, nl, fr, de, en |
| nl | Nederlands | 3,372 MB | 637 MB | nl, fr, de, en |
| pl | Polski | 3,419 MB | 679 MB | pl, nl, fr, de, en |
| pt | Português | 3,400 MB | 662 MB | pt, nl, fr, de, en |
| ru | Русский | 3,402 MB | 660 MB | ru, nl, fr, de, en |
| th | ไทย | 3,376 MB | 639 MB | th, nl, fr, de, en |
| tr | Türkçe | 3,391 MB | 652 MB | tr, nl, fr, de, en |
| uk | Українська | 3,430 MB | 689 MB | uk, nl, fr, de, en |
| vi | Tiếng Việt | 3,389 MB | 651 MB | vi, nl, fr, de, en |
| zh | 中文 | 3,407 MB | 664 MB | zh, nl, fr, de, en |
| lb | Lëtzebuergesch | 3,394 MB | 655 MB | lb, nl, fr, de, en |
| fy | Frysk | 3,403 MB | 663 MB | fy, nl, fr, de, en |

### Britain & Ireland (`britain-ireland`)

Base `osm-britain-ireland-2026-10-26b.zim` 5.4 GB · packs 8.6 GB · 24 versions, 143.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 5,952 MB | 531 MB | ar, ga, cy, en |
| de | Deutsch | 6,040 MB | 628 MB | de, ga, cy, en |
| el | Ελληνικά | 5,914 MB | 500 MB | el, ga, cy, en |
| en | English | 5,906 MB | 494 MB | ga, cy, en |
| es | Español | 6,011 MB | 605 MB | es, ga, cy, en |
| fa | فارسی | 5,974 MB | 556 MB | fa, ga, cy, en |
| fr | Français | 6,184 MB | 774 MB | fr, ga, cy, en |
| he | עברית | 5,923 MB | 507 MB | he, ga, cy, en |
| hi | हिन्दी | 5,910 MB | 497 MB | hi, ga, cy, en |
| id | Bahasa Indonesia | 5,928 MB | 508 MB | id, ga, cy, en |
| it | Italiano | 6,062 MB | 654 MB | it, ga, cy, en |
| ja | 日本語 | 5,940 MB | 519 MB | ja, ga, cy, en |
| ko | 한국어 | 5,923 MB | 508 MB | ko, ga, cy, en |
| nl | Nederlands | 5,996 MB | 586 MB | nl, ga, cy, en |
| pl | Polski | 6,082 MB | 676 MB | pl, ga, cy, en |
| pt | Português | 5,950 MB | 531 MB | pt, ga, cy, en |
| ru | Русский | 5,969 MB | 561 MB | ru, ga, cy, en |
| th | ไทย | 5,913 MB | 499 MB | th, ga, cy, en |
| tr | Türkçe | 5,986 MB | 580 MB | tr, ga, cy, en |
| uk | Українська | 5,943 MB | 526 MB | uk, ga, cy, en |
| vi | Tiếng Việt | 5,915 MB | 501 MB | vi, ga, cy, en |
| zh | 中文 | 5,963 MB | 554 MB | zh, ga, cy, en |
| ga | Gaeilge | 5,912 MB | 494 MB | ga, cy, en |
| cy | Cymraeg | 5,907 MB | 494 MB | ga, cy, en |

### Cairo (`cairo`)

Base `osm-cairo-2026-11-09.zim` 0.1 GB · packs 0.1 GB · 22 versions, 2.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 118 MB | 3 MB | ar, en |
| de | Deutsch | 120 MB | 5 MB | de, ar, en |
| el | Ελληνικά | 119 MB | 4 MB | el, ar, en |
| en | English | 118 MB | 4 MB | ar, en |
| es | Español | 119 MB | 5 MB | es, ar, en |
| fa | فارسی | 119 MB | 4 MB | fa, ar, en |
| fr | Français | 120 MB | 5 MB | fr, ar, en |
| he | עברית | 119 MB | 4 MB | he, ar, en |
| hi | हिन्दी | 118 MB | 4 MB | hi, ar, en |
| id | Bahasa Indonesia | 119 MB | 5 MB | id, ar, en |
| it | Italiano | 119 MB | 5 MB | it, ar, en |
| ja | 日本語 | 119 MB | 5 MB | ja, ar, en |
| ko | 한국어 | 119 MB | 4 MB | ko, ar, en |
| nl | Nederlands | 119 MB | 5 MB | nl, ar, en |
| pl | Polski | 119 MB | 5 MB | pl, ar, en |
| pt | Português | 119 MB | 4 MB | pt, ar, en |
| ru | Русский | 119 MB | 5 MB | ru, ar, en |
| th | ไทย | 118 MB | 4 MB | th, ar, en |
| tr | Türkçe | 119 MB | 4 MB | tr, ar, en |
| uk | Українська | 120 MB | 5 MB | uk, ar, en |
| vi | Tiếng Việt | 118 MB | 4 MB | vi, ar, en |
| zh | 中文 | 119 MB | 5 MB | zh, ar, en |

### California (`california`)

Base `osm-california-2026-10-26b.zim` 2.9 GB · packs 2.4 GB · 22 versions, 67.0 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 3,046 MB | 116 MB | ar, es, en |
| de | Deutsch | 3,056 MB | 142 MB | de, es, en |
| el | Ελληνικά | 3,036 MB | 108 MB | el, es, en |
| en | English | 3,034 MB | 106 MB | es, en |
| es | Español | 3,034 MB | 106 MB | es, en |
| fa | فارسی | 3,052 MB | 122 MB | fa, es, en |
| fr | Français | 3,068 MB | 155 MB | fr, es, en |
| he | עברית | 3,038 MB | 110 MB | he, es, en |
| hi | हिन्दी | 3,035 MB | 107 MB | hi, es, en |
| id | Bahasa Indonesia | 3,040 MB | 111 MB | id, es, en |
| it | Italiano | 3,054 MB | 124 MB | it, es, en |
| ja | 日本語 | 3,047 MB | 132 MB | ja, es, en |
| ko | 한국어 | 3,041 MB | 112 MB | ko, es, en |
| nl | Nederlands | 3,044 MB | 114 MB | nl, es, en |
| pl | Polski | 3,047 MB | 118 MB | pl, es, en |
| pt | Português | 3,041 MB | 112 MB | pt, es, en |
| ru | Русский | 3,048 MB | 133 MB | ru, es, en |
| th | ไทย | 3,035 MB | 107 MB | th, es, en |
| tr | Türkçe | 3,042 MB | 112 MB | tr, es, en |
| uk | Українська | 3,051 MB | 121 MB | uk, es, en |
| vi | Tiếng Việt | 3,038 MB | 110 MB | vi, es, en |
| zh | 中文 | 3,057 MB | 139 MB | zh, es, en |

### Caucasus (`caucasus`)

Base `osm-caucasus-2026-11-10.zim` 1.4 GB · packs 2.8 GB · 28 versions, 45.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,630 MB | 204 MB | ar, ru, az, tr, en |
| de | Deutsch | 1,632 MB | 204 MB | de, ru, az, tr, en |
| el | Ελληνικά | 1,623 MB | 197 MB | el, ru, az, tr, en |
| en | English | 1,615 MB | 189 MB | ru, az, tr, en |
| es | Español | 1,639 MB | 212 MB | es, ru, az, tr, en |
| fa | فارسی | 1,640 MB | 212 MB | fa, ru, az, tr, en |
| fr | Français | 1,643 MB | 217 MB | fr, ru, az, tr, en |
| he | עברית | 1,619 MB | 194 MB | he, ru, az, tr, en |
| hi | हिन्दी | 1,615 MB | 189 MB | hi, ru, az, tr, en |
| id | Bahasa Indonesia | 1,621 MB | 195 MB | id, ru, az, tr, en |
| it | Italiano | 1,638 MB | 212 MB | it, ru, az, tr, en |
| ja | 日本語 | 1,622 MB | 196 MB | ja, ru, az, tr, en |
| ko | 한국어 | 1,619 MB | 193 MB | ko, ru, az, tr, en |
| nl | Nederlands | 1,625 MB | 199 MB | nl, ru, az, tr, en |
| pl | Polski | 1,634 MB | 208 MB | pl, ru, az, tr, en |
| pt | Português | 1,625 MB | 199 MB | pt, ru, az, tr, en |
| ru | Русский | 1,612 MB | 186 MB | ru, az, tr, en |
| th | ไทย | 1,616 MB | 190 MB | th, ru, az, tr, en |
| tr | Türkçe | 1,614 MB | 188 MB | ru, az, tr, en |
| uk | Українська | 1,651 MB | 223 MB | uk, ru, az, tr, en |
| vi | Tiếng Việt | 1,633 MB | 206 MB | vi, ru, az, tr, en |
| zh | 中文 | 1,626 MB | 200 MB | zh, ru, az, tr, en |
| az | Azərbaycanca | 1,614 MB | 188 MB | ru, az, tr, en |
| ka | ქართული | 1,660 MB | 228 MB | ka, ru, az, tr, en |
| hy | Հայերեն | 1,664 MB | 232 MB | hy, ru, az, tr, en |
| ku | Kurdî | 1,619 MB | 193 MB | ku, ru, az, tr, en |
| ce | Нохчийн | 1,661 MB | 229 MB | ce, ru, az, tr, en |
| av | Авар | 1,616 MB | 191 MB | av, ru, az, tr, en |

### Central Africa (`central-africa`)

Base `osm-central-africa-2026-10-27b.zim` 2.5 GB · packs 3.0 GB · 27 versions, 70.8 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,623 MB | 117 MB | ar, fr, ln, sw, en |
| de | Deutsch | 2,626 MB | 119 MB | de, fr, ln, sw, en |
| el | Ελληνικά | 2,619 MB | 115 MB | el, fr, ln, sw, en |
| en | English | 2,620 MB | 114 MB | fr, ln, sw, en |
| es | Español | 2,626 MB | 120 MB | es, fr, ln, sw, en |
| fa | فارسی | 2,620 MB | 117 MB | fa, fr, ln, sw, en |
| fr | Français | 2,619 MB | 114 MB | fr, ln, sw, en |
| he | עברית | 2,620 MB | 116 MB | he, fr, ln, sw, en |
| hi | हिन्दी | 2,618 MB | 115 MB | hi, fr, ln, sw, en |
| id | Bahasa Indonesia | 2,623 MB | 117 MB | id, fr, ln, sw, en |
| it | Italiano | 2,626 MB | 120 MB | it, fr, ln, sw, en |
| ja | 日本語 | 2,621 MB | 117 MB | ja, fr, ln, sw, en |
| ko | 한국어 | 2,620 MB | 116 MB | ko, fr, ln, sw, en |
| nl | Nederlands | 2,622 MB | 117 MB | nl, fr, ln, sw, en |
| pl | Polski | 2,623 MB | 119 MB | pl, fr, ln, sw, en |
| pt | Português | 2,623 MB | 118 MB | pt, fr, ln, sw, en |
| ru | Русский | 2,626 MB | 119 MB | ru, fr, ln, sw, en |
| th | ไทย | 2,618 MB | 115 MB | th, fr, ln, sw, en |
| tr | Türkçe | 2,621 MB | 116 MB | tr, fr, ln, sw, en |
| uk | Українська | 2,622 MB | 118 MB | uk, fr, ln, sw, en |
| vi | Tiếng Việt | 2,619 MB | 116 MB | vi, fr, ln, sw, en |
| zh | 中文 | 2,622 MB | 118 MB | zh, fr, ln, sw, en |
| ln | Lingála | 2,618 MB | 114 MB | fr, ln, sw, en |
| sw | Kiswahili | 2,618 MB | 114 MB | fr, ln, sw, en |
| kg | Kikongo | 2,618 MB | 115 MB | kg, fr, ln, sw, en |
| ff | Fulfulde | 2,618 MB | 115 MB | ff, fr, ln, sw, en |
| ny | Chichewa | 2,618 MB | 115 MB | ny, fr, ln, sw, en |

### Central America & Caribbean (`central-america-caribbean`)

Base `osm-central-america-caribbean-2026-10-27b.zim` 1.9 GB · packs 4.3 GB · 25 versions, 52.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,099 MB | 197 MB | ar, es, ht, jam, en |
| de | Deutsch | 2,108 MB | 221 MB | de, es, ht, jam, en |
| el | Ελληνικά | 2,092 MB | 193 MB | el, es, ht, jam, en |
| en | English | 2,088 MB | 188 MB | es, ht, jam, en |
| es | Español | 2,088 MB | 188 MB | es, ht, jam, en |
| fa | فارسی | 2,096 MB | 196 MB | fa, es, ht, jam, en |
| fr | Français | 2,123 MB | 220 MB | fr, es, ht, jam, en |
| he | עברית | 2,091 MB | 192 MB | he, es, ht, jam, en |
| hi | हिन्दी | 2,088 MB | 189 MB | hi, es, ht, jam, en |
| id | Bahasa Indonesia | 2,098 MB | 196 MB | id, es, ht, jam, en |
| it | Italiano | 2,113 MB | 210 MB | it, es, ht, jam, en |
| ja | 日本語 | 2,096 MB | 195 MB | ja, es, ht, jam, en |
| ko | 한국어 | 2,093 MB | 194 MB | ko, es, ht, jam, en |
| nl | Nederlands | 2,101 MB | 198 MB | nl, es, ht, jam, en |
| pl | Polski | 2,104 MB | 203 MB | pl, es, ht, jam, en |
| pt | Português | 2,103 MB | 202 MB | pt, es, ht, jam, en |
| ru | Русский | 2,109 MB | 221 MB | ru, es, ht, jam, en |
| th | ไทย | 2,089 MB | 190 MB | th, es, ht, jam, en |
| tr | Türkçe | 2,094 MB | 193 MB | tr, es, ht, jam, en |
| uk | Українська | 2,098 MB | 198 MB | uk, es, ht, jam, en |
| vi | Tiếng Việt | 2,096 MB | 197 MB | vi, es, ht, jam, en |
| zh | 中文 | 2,106 MB | 202 MB | zh, es, ht, jam, en |
| ht | Kreyòl ayisyen | 2,087 MB | 188 MB | es, ht, jam, en |
| jam | Patwa | 2,087 MB | 188 MB | es, ht, jam, en |
| pap | Papiamentu | 2,088 MB | 190 MB | pap, es, ht, jam, en |

### Central Asia (`central-asia`)

Base `osm-central-asia-2026-10-29.zim` 4.5 GB · packs 7.1 GB · 25 versions, 122.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 4,913 MB | 355 MB | ar, uz, ru, fa, en |
| de | Deutsch | 4,915 MB | 356 MB | de, uz, ru, fa, en |
| el | Ελληνικά | 4,901 MB | 332 MB | el, uz, ru, fa, en |
| en | English | 4,896 MB | 340 MB | uz, ru, fa, en |
| es | Español | 4,922 MB | 363 MB | es, uz, ru, fa, en |
| fa | فارسی | 4,890 MB | 323 MB | uz, ru, fa, en |
| fr | Français | 4,930 MB | 371 MB | fr, uz, ru, fa, en |
| he | עברית | 4,897 MB | 328 MB | he, uz, ru, fa, en |
| hi | हिन्दी | 4,893 MB | 326 MB | hi, uz, ru, fa, en |
| id | Bahasa Indonesia | 4,903 MB | 347 MB | id, uz, ru, fa, en |
| it | Italiano | 4,922 MB | 364 MB | it, uz, ru, fa, en |
| ja | 日本語 | 4,901 MB | 331 MB | ja, uz, ru, fa, en |
| ko | 한국어 | 4,897 MB | 328 MB | ko, uz, ru, fa, en |
| nl | Nederlands | 4,904 MB | 347 MB | nl, uz, ru, fa, en |
| pl | Polski | 4,913 MB | 343 MB | pl, uz, ru, fa, en |
| pt | Português | 4,905 MB | 348 MB | pt, uz, ru, fa, en |
| ru | Русский | 4,892 MB | 323 MB | uz, ru, fa, en |
| th | ไทย | 4,893 MB | 326 MB | th, uz, ru, fa, en |
| tr | Türkçe | 4,914 MB | 356 MB | tr, uz, ru, fa, en |
| uk | Українська | 4,940 MB | 379 MB | uk, uz, ru, fa, en |
| vi | Tiếng Việt | 4,902 MB | 334 MB | vi, uz, ru, fa, en |
| zh | 中文 | 4,904 MB | 333 MB | zh, uz, ru, fa, en |
| az | Azərbaycanca | 4,923 MB | 351 MB | az, uz, ru, fa, en |
| ka | ქართული | 4,936 MB | 378 MB | ka, uz, ru, fa, en |
| hy | Հայերեն | 4,940 MB | 378 MB | hy, uz, ru, fa, en |

### Central US (`central-us`)

Base `osm-central-us-2026-10-27b.zim` 5.4 GB · packs 4.0 GB · 22 versions, 121.9 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 5,545 MB | 190 MB | ar, es, en |
| de | Deutsch | 5,554 MB | 198 MB | de, es, en |
| el | Ελληνικά | 5,528 MB | 176 MB | el, es, en |
| en | English | 5,527 MB | 174 MB | es, en |
| es | Español | 5,528 MB | 174 MB | es, en |
| fa | فارسی | 5,550 MB | 195 MB | fa, es, en |
| fr | Français | 5,564 MB | 208 MB | fr, es, en |
| he | עברית | 5,530 MB | 177 MB | he, es, en |
| hi | हिन्दी | 5,527 MB | 175 MB | hi, es, en |
| id | Bahasa Indonesia | 5,532 MB | 177 MB | id, es, en |
| it | Italiano | 5,556 MB | 200 MB | it, es, en |
| ja | 日本語 | 5,537 MB | 182 MB | ja, es, en |
| ko | 한국어 | 5,533 MB | 180 MB | ko, es, en |
| nl | Nederlands | 5,534 MB | 178 MB | nl, es, en |
| pl | Polski | 5,544 MB | 190 MB | pl, es, en |
| pt | Português | 5,534 MB | 179 MB | pt, es, en |
| ru | Русский | 5,542 MB | 185 MB | ru, es, en |
| th | ไทย | 5,527 MB | 175 MB | th, es, en |
| tr | Türkçe | 5,536 MB | 181 MB | tr, es, en |
| uk | Українська | 5,552 MB | 197 MB | uk, es, en |
| vi | Tiếng Việt | 5,530 MB | 177 MB | vi, es, en |
| zh | 中文 | 5,552 MB | 193 MB | zh, es, en |

### Chicago Metro (`chicago-metro`)

Base `osm-chicago-metro-2026-10-25b.zim` 0.5 GB · packs 0.3 GB · 22 versions, 11.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 526 MB | 23 MB | ar, es, en |
| de | Deutsch | 526 MB | 23 MB | de, es, en |
| el | Ελληνικά | 521 MB | 18 MB | el, es, en |
| en | English | 520 MB | 17 MB | es, en |
| es | Español | 521 MB | 17 MB | es, en |
| fa | فارسی | 525 MB | 22 MB | fa, es, en |
| fr | Français | 537 MB | 34 MB | fr, es, en |
| he | עברית | 523 MB | 19 MB | he, es, en |
| hi | हिन्दी | 521 MB | 18 MB | hi, es, en |
| id | Bahasa Indonesia | 522 MB | 19 MB | id, es, en |
| it | Italiano | 527 MB | 24 MB | it, es, en |
| ja | 日本語 | 524 MB | 21 MB | ja, es, en |
| ko | 한국어 | 522 MB | 19 MB | ko, es, en |
| nl | Nederlands | 523 MB | 20 MB | nl, es, en |
| pl | Polski | 524 MB | 21 MB | pl, es, en |
| pt | Português | 523 MB | 20 MB | pt, es, en |
| ru | Русский | 525 MB | 22 MB | ru, es, en |
| th | ไทย | 521 MB | 18 MB | th, es, en |
| tr | Türkçe | 523 MB | 20 MB | tr, es, en |
| uk | Українська | 525 MB | 22 MB | uk, es, en |
| vi | Tiếng Việt | 525 MB | 22 MB | vi, es, en |
| zh | 中文 | 529 MB | 26 MB | zh, es, en |

### Colorado (`colorado`)

Base `osm-colorado-2026-10-25b.zim` 0.9 GB · packs 1.0 GB · 22 versions, 20.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 919 MB | 53 MB | ar, es, en |
| de | Deutsch | 921 MB | 54 MB | de, es, en |
| el | Ελληνικά | 916 MB | 49 MB | el, es, en |
| en | English | 916 MB | 49 MB | es, en |
| es | Español | 916 MB | 49 MB | es, en |
| fa | فارسی | 921 MB | 54 MB | fa, es, en |
| fr | Français | 925 MB | 58 MB | fr, es, en |
| he | עברית | 917 MB | 50 MB | he, es, en |
| hi | हिन्दी | 916 MB | 49 MB | hi, es, en |
| id | Bahasa Indonesia | 917 MB | 50 MB | id, es, en |
| it | Italiano | 922 MB | 56 MB | it, es, en |
| ja | 日本語 | 918 MB | 51 MB | ja, es, en |
| ko | 한국어 | 917 MB | 50 MB | ko, es, en |
| nl | Nederlands | 917 MB | 50 MB | nl, es, en |
| pl | Polski | 920 MB | 53 MB | pl, es, en |
| pt | Português | 917 MB | 50 MB | pt, es, en |
| ru | Русский | 919 MB | 52 MB | ru, es, en |
| th | ไทย | 916 MB | 49 MB | th, es, en |
| tr | Türkçe | 918 MB | 51 MB | tr, es, en |
| uk | Українська | 922 MB | 55 MB | uk, es, en |
| vi | Tiếng Việt | 916 MB | 50 MB | vi, es, en |
| zh | 中文 | 921 MB | 54 MB | zh, es, en |

### East Africa (`east-africa`)

Base `osm-east-africa-2026-10-26b.zim` 3.3 GB · packs 3.4 GB · 28 versions, 95.8 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 3,426 MB | 114 MB | ar, sw, am, om, en |
| de | Deutsch | 3,431 MB | 117 MB | de, sw, am, om, en |
| el | Ελληνικά | 3,420 MB | 111 MB | el, sw, am, om, en |
| en | English | 3,419 MB | 107 MB | sw, am, om, en |
| es | Español | 3,427 MB | 115 MB | es, sw, am, om, en |
| fa | فارسی | 3,422 MB | 112 MB | fa, sw, am, om, en |
| fr | Français | 3,432 MB | 120 MB | fr, sw, am, om, en |
| he | עברית | 3,419 MB | 110 MB | he, sw, am, om, en |
| hi | हिन्दी | 3,417 MB | 108 MB | hi, sw, am, om, en |
| id | Bahasa Indonesia | 3,423 MB | 111 MB | id, sw, am, om, en |
| it | Italiano | 3,428 MB | 115 MB | it, sw, am, om, en |
| ja | 日本語 | 3,423 MB | 112 MB | ja, sw, am, om, en |
| ko | 한국어 | 3,420 MB | 110 MB | ko, sw, am, om, en |
| nl | Nederlands | 3,423 MB | 110 MB | nl, sw, am, om, en |
| pl | Polski | 3,422 MB | 113 MB | pl, sw, am, om, en |
| pt | Português | 3,423 MB | 111 MB | pt, sw, am, om, en |
| ru | Русский | 3,427 MB | 114 MB | ru, sw, am, om, en |
| th | ไทย | 3,417 MB | 108 MB | th, sw, am, om, en |
| tr | Türkçe | 3,422 MB | 110 MB | tr, sw, am, om, en |
| uk | Українська | 3,423 MB | 113 MB | uk, sw, am, om, en |
| vi | Tiếng Việt | 3,420 MB | 110 MB | vi, sw, am, om, en |
| zh | 中文 | 3,423 MB | 112 MB | zh, sw, am, om, en |
| sw | Kiswahili | 3,416 MB | 107 MB | sw, am, om, en |
| am | አማርኛ | 3,416 MB | 107 MB | sw, am, om, en |
| om | Afaan Oromoo | 3,416 MB | 107 MB | sw, am, om, en |
| so | Soomaali | 3,420 MB | 110 MB | so, sw, am, om, en |
| ti | ትግርኛ | 3,417 MB | 108 MB | ti, sw, am, om, en |
| din | Thuɔŋjäŋ | 3,417 MB | 108 MB | din, sw, am, om, en |

### East India (`india-east`)

Base `osm-india-east-2026-11-05b.zim` 1.2 GB · packs 1.0 GB · 25 versions, 30.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,223 MB | 61 MB | ar, hi, bn, or, mai, bho, ur, en |
| de | Deutsch | 1,224 MB | 62 MB | de, hi, bn, or, mai, bho, ur, en |
| el | Ελληνικά | 1,221 MB | 60 MB | el, hi, bn, or, mai, bho, ur, en |
| en | English | 1,221 MB | 60 MB | hi, bn, or, mai, bho, ur, en |
| es | Español | 1,225 MB | 63 MB | es, hi, bn, or, mai, bho, ur, en |
| fa | فارسی | 1,223 MB | 62 MB | fa, hi, bn, or, mai, bho, ur, en |
| fr | Français | 1,226 MB | 65 MB | fr, hi, bn, or, mai, bho, ur, en |
| he | עברית | 1,221 MB | 60 MB | he, hi, bn, or, mai, bho, ur, en |
| hi | हिन्दी | 1,220 MB | 60 MB | hi, bn, or, mai, bho, ur, en |
| id | Bahasa Indonesia | 1,222 MB | 61 MB | id, hi, bn, or, mai, bho, ur, en |
| it | Italiano | 1,225 MB | 63 MB | it, hi, bn, or, mai, bho, ur, en |
| ja | 日本語 | 1,222 MB | 61 MB | ja, hi, bn, or, mai, bho, ur, en |
| ko | 한국어 | 1,221 MB | 60 MB | ko, hi, bn, or, mai, bho, ur, en |
| nl | Nederlands | 1,222 MB | 60 MB | nl, hi, bn, or, mai, bho, ur, en |
| pl | Polski | 1,222 MB | 61 MB | pl, hi, bn, or, mai, bho, ur, en |
| pt | Português | 1,222 MB | 61 MB | pt, hi, bn, or, mai, bho, ur, en |
| ru | Русский | 1,224 MB | 62 MB | ru, hi, bn, or, mai, bho, ur, en |
| th | ไทย | 1,221 MB | 60 MB | th, hi, bn, or, mai, bho, ur, en |
| tr | Türkçe | 1,222 MB | 60 MB | tr, hi, bn, or, mai, bho, ur, en |
| uk | Українська | 1,222 MB | 61 MB | uk, hi, bn, or, mai, bho, ur, en |
| vi | Tiếng Việt | 1,223 MB | 62 MB | vi, hi, bn, or, mai, bho, ur, en |
| zh | 中文 | 1,223 MB | 61 MB | zh, hi, bn, or, mai, bho, ur, en |
| bn | বাংলা | 1,220 MB | 60 MB | hi, bn, or, mai, bho, ur, en |
| or | ଓଡ଼ିଆ | 1,220 MB | 60 MB | hi, bn, or, mai, bho, ur, en |
| ur | اردو | 1,220 MB | 60 MB | hi, bn, or, mai, bho, ur, en |

### East India (Light) (`india-east-light`)

Base `osm-india-east-light-2026-11-07.zim` 0.6 GB · packs 0.9 GB · 25 versions, 15.9 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 638 MB | 38 MB | ar, hi, bn, or, mai, bho, ur, en |
| de | Deutsch | 638 MB | 38 MB | de, hi, bn, or, mai, bho, ur, en |
| el | Ελληνικά | 637 MB | 38 MB | el, hi, bn, or, mai, bho, ur, en |
| en | English | 637 MB | 37 MB | hi, bn, or, mai, bho, ur, en |
| es | Español | 638 MB | 38 MB | es, hi, bn, or, mai, bho, ur, en |
| fa | فارسی | 637 MB | 38 MB | fa, hi, bn, or, mai, bho, ur, en |
| fr | Français | 638 MB | 38 MB | fr, hi, bn, or, mai, bho, ur, en |
| he | עברית | 637 MB | 38 MB | he, hi, bn, or, mai, bho, ur, en |
| hi | हिन्दी | 636 MB | 37 MB | hi, bn, or, mai, bho, ur, en |
| id | Bahasa Indonesia | 638 MB | 38 MB | id, hi, bn, or, mai, bho, ur, en |
| it | Italiano | 638 MB | 38 MB | it, hi, bn, or, mai, bho, ur, en |
| ja | 日本語 | 637 MB | 38 MB | ja, hi, bn, or, mai, bho, ur, en |
| ko | 한국어 | 637 MB | 38 MB | ko, hi, bn, or, mai, bho, ur, en |
| nl | Nederlands | 638 MB | 38 MB | nl, hi, bn, or, mai, bho, ur, en |
| pl | Polski | 637 MB | 38 MB | pl, hi, bn, or, mai, bho, ur, en |
| pt | Português | 637 MB | 38 MB | pt, hi, bn, or, mai, bho, ur, en |
| ru | Русский | 638 MB | 38 MB | ru, hi, bn, or, mai, bho, ur, en |
| th | ไทย | 637 MB | 38 MB | th, hi, bn, or, mai, bho, ur, en |
| tr | Türkçe | 637 MB | 38 MB | tr, hi, bn, or, mai, bho, ur, en |
| uk | Українська | 637 MB | 38 MB | uk, hi, bn, or, mai, bho, ur, en |
| vi | Tiếng Việt | 637 MB | 38 MB | vi, hi, bn, or, mai, bho, ur, en |
| zh | 中文 | 638 MB | 38 MB | zh, hi, bn, or, mai, bho, ur, en |
| bn | বাংলা | 636 MB | 37 MB | hi, bn, or, mai, bho, ur, en |
| or | ଓଡ଼ିଆ | 636 MB | 37 MB | hi, bn, or, mai, bho, ur, en |
| ur | اردو | 636 MB | 37 MB | hi, bn, or, mai, bho, ur, en |

### East Polynesia (`east-polynesia`)

Base `osm-east-polynesia-2026-10-26b.zim` 0.2 GB · packs 0.4 GB · 23 versions, 5.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 226 MB | 19 MB | ar, fr, ty, en |
| de | Deutsch | 227 MB | 21 MB | de, fr, ty, en |
| el | Ελληνικά | 225 MB | 19 MB | el, fr, ty, en |
| en | English | 225 MB | 19 MB | fr, ty, en |
| es | Español | 228 MB | 21 MB | es, fr, ty, en |
| fa | فارسی | 226 MB | 19 MB | fa, fr, ty, en |
| fr | Français | 225 MB | 19 MB | fr, ty, en |
| he | עברית | 226 MB | 19 MB | he, fr, ty, en |
| hi | हिन्दी | 225 MB | 19 MB | hi, fr, ty, en |
| id | Bahasa Indonesia | 226 MB | 20 MB | id, fr, ty, en |
| it | Italiano | 227 MB | 20 MB | it, fr, ty, en |
| ja | 日本語 | 226 MB | 19 MB | ja, fr, ty, en |
| ko | 한국어 | 225 MB | 19 MB | ko, fr, ty, en |
| nl | Nederlands | 226 MB | 19 MB | nl, fr, ty, en |
| pl | Polski | 226 MB | 19 MB | pl, fr, ty, en |
| pt | Português | 227 MB | 20 MB | pt, fr, ty, en |
| ru | Русский | 226 MB | 20 MB | ru, fr, ty, en |
| th | ไทย | 225 MB | 19 MB | th, fr, ty, en |
| tr | Türkçe | 226 MB | 19 MB | tr, fr, ty, en |
| uk | Українська | 226 MB | 19 MB | uk, fr, ty, en |
| vi | Tiếng Việt | 225 MB | 19 MB | vi, fr, ty, en |
| zh | 中文 | 227 MB | 20 MB | zh, fr, ty, en |
| ty | Reo Tahiti | 225 MB | 18 MB | fr, ty, en |

### Egypt (`egypt`)

Base `osm-egypt-2026-11-10.zim` 1.2 GB · packs 0.4 GB · 22 versions, 26.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,204 MB | 22 MB | ar, en |
| de | Deutsch | 1,210 MB | 28 MB | de, ar, en |
| el | Ελληνικά | 1,207 MB | 24 MB | el, ar, en |
| en | English | 1,205 MB | 22 MB | ar, en |
| es | Español | 1,210 MB | 28 MB | es, ar, en |
| fa | فارسی | 1,207 MB | 25 MB | fa, ar, en |
| fr | Français | 1,212 MB | 30 MB | fr, ar, en |
| he | עברית | 1,209 MB | 26 MB | he, ar, en |
| hi | हिन्दी | 1,205 MB | 23 MB | hi, ar, en |
| id | Bahasa Indonesia | 1,207 MB | 25 MB | id, ar, en |
| it | Italiano | 1,210 MB | 27 MB | it, ar, en |
| ja | 日本語 | 1,207 MB | 25 MB | ja, ar, en |
| ko | 한국어 | 1,206 MB | 24 MB | ko, ar, en |
| nl | Nederlands | 1,208 MB | 26 MB | nl, ar, en |
| pl | Polski | 1,208 MB | 26 MB | pl, ar, en |
| pt | Português | 1,208 MB | 25 MB | pt, ar, en |
| ru | Русский | 1,210 MB | 27 MB | ru, ar, en |
| th | ไทย | 1,206 MB | 23 MB | th, ar, en |
| tr | Türkçe | 1,207 MB | 24 MB | tr, ar, en |
| uk | Українська | 1,209 MB | 27 MB | uk, ar, en |
| vi | Tiếng Việt | 1,206 MB | 24 MB | vi, ar, en |
| zh | 中文 | 1,208 MB | 25 MB | zh, ar, en |

### Florida (`florida`)

Base `osm-florida-2026-10-25b.zim` 1.8 GB · packs 0.7 GB · 22 versions, 39.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,791 MB | 42 MB | ar, es, en |
| de | Deutsch | 1,799 MB | 50 MB | de, es, en |
| el | Ελληνικά | 1,786 MB | 37 MB | el, es, en |
| en | English | 1,785 MB | 36 MB | es, en |
| es | Español | 1,785 MB | 36 MB | es, en |
| fa | فارسی | 1,794 MB | 45 MB | fa, es, en |
| fr | Français | 1,798 MB | 49 MB | fr, es, en |
| he | עברית | 1,787 MB | 38 MB | he, es, en |
| hi | हिन्दी | 1,786 MB | 36 MB | hi, es, en |
| id | Bahasa Indonesia | 1,787 MB | 38 MB | id, es, en |
| it | Italiano | 1,795 MB | 46 MB | it, es, en |
| ja | 日本語 | 1,790 MB | 41 MB | ja, es, en |
| ko | 한국어 | 1,787 MB | 38 MB | ko, es, en |
| nl | Nederlands | 1,788 MB | 39 MB | nl, es, en |
| pl | Polski | 1,791 MB | 42 MB | pl, es, en |
| pt | Português | 1,788 MB | 39 MB | pt, es, en |
| ru | Русский | 1,792 MB | 43 MB | ru, es, en |
| th | ไทย | 1,786 MB | 37 MB | th, es, en |
| tr | Türkçe | 1,788 MB | 39 MB | tr, es, en |
| uk | Українська | 1,794 MB | 45 MB | uk, es, en |
| vi | Tiếng Việt | 1,787 MB | 37 MB | vi, es, en |
| zh | 中文 | 1,797 MB | 48 MB | zh, es, en |

### Germany (`germany`)

Base `osm-germany-2026-10-23b.zim` 7.4 GB · packs 15.4 GB · 22 versions, 205.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 9,290 MB | 1,889 MB | ar, de, en |
| de | Deutsch | 9,221 MB | 1,829 MB | de, en |
| el | Ελληνικά | 9,247 MB | 1,855 MB | el, de, en |
| en | English | 9,231 MB | 1,829 MB | de, en |
| es | Español | 9,370 MB | 1,963 MB | es, de, en |
| fa | فارسی | 9,308 MB | 1,909 MB | fa, de, en |
| fr | Français | 9,516 MB | 2,114 MB | fr, de, en |
| he | עברית | 9,249 MB | 1,856 MB | he, de, en |
| hi | हिन्दी | 9,236 MB | 1,830 MB | hi, de, en |
| id | Bahasa Indonesia | 9,273 MB | 1,872 MB | id, de, en |
| it | Italiano | 9,391 MB | 1,986 MB | it, de, en |
| ja | 日本語 | 9,299 MB | 1,892 MB | ja, de, en |
| ko | 한국어 | 9,253 MB | 1,860 MB | ko, de, en |
| nl | Nederlands | 9,433 MB | 2,028 MB | nl, de, en |
| pl | Polski | 9,425 MB | 2,021 MB | pl, de, en |
| pt | Português | 9,396 MB | 2,002 MB | pt, de, en |
| ru | Русский | 9,440 MB | 2,042 MB | ru, de, en |
| th | ไทย | 9,240 MB | 1,833 MB | th, de, en |
| tr | Türkçe | 9,277 MB | 1,881 MB | tr, de, en |
| uk | Українська | 9,371 MB | 1,970 MB | uk, de, en |
| vi | Tiếng Việt | 9,343 MB | 1,949 MB | vi, de, en |
| zh | 中文 | 9,384 MB | 1,991 MB | zh, de, en |

### Greater Los Angeles (`greater-la`)

Base `osm-greater-la-2026-10-24b.zim` 0.7 GB · packs 0.4 GB · 22 versions, 15.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 713 MB | 22 MB | ar, es, en |
| de | Deutsch | 716 MB | 26 MB | de, es, en |
| el | Ελληνικά | 710 MB | 19 MB | el, es, en |
| en | English | 709 MB | 18 MB | es, en |
| es | Español | 709 MB | 18 MB | es, en |
| fa | فارسی | 714 MB | 24 MB | fa, es, en |
| fr | Français | 720 MB | 29 MB | fr, es, en |
| he | עברית | 711 MB | 21 MB | he, es, en |
| hi | हिन्दी | 709 MB | 19 MB | hi, es, en |
| id | Bahasa Indonesia | 711 MB | 21 MB | id, es, en |
| it | Italiano | 715 MB | 25 MB | it, es, en |
| ja | 日本語 | 715 MB | 24 MB | ja, es, en |
| ko | 한국어 | 712 MB | 22 MB | ko, es, en |
| nl | Nederlands | 712 MB | 22 MB | nl, es, en |
| pl | Polski | 713 MB | 23 MB | pl, es, en |
| pt | Português | 711 MB | 21 MB | pt, es, en |
| ru | Русский | 713 MB | 23 MB | ru, es, en |
| th | ไทย | 710 MB | 20 MB | th, es, en |
| tr | Türkçe | 711 MB | 21 MB | tr, es, en |
| uk | Українська | 714 MB | 24 MB | uk, es, en |
| vi | Tiếng Việt | 710 MB | 20 MB | vi, es, en |
| zh | 中文 | 716 MB | 25 MB | zh, es, en |

### Greece (`greece`)

Base `osm-greece-2026-11-10.zim` 1.1 GB · packs 1.6 GB · 22 versions, 26.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,208 MB | 131 MB | ar, el, en |
| de | Deutsch | 1,209 MB | 132 MB | de, el, en |
| el | Ελληνικά | 1,192 MB | 108 MB | el, en |
| en | English | 1,193 MB | 108 MB | el, en |
| es | Español | 1,209 MB | 131 MB | es, el, en |
| fa | فارسی | 1,197 MB | 112 MB | fa, el, en |
| fr | Français | 1,246 MB | 169 MB | fr, el, en |
| he | עברית | 1,196 MB | 111 MB | he, el, en |
| hi | हिन्दी | 1,193 MB | 108 MB | hi, el, en |
| id | Bahasa Indonesia | 1,197 MB | 120 MB | id, el, en |
| it | Italiano | 1,213 MB | 136 MB | it, el, en |
| ja | 日本語 | 1,198 MB | 121 MB | ja, el, en |
| ko | 한국어 | 1,197 MB | 111 MB | ko, el, en |
| nl | Nederlands | 1,200 MB | 123 MB | nl, el, en |
| pl | Polski | 1,204 MB | 126 MB | pl, el, en |
| pt | Português | 1,200 MB | 123 MB | pt, el, en |
| ru | Русский | 1,214 MB | 137 MB | ru, el, en |
| th | ไทย | 1,194 MB | 109 MB | th, el, en |
| tr | Türkçe | 1,203 MB | 126 MB | tr, el, en |
| uk | Українська | 1,206 MB | 129 MB | uk, el, en |
| vi | Tiếng Việt | 1,196 MB | 111 MB | vi, el, en |
| zh | 中文 | 1,201 MB | 124 MB | zh, el, en |

### Hawaii (`hawaii`)

Base `osm-hawaii-2026-10-26b.zim` 0.1 GB · packs 0.2 GB · 23 versions, 3.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 149 MB | 7 MB | ar, haw, en |
| de | Deutsch | 151 MB | 8 MB | de, haw, en |
| el | Ελληνικά | 149 MB | 6 MB | el, haw, en |
| en | English | 149 MB | 6 MB | haw, en |
| es | Español | 151 MB | 9 MB | es, haw, en |
| fa | فارسی | 150 MB | 8 MB | fa, haw, en |
| fr | Français | 152 MB | 9 MB | fr, haw, en |
| he | עברית | 150 MB | 7 MB | he, haw, en |
| hi | हिन्दी | 149 MB | 6 MB | hi, haw, en |
| id | Bahasa Indonesia | 150 MB | 7 MB | id, haw, en |
| it | Italiano | 151 MB | 8 MB | it, haw, en |
| ja | 日本語 | 152 MB | 9 MB | ja, haw, en |
| ko | 한국어 | 149 MB | 7 MB | ko, haw, en |
| nl | Nederlands | 150 MB | 7 MB | nl, haw, en |
| pl | Polski | 150 MB | 7 MB | pl, haw, en |
| pt | Português | 150 MB | 7 MB | pt, haw, en |
| ru | Русский | 151 MB | 8 MB | ru, haw, en |
| th | ไทย | 149 MB | 6 MB | th, haw, en |
| tr | Türkçe | 149 MB | 6 MB | tr, haw, en |
| uk | Українська | 151 MB | 8 MB | uk, haw, en |
| vi | Tiếng Việt | 149 MB | 6 MB | vi, haw, en |
| zh | 中文 | 151 MB | 8 MB | zh, haw, en |
| haw | ʻŌlelo Hawaiʻi | 149 MB | 6 MB | haw, en |

### Himalayas (`himalayas`)

Base `osm-himalayas-2026-10-26b.zim` 5.3 GB · packs 5.2 GB · 21 versions, 117.0 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 5,574 MB | 246 MB | ar, ne, hi, ur, en |
| de | Deutsch | 5,583 MB | 253 MB | de, ne, hi, ur, en |
| el | Ελληνικά | 5,559 MB | 241 MB | el, ne, hi, ur, en |
| en | English | 5,566 MB | 239 MB | ne, hi, ur, en |
| es | Español | 5,579 MB | 251 MB | es, ne, hi, ur, en |
| fa | فارسی | 5,568 MB | 249 MB | fa, ne, hi, ur, en |
| fr | Français | 5,586 MB | 258 MB | fr, ne, hi, ur, en |
| he | עברית | 5,559 MB | 241 MB | he, ne, hi, ur, en |
| id | Bahasa Indonesia | 5,572 MB | 244 MB | id, ne, hi, ur, en |
| it | Italiano | 5,581 MB | 253 MB | it, ne, hi, ur, en |
| ja | 日本語 | 5,565 MB | 246 MB | ja, ne, hi, ur, en |
| ko | 한국어 | 5,561 MB | 243 MB | ko, ne, hi, ur, en |
| nl | Nederlands | 5,572 MB | 244 MB | nl, ne, hi, ur, en |
| pl | Polski | 5,567 MB | 248 MB | pl, ne, hi, ur, en |
| pt | Português | 5,573 MB | 245 MB | pt, ne, hi, ur, en |
| ru | Русский | 5,581 MB | 251 MB | ru, ne, hi, ur, en |
| th | ไทย | 5,560 MB | 243 MB | th, ne, hi, ur, en |
| tr | Türkçe | 5,570 MB | 242 MB | tr, ne, hi, ur, en |
| uk | Українська | 5,567 MB | 249 MB | uk, ne, hi, ur, en |
| vi | Tiếng Việt | 5,565 MB | 247 MB | vi, ne, hi, ur, en |
| zh | 中文 | 5,572 MB | 249 MB | zh, ne, hi, ur, en |

### Hispaniola (`hispaniola`)

Base `osm-hispaniola-2026-10-26b.zim` 0.2 GB · packs 0.4 GB · 23 versions, 4.9 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 215 MB | 25 MB | ar, ht, es, fr, en |
| de | Deutsch | 215 MB | 24 MB | de, ht, es, fr, en |
| el | Ελληνικά | 213 MB | 23 MB | el, ht, es, fr, en |
| en | English | 213 MB | 22 MB | ht, es, fr, en |
| es | Español | 213 MB | 22 MB | ht, es, fr, en |
| fa | فارسی | 214 MB | 23 MB | fa, ht, es, fr, en |
| fr | Français | 213 MB | 22 MB | ht, es, fr, en |
| he | עברית | 213 MB | 23 MB | he, ht, es, fr, en |
| hi | हिन्दी | 213 MB | 23 MB | hi, ht, es, fr, en |
| id | Bahasa Indonesia | 214 MB | 23 MB | id, ht, es, fr, en |
| it | Italiano | 216 MB | 25 MB | it, ht, es, fr, en |
| ja | 日本語 | 214 MB | 24 MB | ja, ht, es, fr, en |
| ko | 한국어 | 214 MB | 23 MB | ko, ht, es, fr, en |
| nl | Nederlands | 214 MB | 24 MB | nl, ht, es, fr, en |
| pl | Polski | 214 MB | 24 MB | pl, ht, es, fr, en |
| pt | Português | 214 MB | 24 MB | pt, ht, es, fr, en |
| ru | Русский | 215 MB | 24 MB | ru, ht, es, fr, en |
| th | ไทย | 213 MB | 23 MB | th, ht, es, fr, en |
| tr | Türkçe | 213 MB | 23 MB | tr, ht, es, fr, en |
| uk | Українська | 214 MB | 24 MB | uk, ht, es, fr, en |
| vi | Tiếng Việt | 213 MB | 23 MB | vi, ht, es, fr, en |
| zh | 中文 | 215 MB | 25 MB | zh, ht, es, fr, en |
| ht | Kreyòl ayisyen | 213 MB | 22 MB | ht, es, fr, en |

### Horn of Africa (`horn-of-africa`)

Base `osm-horn-of-africa-2026-10-26b.zim` 1.7 GB · packs 1.4 GB · 27 versions, 47.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,770 MB | 58 MB | ar, am, om, so, en |
| de | Deutsch | 1,770 MB | 57 MB | de, am, om, so, en |
| el | Ελληνικά | 1,765 MB | 54 MB | el, am, om, so, en |
| en | English | 1,765 MB | 54 MB | am, om, so, en |
| es | Español | 1,769 MB | 57 MB | es, am, om, so, en |
| fa | فارسی | 1,767 MB | 56 MB | fa, am, om, so, en |
| fr | Français | 1,772 MB | 60 MB | fr, am, om, so, en |
| he | עברית | 1,765 MB | 55 MB | he, am, om, so, en |
| hi | हिन्दी | 1,764 MB | 54 MB | hi, am, om, so, en |
| id | Bahasa Indonesia | 1,767 MB | 56 MB | id, am, om, so, en |
| it | Italiano | 1,770 MB | 58 MB | it, am, om, so, en |
| ja | 日本語 | 1,768 MB | 56 MB | ja, am, om, so, en |
| ko | 한국어 | 1,765 MB | 55 MB | ko, am, om, so, en |
| nl | Nederlands | 1,767 MB | 55 MB | nl, am, om, so, en |
| pl | Polski | 1,767 MB | 57 MB | pl, am, om, so, en |
| pt | Português | 1,767 MB | 56 MB | pt, am, om, so, en |
| ru | Русский | 1,770 MB | 58 MB | ru, am, om, so, en |
| th | ไทย | 1,764 MB | 54 MB | th, am, om, so, en |
| tr | Türkçe | 1,766 MB | 55 MB | tr, am, om, so, en |
| uk | Українська | 1,767 MB | 57 MB | uk, am, om, so, en |
| zh | 中文 | 1,768 MB | 56 MB | zh, am, om, so, en |
| am | አማርኛ | 1,764 MB | 54 MB | am, om, so, en |
| om | Afaan Oromoo | 1,764 MB | 54 MB | am, om, so, en |
| so | Soomaali | 1,764 MB | 54 MB | am, om, so, en |
| ti | ትግርኛ | 1,764 MB | 54 MB | ti, am, om, so, en |
| din | Thuɔŋjäŋ | 1,764 MB | 54 MB | din, am, om, so, en |
| sw | Kiswahili | 1,766 MB | 55 MB | sw, am, om, so, en |

### Iberia (`iberia`)

Base `osm-iberia-2026-10-26b.zim` 5.3 GB · packs 15.7 GB · 25 versions, 166.8 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 6,680 MB | 1,428 MB | ar, es, pt, ca, en |
| de | Deutsch | 6,896 MB | 1,650 MB | de, es, pt, ca, en |
| el | Ελληνικά | 6,618 MB | 1,362 MB | el, es, pt, ca, en |
| en | English | 6,614 MB | 1,372 MB | es, pt, ca, en |
| es | Español | 6,609 MB | 1,355 MB | es, pt, ca, en |
| fa | فارسی | 6,641 MB | 1,384 MB | fa, es, pt, ca, en |
| fr | Français | 6,781 MB | 1,527 MB | fr, es, pt, ca, en |
| he | עברית | 6,616 MB | 1,360 MB | he, es, pt, ca, en |
| hi | हिन्दी | 6,612 MB | 1,357 MB | hi, es, pt, ca, en |
| id | Bahasa Indonesia | 6,633 MB | 1,383 MB | id, es, pt, ca, en |
| it | Italiano | 6,722 MB | 1,471 MB | it, es, pt, ca, en |
| ja | 日本語 | 6,630 MB | 1,387 MB | ja, es, pt, ca, en |
| ko | 한국어 | 6,618 MB | 1,362 MB | ko, es, pt, ca, en |
| nl | Nederlands | 6,650 MB | 1,393 MB | nl, es, pt, ca, en |
| pl | Polski | 6,708 MB | 1,464 MB | pl, es, pt, ca, en |
| pt | Português | 6,609 MB | 1,355 MB | es, pt, ca, en |
| ru | Русский | 6,747 MB | 1,505 MB | ru, es, pt, ca, en |
| th | ไทย | 6,612 MB | 1,358 MB | th, es, pt, ca, en |
| tr | Türkçe | 6,642 MB | 1,394 MB | tr, es, pt, ca, en |
| uk | Українська | 6,692 MB | 1,446 MB | uk, es, pt, ca, en |
| vi | Tiếng Việt | 6,669 MB | 1,412 MB | vi, es, pt, ca, en |
| zh | 中文 | 6,663 MB | 1,415 MB | zh, es, pt, ca, en |
| ca | Català | 6,604 MB | 1,355 MB | es, pt, ca, en |
| gl | Galego | 6,796 MB | 1,550 MB | gl, es, pt, ca, en |
| eu | Euskara | 6,784 MB | 1,539 MB | eu, es, pt, ca, en |

### Iceland (`iceland`)

Base `osm-iceland-2026-10-26b.zim` 0.3 GB · packs 0.6 GB · 23 versions, 8.0 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 347 MB | 27 MB | ar, is, en |
| de | Deutsch | 350 MB | 31 MB | de, is, en |
| el | Ελληνικά | 346 MB | 27 MB | el, is, en |
| en | English | 346 MB | 26 MB | is, en |
| es | Español | 348 MB | 29 MB | es, is, en |
| fa | فارسی | 347 MB | 27 MB | fa, is, en |
| fr | Français | 350 MB | 31 MB | fr, is, en |
| he | עברית | 346 MB | 27 MB | he, is, en |
| hi | हिन्दी | 346 MB | 26 MB | hi, is, en |
| id | Bahasa Indonesia | 347 MB | 28 MB | id, is, en |
| it | Italiano | 349 MB | 29 MB | it, is, en |
| ja | 日本語 | 347 MB | 28 MB | ja, is, en |
| ko | 한국어 | 347 MB | 27 MB | ko, is, en |
| nl | Nederlands | 348 MB | 29 MB | nl, is, en |
| pl | Polski | 348 MB | 29 MB | pl, is, en |
| pt | Português | 348 MB | 28 MB | pt, is, en |
| ru | Русский | 349 MB | 30 MB | ru, is, en |
| th | ไทย | 346 MB | 26 MB | th, is, en |
| tr | Türkçe | 347 MB | 27 MB | tr, is, en |
| uk | Українська | 348 MB | 28 MB | uk, is, en |
| vi | Tiếng Việt | 346 MB | 27 MB | vi, is, en |
| zh | 中文 | 347 MB | 28 MB | zh, is, en |
| is | Íslenska | 346 MB | 26 MB | is, en |

### Iran (`iran`)

Base `osm-iran-2026-10-27b.zim` 2.0 GB · packs 4.3 GB · 26 versions, 57.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,252 MB | 270 MB | ar, fa, az, ku, glk, mzn, en |
| de | Deutsch | 2,222 MB | 227 MB | de, fa, az, ku, glk, mzn, en |
| el | Ελληνικά | 2,212 MB | 221 MB | el, fa, az, ku, glk, mzn, en |
| en | English | 2,214 MB | 220 MB | fa, az, ku, glk, mzn, en |
| es | Español | 2,219 MB | 225 MB | es, fa, az, ku, glk, mzn, en |
| fa | فارسی | 2,211 MB | 220 MB | fa, az, ku, glk, mzn, en |
| fr | Français | 2,222 MB | 228 MB | fr, fa, az, ku, glk, mzn, en |
| he | עברית | 2,215 MB | 223 MB | he, fa, az, ku, glk, mzn, en |
| hi | हिन्दी | 2,212 MB | 221 MB | hi, fa, az, ku, glk, mzn, en |
| id | Bahasa Indonesia | 2,218 MB | 223 MB | id, fa, az, ku, glk, mzn, en |
| it | Italiano | 2,225 MB | 230 MB | it, fa, az, ku, glk, mzn, en |
| ja | 日本語 | 2,214 MB | 223 MB | ja, fa, az, ku, glk, mzn, en |
| ko | 한국어 | 2,213 MB | 222 MB | ko, fa, az, ku, glk, mzn, en |
| nl | Nederlands | 2,217 MB | 222 MB | nl, fa, az, ku, glk, mzn, en |
| pl | Polski | 2,215 MB | 224 MB | pl, fa, az, ku, glk, mzn, en |
| pt | Português | 2,217 MB | 223 MB | pt, fa, az, ku, glk, mzn, en |
| ru | Русский | 2,225 MB | 230 MB | ru, fa, az, ku, glk, mzn, en |
| th | ไทย | 2,213 MB | 222 MB | th, fa, az, ku, glk, mzn, en |
| tr | Türkçe | 2,221 MB | 226 MB | tr, fa, az, ku, glk, mzn, en |
| uk | Українська | 2,220 MB | 228 MB | uk, fa, az, ku, glk, mzn, en |
| vi | Tiếng Việt | 2,212 MB | 222 MB | vi, fa, az, ku, glk, mzn, en |
| zh | 中文 | 2,215 MB | 223 MB | zh, fa, az, ku, glk, mzn, en |
| az | Azərbaycanca | 2,211 MB | 220 MB | fa, az, ku, glk, mzn, en |
| ku | Kurdî | 2,211 MB | 220 MB | fa, az, ku, glk, mzn, en |
| glk | گیلکی | 2,211 MB | 220 MB | fa, az, ku, glk, mzn, en |
| mzn | مازِرونی | 2,211 MB | 220 MB | fa, az, ku, glk, mzn, en |

### Italy (`italy`)

Base `osm-italy-2026-10-26b.zim` 5.6 GB · packs 9.9 GB · 23 versions, 149.9 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 6,519 MB | 971 MB | ar, it, sc, en |
| de | Deutsch | 6,626 MB | 1,079 MB | de, it, sc, en |
| el | Ελληνικά | 6,499 MB | 938 MB | el, it, sc, en |
| en | English | 6,446 MB | 888 MB | it, sc, en |
| es | Español | 6,563 MB | 1,016 MB | es, it, sc, en |
| fa | فارسی | 6,498 MB | 937 MB | fa, it, sc, en |
| fr | Français | 6,698 MB | 1,145 MB | fr, it, sc, en |
| he | עברית | 6,456 MB | 899 MB | he, it, sc, en |
| hi | हिन्दी | 6,443 MB | 889 MB | hi, it, sc, en |
| id | Bahasa Indonesia | 6,501 MB | 938 MB | id, it, sc, en |
| it | Italiano | 6,437 MB | 888 MB | it, sc, en |
| ja | 日本語 | 6,523 MB | 972 MB | ja, it, sc, en |
| ko | 한국어 | 6,487 MB | 926 MB | ko, it, sc, en |
| nl | Nederlands | 6,527 MB | 973 MB | nl, it, sc, en |
| pl | Polski | 6,548 MB | 986 MB | pl, it, sc, en |
| pt | Português | 6,581 MB | 1,022 MB | pt, it, sc, en |
| ru | Русский | 6,569 MB | 1,015 MB | ru, it, sc, en |
| th | ไทย | 6,447 MB | 892 MB | th, it, sc, en |
| tr | Türkçe | 6,503 MB | 941 MB | tr, it, sc, en |
| uk | Українська | 6,557 MB | 994 MB | uk, it, sc, en |
| vi | Tiếng Việt | 6,507 MB | 950 MB | vi, it, sc, en |
| zh | 中文 | 6,525 MB | 972 MB | zh, it, sc, en |
| sc | Sardu | 6,442 MB | 888 MB | it, sc, en |

### Japan (`japan`)

Base `osm-japan-2026-10-27b.zim` 4.4 GB · packs 12.2 GB · 22 versions, 123.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 5,601 MB | 1,157 MB | ar, ja, en |
| de | Deutsch | 5,627 MB | 1,196 MB | de, ja, en |
| el | Ελληνικά | 5,572 MB | 1,143 MB | el, ja, en |
| en | English | 5,584 MB | 1,141 MB | ja, en |
| es | Español | 5,616 MB | 1,187 MB | es, ja, en |
| fa | فارسی | 5,595 MB | 1,164 MB | fa, ja, en |
| fr | Français | 5,677 MB | 1,245 MB | fr, ja, en |
| he | עברית | 5,573 MB | 1,144 MB | he, ja, en |
| hi | हिन्दी | 5,570 MB | 1,142 MB | hi, ja, en |
| id | Bahasa Indonesia | 5,663 MB | 1,233 MB | id, ja, en |
| it | Italiano | 5,644 MB | 1,214 MB | it, ja, en |
| ja | 日本語 | 5,569 MB | 1,141 MB | ja, en |
| ko | 한국어 | 5,704 MB | 1,275 MB | ko, ja, en |
| nl | Nederlands | 5,601 MB | 1,172 MB | nl, ja, en |
| pl | Polski | 5,595 MB | 1,164 MB | pl, ja, en |
| pt | Português | 5,602 MB | 1,158 MB | pt, ja, en |
| ru | Русский | 5,622 MB | 1,191 MB | ru, ja, en |
| th | ไทย | 5,585 MB | 1,155 MB | th, ja, en |
| tr | Türkçe | 5,608 MB | 1,179 MB | tr, ja, en |
| uk | Українська | 5,593 MB | 1,162 MB | uk, ja, en |
| vi | Tiếng Việt | 5,587 MB | 1,158 MB | vi, ja, en |
| zh | 中文 | 5,746 MB | 1,315 MB | zh, ja, en |

### Korea & Mongolia (`korea-mongolia`)

Base `osm-korea-mongolia-2026-10-27b.zim` 2.1 GB · packs 2.1 GB · 22 versions, 49.0 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,227 MB | 137 MB | ar, ko, mn, en |
| de | Deutsch | 2,230 MB | 140 MB | de, ko, mn, en |
| el | Ελληνικά | 2,221 MB | 134 MB | el, ko, mn, en |
| en | English | 2,223 MB | 133 MB | ko, mn, en |
| es | Español | 2,231 MB | 141 MB | es, ko, mn, en |
| fa | فارسی | 2,227 MB | 139 MB | fa, ko, mn, en |
| fr | Français | 2,242 MB | 151 MB | fr, ko, mn, en |
| he | עברית | 2,222 MB | 135 MB | he, ko, mn, en |
| hi | हिन्दी | 2,220 MB | 134 MB | hi, ko, mn, en |
| id | Bahasa Indonesia | 2,228 MB | 138 MB | id, ko, mn, en |
| it | Italiano | 2,232 MB | 142 MB | it, ko, mn, en |
| ja | 日本語 | 2,248 MB | 155 MB | ja, ko, mn, en |
| ko | 한국어 | 2,220 MB | 133 MB | ko, mn, en |
| nl | Nederlands | 2,226 MB | 136 MB | nl, ko, mn, en |
| pl | Polski | 2,226 MB | 138 MB | pl, ko, mn, en |
| pt | Português | 2,226 MB | 136 MB | pt, ko, mn, en |
| ru | Русский | 2,235 MB | 143 MB | ru, ko, mn, en |
| th | ไทย | 2,223 MB | 136 MB | th, ko, mn, en |
| tr | Türkçe | 2,228 MB | 137 MB | tr, ko, mn, en |
| uk | Українська | 2,227 MB | 138 MB | uk, ko, mn, en |
| vi | Tiếng Việt | 2,237 MB | 149 MB | vi, ko, mn, en |
| zh | 中文 | 2,247 MB | 153 MB | zh, ko, mn, en |

### Kyiv (`kyiv`)

Base `osm-kyiv-2026-11-09.zim` 0.1 GB · packs 0.2 GB · 22 versions, 2.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 109 MB | 28 MB | ar, uk, ru, en |
| de | Deutsch | 111 MB | 30 MB | de, uk, ru, en |
| el | Ελληνικά | 109 MB | 28 MB | el, uk, ru, en |
| en | English | 108 MB | 27 MB | uk, ru, en |
| es | Español | 110 MB | 29 MB | es, uk, ru, en |
| fa | فارسی | 109 MB | 28 MB | fa, uk, ru, en |
| fr | Français | 111 MB | 30 MB | fr, uk, ru, en |
| he | עברית | 109 MB | 28 MB | he, uk, ru, en |
| hi | हिन्दी | 108 MB | 27 MB | hi, uk, ru, en |
| id | Bahasa Indonesia | 109 MB | 28 MB | id, uk, ru, en |
| it | Italiano | 111 MB | 30 MB | it, uk, ru, en |
| ja | 日本語 | 109 MB | 28 MB | ja, uk, ru, en |
| ko | 한국어 | 109 MB | 28 MB | ko, uk, ru, en |
| nl | Nederlands | 109 MB | 28 MB | nl, uk, ru, en |
| pl | Polski | 111 MB | 30 MB | pl, uk, ru, en |
| pt | Português | 109 MB | 28 MB | pt, uk, ru, en |
| ru | Русский | 107 MB | 26 MB | uk, ru, en |
| th | ไทย | 108 MB | 27 MB | th, uk, ru, en |
| tr | Türkçe | 109 MB | 28 MB | tr, uk, ru, en |
| uk | Українська | 108 MB | 27 MB | uk, ru, en |
| vi | Tiếng Việt | 108 MB | 27 MB | vi, uk, ru, en |
| zh | 中文 | 110 MB | 29 MB | zh, uk, ru, en |

### Mexico (`mexico`)

Base `osm-mexico-2026-10-27b.zim` 4.3 GB · packs 4.4 GB · 22 versions, 98.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 4,481 MB | 217 MB | ar, es, en |
| de | Deutsch | 4,488 MB | 222 MB | de, es, en |
| el | Ελληνικά | 4,479 MB | 217 MB | el, es, en |
| en | English | 4,477 MB | 213 MB | es, en |
| es | Español | 4,476 MB | 213 MB | es, en |
| fa | فارسی | 4,478 MB | 217 MB | fa, es, en |
| fr | Français | 4,500 MB | 234 MB | fr, es, en |
| he | עברית | 4,476 MB | 214 MB | he, es, en |
| hi | हिन्दी | 4,474 MB | 213 MB | hi, es, en |
| id | Bahasa Indonesia | 4,481 MB | 216 MB | id, es, en |
| it | Italiano | 4,493 MB | 228 MB | it, es, en |
| ja | 日本語 | 4,479 MB | 216 MB | ja, es, en |
| ko | 한국어 | 4,477 MB | 215 MB | ko, es, en |
| nl | Nederlands | 4,480 MB | 215 MB | nl, es, en |
| pl | Polski | 4,481 MB | 219 MB | pl, es, en |
| pt | Português | 4,489 MB | 225 MB | pt, es, en |
| ru | Русский | 4,497 MB | 230 MB | ru, es, en |
| th | ไทย | 4,475 MB | 214 MB | th, es, en |
| tr | Türkçe | 4,483 MB | 218 MB | tr, es, en |
| uk | Українська | 4,479 MB | 217 MB | uk, es, en |
| vi | Tiếng Việt | 4,489 MB | 227 MB | vi, es, en |
| zh | 中文 | 4,480 MB | 217 MB | zh, es, en |

### Midwest US (`midwest-us`)

Base `osm-midwest-us-2026-10-25b.zim` 7.6 GB · packs 7.2 GB · 22 versions, 174.1 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 7,968 MB | 424 MB | ar, es, en |
| de | Deutsch | 7,934 MB | 395 MB | de, es, en |
| el | Ελληνικά | 7,876 MB | 328 MB | el, es, en |
| en | English | 7,869 MB | 321 MB | es, en |
| es | Español | 7,870 MB | 321 MB | es, en |
| fa | فارسی | 7,958 MB | 419 MB | fa, es, en |
| fr | Français | 8,005 MB | 464 MB | fr, es, en |
| he | עברית | 7,875 MB | 326 MB | he, es, en |
| hi | हिन्दी | 7,870 MB | 323 MB | hi, es, en |
| id | Bahasa Indonesia | 7,879 MB | 327 MB | id, es, en |
| it | Italiano | 7,982 MB | 443 MB | it, es, en |
| ja | 日本語 | 7,898 MB | 344 MB | ja, es, en |
| ko | 한국어 | 7,887 MB | 337 MB | ko, es, en |
| nl | Nederlands | 7,885 MB | 346 MB | nl, es, en |
| pl | Polski | 7,932 MB | 379 MB | pl, es, en |
| pt | Português | 7,889 MB | 350 MB | pt, es, en |
| ru | Русский | 7,909 MB | 370 MB | ru, es, en |
| th | ไทย | 7,870 MB | 323 MB | th, es, en |
| tr | Türkçe | 7,895 MB | 343 MB | tr, es, en |
| uk | Українська | 7,962 MB | 421 MB | uk, es, en |
| vi | Tiếng Việt | 7,941 MB | 388 MB | vi, es, en |
| zh | 中文 | 7,991 MB | 437 MB | zh, es, en |

### Moscow (`moscow`)

Base `osm-moscow-2026-11-09.zim` 0.1 GB · packs 0.4 GB · 22 versions, 4.1 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 183 MB | 40 MB | ar, ru, en |
| de | Deutsch | 186 MB | 43 MB | de, ru, en |
| el | Ελληνικά | 182 MB | 39 MB | el, ru, en |
| en | English | 181 MB | 38 MB | ru, en |
| es | Español | 184 MB | 41 MB | es, ru, en |
| fa | فارسی | 182 MB | 40 MB | fa, ru, en |
| fr | Français | 189 MB | 46 MB | fr, ru, en |
| he | עברית | 185 MB | 42 MB | he, ru, en |
| hi | हिन्दी | 181 MB | 38 MB | hi, ru, en |
| id | Bahasa Indonesia | 183 MB | 40 MB | id, ru, en |
| it | Italiano | 188 MB | 45 MB | it, ru, en |
| ja | 日本語 | 184 MB | 41 MB | ja, ru, en |
| ko | 한국어 | 184 MB | 41 MB | ko, ru, en |
| nl | Nederlands | 186 MB | 43 MB | nl, ru, en |
| pl | Polski | 187 MB | 44 MB | pl, ru, en |
| pt | Português | 183 MB | 41 MB | pt, ru, en |
| ru | Русский | 178 MB | 35 MB | ru, en |
| th | ไทย | 182 MB | 39 MB | th, ru, en |
| tr | Türkçe | 183 MB | 40 MB | tr, ru, en |
| uk | Українська | 192 MB | 49 MB | uk, ru, en |
| vi | Tiếng Việt | 182 MB | 39 MB | vi, ru, en |
| zh | 中文 | 187 MB | 44 MB | zh, ru, en |

### NYC Metro (`nyc-metro`)

Base `osm-nyc-metro-2026-10-24b.zim` 0.8 GB · packs 0.7 GB · 22 versions, 18.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 849 MB | 60 MB | ar, es, en |
| de | Deutsch | 855 MB | 65 MB | de, es, en |
| el | Ελληνικά | 838 MB | 48 MB | el, es, en |
| en | English | 836 MB | 46 MB | es, en |
| es | Español | 836 MB | 47 MB | es, en |
| fa | فارسی | 848 MB | 58 MB | fa, es, en |
| fr | Français | 865 MB | 75 MB | fr, es, en |
| he | עברית | 842 MB | 53 MB | he, es, en |
| hi | हिन्दी | 838 MB | 48 MB | hi, es, en |
| id | Bahasa Indonesia | 842 MB | 52 MB | id, es, en |
| it | Italiano | 858 MB | 69 MB | it, es, en |
| ja | 日本語 | 854 MB | 64 MB | ja, es, en |
| ko | 한국어 | 843 MB | 53 MB | ko, es, en |
| nl | Nederlands | 849 MB | 59 MB | nl, es, en |
| pl | Polski | 848 MB | 58 MB | pl, es, en |
| pt | Português | 844 MB | 55 MB | pt, es, en |
| ru | Русский | 852 MB | 62 MB | ru, es, en |
| th | ไทย | 838 MB | 49 MB | th, es, en |
| tr | Türkçe | 842 MB | 52 MB | tr, es, en |
| uk | Українська | 852 MB | 62 MB | uk, es, en |
| vi | Tiếng Việt | 839 MB | 49 MB | vi, es, en |
| zh | 中文 | 861 MB | 71 MB | zh, es, en |

### New York State (`new-york-state`)

Base `osm-new-york-state-2026-10-24b.zim` 1.7 GB · packs 1.6 GB · 22 versions, 38.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,767 MB | 119 MB | ar, es, en |
| de | Deutsch | 1,769 MB | 122 MB | de, es, en |
| el | Ελληνικά | 1,744 MB | 96 MB | el, es, en |
| en | English | 1,741 MB | 86 MB | es, en |
| es | Español | 1,742 MB | 86 MB | es, en |
| fa | فارسی | 1,758 MB | 111 MB | fa, es, en |
| fr | Français | 1,787 MB | 140 MB | fr, es, en |
| he | עברית | 1,749 MB | 101 MB | he, es, en |
| hi | हिन्दी | 1,743 MB | 95 MB | hi, es, en |
| id | Bahasa Indonesia | 1,748 MB | 100 MB | id, es, en |
| it | Italiano | 1,776 MB | 128 MB | it, es, en |
| ja | 日本語 | 1,762 MB | 115 MB | ja, es, en |
| ko | 한국어 | 1,750 MB | 102 MB | ko, es, en |
| nl | Nederlands | 1,756 MB | 108 MB | nl, es, en |
| pl | Polski | 1,762 MB | 115 MB | pl, es, en |
| pt | Português | 1,751 MB | 103 MB | pt, es, en |
| ru | Русский | 1,761 MB | 113 MB | ru, es, en |
| th | ไทย | 1,744 MB | 96 MB | th, es, en |
| tr | Türkçe | 1,748 MB | 100 MB | tr, es, en |
| uk | Українська | 1,770 MB | 123 MB | uk, es, en |
| vi | Tiếng Việt | 1,745 MB | 97 MB | vi, es, en |
| zh | 中文 | 1,781 MB | 133 MB | zh, es, en |

### Nordics (`nordics`)

Base `osm-nordics-2026-10-26b.zim` 6.7 GB · packs 14.0 GB · 26 versions, 197.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 7,574 MB | 896 MB | ar, sv, da, fi, en |
| de | Deutsch | 7,621 MB | 939 MB | de, sv, da, fi, en |
| el | Ελληνικά | 7,557 MB | 872 MB | el, sv, da, fi, en |
| en | English | 7,562 MB | 886 MB | sv, da, fi, en |
| es | Español | 7,595 MB | 919 MB | es, sv, da, fi, en |
| fa | فارسی | 7,573 MB | 905 MB | fa, sv, da, fi, en |
| fr | Français | 7,655 MB | 975 MB | fr, sv, da, fi, en |
| he | עברית | 7,557 MB | 872 MB | he, sv, da, fi, en |
| hi | हिन्दी | 7,553 MB | 870 MB | hi, sv, da, fi, en |
| id | Bahasa Indonesia | 7,573 MB | 894 MB | id, sv, da, fi, en |
| it | Italiano | 7,598 MB | 919 MB | it, sv, da, fi, en |
| ja | 日本語 | 7,563 MB | 894 MB | ja, sv, da, fi, en |
| ko | 한국어 | 7,562 MB | 894 MB | ko, sv, da, fi, en |
| nl | Nederlands | 7,608 MB | 928 MB | nl, sv, da, fi, en |
| pl | Polski | 7,600 MB | 929 MB | pl, sv, da, fi, en |
| pt | Português | 7,585 MB | 908 MB | pt, sv, da, fi, en |
| ru | Русский | 7,599 MB | 916 MB | ru, sv, da, fi, en |
| th | ไทย | 7,555 MB | 871 MB | th, sv, da, fi, en |
| tr | Türkçe | 7,577 MB | 899 MB | tr, sv, da, fi, en |
| uk | Українська | 7,578 MB | 908 MB | uk, sv, da, fi, en |
| vi | Tiếng Việt | 7,563 MB | 878 MB | vi, sv, da, fi, en |
| zh | 中文 | 7,578 MB | 906 MB | zh, sv, da, fi, en |
| sv | Svenska | 7,558 MB | 886 MB | sv, da, fi, en |
| da | Dansk | 7,558 MB | 886 MB | sv, da, fi, en |
| fi | Suomi | 7,555 MB | 886 MB | sv, da, fi, en |
| no | Norsk | 7,699 MB | 1,031 MB | no, sv, da, fi, en |

### North Africa (Maghreb) (`north-africa`)

Base `osm-north-africa-2026-10-27b.zim` 3.3 GB · packs 3.5 GB · 23 versions, 83.0 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 3,589 MB | 254 MB | ar, fr, zgh, en |
| de | Deutsch | 3,634 MB | 293 MB | de, ar, fr, zgh, en |
| el | Ελληνικά | 3,611 MB | 271 MB | el, ar, fr, zgh, en |
| en | English | 3,590 MB | 254 MB | ar, fr, zgh, en |
| es | Español | 3,668 MB | 323 MB | es, ar, fr, zgh, en |
| fa | فارسی | 3,610 MB | 273 MB | fa, ar, fr, zgh, en |
| fr | Français | 3,590 MB | 254 MB | ar, fr, zgh, en |
| he | עברית | 3,594 MB | 258 MB | he, ar, fr, zgh, en |
| hi | हिन्दी | 3,590 MB | 255 MB | hi, ar, fr, zgh, en |
| id | Bahasa Indonesia | 3,602 MB | 263 MB | id, ar, fr, zgh, en |
| it | Italiano | 3,651 MB | 309 MB | it, ar, fr, zgh, en |
| ja | 日本語 | 3,598 MB | 261 MB | ja, ar, fr, zgh, en |
| ko | 한국어 | 3,595 MB | 260 MB | ko, ar, fr, zgh, en |
| nl | Nederlands | 3,602 MB | 263 MB | nl, ar, fr, zgh, en |
| pl | Polski | 3,611 MB | 274 MB | pl, ar, fr, zgh, en |
| pt | Português | 3,634 MB | 295 MB | pt, ar, fr, zgh, en |
| ru | Русский | 3,616 MB | 276 MB | ru, ar, fr, zgh, en |
| th | ไทย | 3,590 MB | 256 MB | th, ar, fr, zgh, en |
| tr | Türkçe | 3,603 MB | 265 MB | tr, ar, fr, zgh, en |
| uk | Українська | 3,608 MB | 271 MB | uk, ar, fr, zgh, en |
| vi | Tiếng Việt | 3,609 MB | 274 MB | vi, ar, fr, zgh, en |
| zh | 中文 | 3,610 MB | 272 MB | zh, ar, fr, zgh, en |
| zgh | ⵜⴰⵎⴰⵣⵉⵖⵜ | 3,588 MB | 254 MB | ar, fr, zgh, en |

### Northeast India (`india-northeast`)

Base `osm-india-northeast-2026-11-05b.zim` 0.5 GB · packs 0.5 GB · 25 versions, 14.3 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 572 MB | 30 MB | ar, as, bn, hi, ne, en |
| de | Deutsch | 573 MB | 31 MB | de, as, bn, hi, ne, en |
| el | Ελληνικά | 570 MB | 26 MB | el, as, bn, hi, ne, en |
| en | English | 570 MB | 25 MB | as, bn, hi, ne, en |
| es | Español | 573 MB | 31 MB | es, as, bn, hi, ne, en |
| fa | فارسی | 572 MB | 30 MB | fa, as, bn, hi, ne, en |
| fr | Français | 573 MB | 31 MB | fr, as, bn, hi, ne, en |
| he | עברית | 570 MB | 26 MB | he, as, bn, hi, ne, en |
| hi | हिन्दी | 570 MB | 25 MB | as, bn, hi, ne, en |
| id | Bahasa Indonesia | 571 MB | 29 MB | id, as, bn, hi, ne, en |
| it | Italiano | 573 MB | 31 MB | it, as, bn, hi, ne, en |
| ja | 日本語 | 571 MB | 29 MB | ja, as, bn, hi, ne, en |
| ko | 한국어 | 571 MB | 26 MB | ko, as, bn, hi, ne, en |
| nl | Nederlands | 571 MB | 29 MB | nl, as, bn, hi, ne, en |
| pl | Polski | 571 MB | 26 MB | pl, as, bn, hi, ne, en |
| pt | Português | 571 MB | 29 MB | pt, as, bn, hi, ne, en |
| ru | Русский | 572 MB | 30 MB | ru, as, bn, hi, ne, en |
| th | ไทย | 571 MB | 26 MB | th, as, bn, hi, ne, en |
| tr | Türkçe | 571 MB | 29 MB | tr, as, bn, hi, ne, en |
| uk | Українська | 572 MB | 30 MB | uk, as, bn, hi, ne, en |
| vi | Tiếng Việt | 571 MB | 27 MB | vi, as, bn, hi, ne, en |
| zh | 中文 | 572 MB | 30 MB | zh, as, bn, hi, ne, en |
| as | অসমীয়া | 570 MB | 25 MB | as, bn, hi, ne, en |
| bn | বাংলা | 570 MB | 25 MB | as, bn, hi, ne, en |
| ne | नेपाली | 571 MB | 29 MB | as, bn, hi, ne, en |

### Northeast India (Light) (`india-northeast-light`)

Base `osm-india-northeast-light-2026-11-07.zim` 0.3 GB · packs 0.5 GB · 25 versions, 8.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 348 MB | 22 MB | ar, as, bn, hi, ne, en |
| de | Deutsch | 348 MB | 22 MB | de, as, bn, hi, ne, en |
| el | Ελληνικά | 347 MB | 19 MB | el, as, bn, hi, ne, en |
| en | English | 348 MB | 18 MB | as, bn, hi, ne, en |
| es | Español | 348 MB | 22 MB | es, as, bn, hi, ne, en |
| fa | فارسی | 348 MB | 19 MB | fa, as, bn, hi, ne, en |
| fr | Français | 348 MB | 22 MB | fr, as, bn, hi, ne, en |
| he | עברית | 347 MB | 19 MB | he, as, bn, hi, ne, en |
| hi | हिन्दी | 347 MB | 18 MB | as, bn, hi, ne, en |
| id | Bahasa Indonesia | 348 MB | 21 MB | id, as, bn, hi, ne, en |
| it | Italiano | 348 MB | 22 MB | it, as, bn, hi, ne, en |
| ja | 日本語 | 348 MB | 19 MB | ja, as, bn, hi, ne, en |
| ko | 한국어 | 347 MB | 19 MB | ko, as, bn, hi, ne, en |
| nl | Nederlands | 348 MB | 22 MB | nl, as, bn, hi, ne, en |
| pl | Polski | 347 MB | 19 MB | pl, as, bn, hi, ne, en |
| pt | Português | 348 MB | 19 MB | pt, as, bn, hi, ne, en |
| ru | Русский | 348 MB | 22 MB | ru, as, bn, hi, ne, en |
| th | ไทย | 347 MB | 19 MB | th, as, bn, hi, ne, en |
| tr | Türkçe | 348 MB | 18 MB | tr, as, bn, hi, ne, en |
| uk | Українська | 348 MB | 19 MB | uk, as, bn, hi, ne, en |
| vi | Tiếng Việt | 348 MB | 19 MB | vi, as, bn, hi, ne, en |
| zh | 中文 | 348 MB | 22 MB | zh, as, bn, hi, ne, en |
| as | অসমীয়া | 347 MB | 18 MB | as, bn, hi, ne, en |
| bn | বাংলা | 347 MB | 18 MB | as, bn, hi, ne, en |
| ne | नेपाली | 348 MB | 21 MB | as, bn, hi, ne, en |

### Northwest India (`india-northwest`)

Base `osm-india-northwest-2026-11-05b.zim` 1.6 GB · packs 1.5 GB · 24 versions, 41.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,719 MB | 73 MB | ar, hi, pa, ur, ks, en |
| de | Deutsch | 1,720 MB | 74 MB | de, hi, pa, ur, ks, en |
| el | Ελληνικά | 1,715 MB | 71 MB | el, hi, pa, ur, ks, en |
| en | English | 1,716 MB | 71 MB | hi, pa, ur, ks, en |
| es | Español | 1,720 MB | 74 MB | es, hi, pa, ur, ks, en |
| fa | فارسی | 1,717 MB | 73 MB | fa, hi, pa, ur, ks, en |
| fr | Français | 1,722 MB | 76 MB | fr, hi, pa, ur, ks, en |
| he | עברית | 1,715 MB | 72 MB | he, hi, pa, ur, ks, en |
| hi | हिन्दी | 1,714 MB | 71 MB | hi, pa, ur, ks, en |
| id | Bahasa Indonesia | 1,717 MB | 72 MB | id, hi, pa, ur, ks, en |
| it | Italiano | 1,721 MB | 75 MB | it, hi, pa, ur, ks, en |
| ja | 日本語 | 1,717 MB | 73 MB | ja, hi, pa, ur, ks, en |
| ko | 한국어 | 1,715 MB | 72 MB | ko, hi, pa, ur, ks, en |
| nl | Nederlands | 1,717 MB | 72 MB | nl, hi, pa, ur, ks, en |
| pl | Polski | 1,716 MB | 73 MB | pl, hi, pa, ur, ks, en |
| pt | Português | 1,718 MB | 73 MB | pt, hi, pa, ur, ks, en |
| ru | Русский | 1,720 MB | 74 MB | ru, hi, pa, ur, ks, en |
| th | ไทย | 1,716 MB | 72 MB | th, hi, pa, ur, ks, en |
| tr | Türkçe | 1,717 MB | 72 MB | tr, hi, pa, ur, ks, en |
| uk | Українська | 1,718 MB | 74 MB | uk, hi, pa, ur, ks, en |
| vi | Tiếng Việt | 1,717 MB | 74 MB | vi, hi, pa, ur, ks, en |
| zh | 中文 | 1,718 MB | 73 MB | zh, hi, pa, ur, ks, en |
| pa | ਪੰਜਾਬੀ | 1,714 MB | 71 MB | hi, pa, ur, ks, en |
| ur | اردو | 1,714 MB | 71 MB | hi, pa, ur, ks, en |

### Northwest India (Light) (`india-northwest-light`)

Base `osm-india-northwest-light-2026-11-07.zim` 0.8 GB · packs 1.4 GB · 24 versions, 19.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 819 MB | 54 MB | ar, hi, pa, ur, ks, en |
| de | Deutsch | 819 MB | 54 MB | de, hi, pa, ur, ks, en |
| el | Ελληνικά | 817 MB | 53 MB | el, hi, pa, ur, ks, en |
| en | English | 818 MB | 53 MB | hi, pa, ur, ks, en |
| es | Español | 819 MB | 54 MB | es, hi, pa, ur, ks, en |
| fa | فارسی | 817 MB | 54 MB | fa, hi, pa, ur, ks, en |
| fr | Français | 818 MB | 54 MB | fr, hi, pa, ur, ks, en |
| he | עברית | 817 MB | 53 MB | he, hi, pa, ur, ks, en |
| hi | हिन्दी | 816 MB | 53 MB | hi, pa, ur, ks, en |
| id | Bahasa Indonesia | 818 MB | 53 MB | id, hi, pa, ur, ks, en |
| it | Italiano | 819 MB | 54 MB | it, hi, pa, ur, ks, en |
| ja | 日本語 | 818 MB | 54 MB | ja, hi, pa, ur, ks, en |
| ko | 한국어 | 817 MB | 53 MB | ko, hi, pa, ur, ks, en |
| nl | Nederlands | 819 MB | 54 MB | nl, hi, pa, ur, ks, en |
| pl | Polski | 817 MB | 54 MB | pl, hi, pa, ur, ks, en |
| pt | Português | 818 MB | 54 MB | pt, hi, pa, ur, ks, en |
| ru | Русский | 819 MB | 54 MB | ru, hi, pa, ur, ks, en |
| th | ไทย | 817 MB | 53 MB | th, hi, pa, ur, ks, en |
| tr | Türkçe | 818 MB | 53 MB | tr, hi, pa, ur, ks, en |
| uk | Українська | 817 MB | 54 MB | uk, hi, pa, ur, ks, en |
| vi | Tiếng Việt | 817 MB | 54 MB | vi, hi, pa, ur, ks, en |
| zh | 中文 | 818 MB | 54 MB | zh, hi, pa, ur, ks, en |
| pa | ਪੰਜਾਬੀ | 816 MB | 53 MB | hi, pa, ur, ks, en |
| ur | اردو | 816 MB | 53 MB | hi, pa, ur, ks, en |

### Osaka (`osaka`)

Base `osm-osaka-2026-11-09.zim` 0.1 GB · packs 0.3 GB · 22 versions, 2.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 111 MB | 33 MB | ar, ja, en |
| de | Deutsch | 111 MB | 33 MB | de, ja, en |
| el | Ελληνικά | 110 MB | 32 MB | el, ja, en |
| en | English | 111 MB | 32 MB | ja, en |
| es | Español | 111 MB | 33 MB | es, ja, en |
| fa | فارسی | 111 MB | 33 MB | fa, ja, en |
| fr | Français | 115 MB | 37 MB | fr, ja, en |
| he | עברית | 110 MB | 32 MB | he, ja, en |
| hi | हिन्दी | 110 MB | 32 MB | hi, ja, en |
| id | Bahasa Indonesia | 111 MB | 33 MB | id, ja, en |
| it | Italiano | 114 MB | 36 MB | it, ja, en |
| ja | 日本語 | 110 MB | 32 MB | ja, en |
| ko | 한국어 | 115 MB | 37 MB | ko, ja, en |
| nl | Nederlands | 113 MB | 35 MB | nl, ja, en |
| pl | Polski | 111 MB | 33 MB | pl, ja, en |
| pt | Português | 111 MB | 33 MB | pt, ja, en |
| ru | Русский | 111 MB | 33 MB | ru, ja, en |
| th | ไทย | 111 MB | 33 MB | th, ja, en |
| tr | Türkçe | 111 MB | 33 MB | tr, ja, en |
| uk | Українська | 111 MB | 33 MB | uk, ja, en |
| vi | Tiếng Việt | 111 MB | 33 MB | vi, ja, en |
| zh | 中文 | 115 MB | 37 MB | zh, ja, en |

### Pacific Islands (`pacific-islands`)

Base `osm-pacific-islands-2026-10-26b.zim` 0.8 GB · packs 0.6 GB · 26 versions, 21.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 826 MB | 23 MB | ar, tpi, fj, hif, en |
| de | Deutsch | 830 MB | 28 MB | de, tpi, fj, hif, en |
| el | Ελληνικά | 825 MB | 22 MB | el, tpi, fj, hif, en |
| en | English | 824 MB | 21 MB | tpi, fj, hif, en |
| es | Español | 828 MB | 26 MB | es, tpi, fj, hif, en |
| fa | فارسی | 826 MB | 24 MB | fa, tpi, fj, hif, en |
| fr | Français | 831 MB | 28 MB | fr, tpi, fj, hif, en |
| he | עברית | 825 MB | 22 MB | he, tpi, fj, hif, en |
| hi | हिन्दी | 824 MB | 22 MB | hi, tpi, fj, hif, en |
| id | Bahasa Indonesia | 827 MB | 24 MB | id, tpi, fj, hif, en |
| it | Italiano | 829 MB | 26 MB | it, tpi, fj, hif, en |
| ja | 日本語 | 827 MB | 25 MB | ja, tpi, fj, hif, en |
| ko | 한국어 | 826 MB | 23 MB | ko, tpi, fj, hif, en |
| nl | Nederlands | 826 MB | 23 MB | nl, tpi, fj, hif, en |
| pl | Polski | 827 MB | 25 MB | pl, tpi, fj, hif, en |
| pt | Português | 826 MB | 24 MB | pt, tpi, fj, hif, en |
| ru | Русский | 829 MB | 26 MB | ru, tpi, fj, hif, en |
| th | ไทย | 824 MB | 22 MB | th, tpi, fj, hif, en |
| tr | Türkçe | 825 MB | 23 MB | tr, tpi, fj, hif, en |
| uk | Українська | 827 MB | 24 MB | uk, tpi, fj, hif, en |
| vi | Tiếng Việt | 825 MB | 22 MB | vi, tpi, fj, hif, en |
| zh | 中文 | 828 MB | 25 MB | zh, tpi, fj, hif, en |
| tpi | Tok Pisin | 824 MB | 21 MB | tpi, fj, hif, en |
| fj | Na Vosa Vakaviti | 824 MB | 21 MB | tpi, fj, hif, en |
| hif | Fiji Baat | 824 MB | 21 MB | tpi, fj, hif, en |
| bi | Bislama | 824 MB | 21 MB | bi, tpi, fj, hif, en |

### Poland (`poland`)

Base `osm-poland-2026-10-23b.zim` 3.2 GB · packs 7.4 GB · 22 versions, 88.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 3,976 MB | 744 MB | ar, pl, en |
| de | Deutsch | 4,082 MB | 847 MB | de, pl, en |
| el | Ελληνικά | 3,978 MB | 748 MB | el, pl, en |
| en | English | 3,969 MB | 738 MB | pl, en |
| es | Español | 4,002 MB | 770 MB | es, pl, en |
| fa | فارسی | 4,005 MB | 769 MB | fa, pl, en |
| fr | Français | 4,232 MB | 1,000 MB | fr, pl, en |
| he | עברית | 3,975 MB | 744 MB | he, pl, en |
| hi | हिन्दी | 3,967 MB | 739 MB | hi, pl, en |
| id | Bahasa Indonesia | 3,979 MB | 746 MB | id, pl, en |
| it | Italiano | 3,993 MB | 763 MB | it, pl, en |
| ja | 日本語 | 3,973 MB | 743 MB | ja, pl, en |
| ko | 한국어 | 3,976 MB | 746 MB | ko, pl, en |
| nl | Nederlands | 4,025 MB | 787 MB | nl, pl, en |
| pl | Polski | 3,967 MB | 738 MB | pl, en |
| pt | Português | 3,999 MB | 768 MB | pt, pl, en |
| ru | Русский | 4,043 MB | 803 MB | ru, pl, en |
| th | ไทย | 3,969 MB | 740 MB | th, pl, en |
| tr | Türkçe | 3,977 MB | 746 MB | tr, pl, en |
| uk | Українська | 4,312 MB | 1,084 MB | uk, pl, en |
| vi | Tiếng Việt | 4,006 MB | 775 MB | vi, pl, en |
| zh | 中文 | 3,988 MB | 755 MB | zh, pl, en |

### Romania, Hungary & Bulgaria (`carpathians`)

Base `osm-carpathians-2026-10-26b.zim` 2.2 GB · packs 4.8 GB · 25 versions, 66.3 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,638 MB | 442 MB | ar, ro, hu, bg, en |
| de | Deutsch | 2,666 MB | 482 MB | de, ro, hu, bg, en |
| el | Ελληνικά | 2,632 MB | 438 MB | el, ro, hu, bg, en |
| en | English | 2,626 MB | 432 MB | ro, hu, bg, en |
| es | Español | 2,680 MB | 484 MB | es, ro, hu, bg, en |
| fa | فارسی | 2,649 MB | 453 MB | fa, ro, hu, bg, en |
| fr | Français | 2,717 MB | 520 MB | fr, ro, hu, bg, en |
| he | עברית | 2,640 MB | 444 MB | he, ro, hu, bg, en |
| hi | हिन्दी | 2,625 MB | 433 MB | hi, ro, hu, bg, en |
| id | Bahasa Indonesia | 2,634 MB | 439 MB | id, ro, hu, bg, en |
| it | Italiano | 2,670 MB | 474 MB | it, ro, hu, bg, en |
| ja | 日本語 | 2,634 MB | 439 MB | ja, ro, hu, bg, en |
| ko | 한국어 | 2,632 MB | 438 MB | ko, ro, hu, bg, en |
| nl | Nederlands | 2,651 MB | 470 MB | nl, ro, hu, bg, en |
| pl | Polski | 2,726 MB | 541 MB | pl, ro, hu, bg, en |
| pt | Português | 2,683 MB | 489 MB | pt, ro, hu, bg, en |
| ru | Русский | 2,676 MB | 494 MB | ru, ro, hu, bg, en |
| th | ไทย | 2,627 MB | 434 MB | th, ro, hu, bg, en |
| tr | Türkçe | 2,637 MB | 442 MB | tr, ro, hu, bg, en |
| uk | Українська | 2,696 MB | 506 MB | uk, ro, hu, bg, en |
| vi | Tiếng Việt | 2,672 MB | 478 MB | vi, ro, hu, bg, en |
| zh | 中文 | 2,650 MB | 468 MB | zh, ro, hu, bg, en |
| ro | Română | 2,623 MB | 432 MB | ro, hu, bg, en |
| hu | Magyar | 2,619 MB | 432 MB | ro, hu, bg, en |
| bg | Български | 2,624 MB | 432 MB | ro, hu, bg, en |

### Seoul (`seoul`)

Base `osm-seoul-2026-11-09.zim` 0.1 GB · packs 0.2 GB · 22 versions, 3.0 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 135 MB | 19 MB | ar, ko, en |
| de | Deutsch | 136 MB | 20 MB | de, ko, en |
| el | Ελληνικά | 134 MB | 18 MB | el, ko, en |
| en | English | 134 MB | 18 MB | ko, en |
| es | Español | 136 MB | 20 MB | es, ko, en |
| fa | فارسی | 135 MB | 19 MB | fa, ko, en |
| fr | Français | 141 MB | 25 MB | fr, ko, en |
| he | עברית | 135 MB | 19 MB | he, ko, en |
| hi | हिन्दी | 134 MB | 18 MB | hi, ko, en |
| id | Bahasa Indonesia | 136 MB | 20 MB | id, ko, en |
| it | Italiano | 138 MB | 22 MB | it, ko, en |
| ja | 日本語 | 142 MB | 26 MB | ja, ko, en |
| ko | 한국어 | 134 MB | 18 MB | ko, en |
| nl | Nederlands | 135 MB | 19 MB | nl, ko, en |
| pl | Polski | 135 MB | 19 MB | pl, ko, en |
| pt | Português | 135 MB | 19 MB | pt, ko, en |
| ru | Русский | 136 MB | 20 MB | ru, ko, en |
| th | ไทย | 135 MB | 19 MB | th, ko, en |
| tr | Türkçe | 135 MB | 19 MB | tr, ko, en |
| uk | Українська | 135 MB | 19 MB | uk, ko, en |
| vi | Tiếng Việt | 139 MB | 24 MB | vi, ko, en |
| zh | 中文 | 141 MB | 25 MB | zh, ko, en |

### Silicon Valley (`silicon-valley`)

Base `osm-silicon-valley-2026-10-24b.zim` 0.3 GB · packs 0.2 GB · 22 versions, 7.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 326 MB | 14 MB | ar, es, en |
| de | Deutsch | 328 MB | 16 MB | de, es, en |
| el | Ελληνικά | 325 MB | 13 MB | el, es, en |
| en | English | 324 MB | 12 MB | es, en |
| es | Español | 324 MB | 12 MB | es, en |
| fa | فارسی | 326 MB | 14 MB | fa, es, en |
| fr | Français | 329 MB | 17 MB | fr, es, en |
| he | עברית | 325 MB | 13 MB | he, es, en |
| hi | हिन्दी | 325 MB | 13 MB | hi, es, en |
| id | Bahasa Indonesia | 326 MB | 14 MB | id, es, en |
| it | Italiano | 327 MB | 15 MB | it, es, en |
| ja | 日本語 | 327 MB | 15 MB | ja, es, en |
| ko | 한국어 | 326 MB | 14 MB | ko, es, en |
| nl | Nederlands | 326 MB | 14 MB | nl, es, en |
| pl | Polski | 326 MB | 14 MB | pl, es, en |
| pt | Português | 326 MB | 14 MB | pt, es, en |
| ru | Русский | 327 MB | 15 MB | ru, es, en |
| th | ไทย | 324 MB | 13 MB | th, es, en |
| tr | Türkçe | 325 MB | 13 MB | tr, es, en |
| uk | Українська | 326 MB | 14 MB | uk, es, en |
| vi | Tiếng Việt | 325 MB | 13 MB | vi, es, en |
| zh | 中文 | 329 MB | 18 MB | zh, es, en |

### South India (`india-south`)

Base `osm-india-south-2026-11-05b.zim` 1.9 GB · packs 3.6 GB · 26 versions, 55.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,145 MB | 222 MB | ar, te, ta, kn, ml, hi, ur, en |
| de | Deutsch | 2,148 MB | 224 MB | de, te, ta, kn, ml, hi, ur, en |
| el | Ελληνικά | 2,139 MB | 220 MB | el, te, ta, kn, ml, hi, ur, en |
| en | English | 2,142 MB | 219 MB | te, ta, kn, ml, hi, ur, en |
| es | Español | 2,151 MB | 227 MB | es, te, ta, kn, ml, hi, ur, en |
| fa | فارسی | 2,143 MB | 223 MB | fa, te, ta, kn, ml, hi, ur, en |
| fr | Français | 2,152 MB | 229 MB | fr, te, ta, kn, ml, hi, ur, en |
| he | עברית | 2,140 MB | 220 MB | he, te, ta, kn, ml, hi, ur, en |
| hi | हिन्दी | 2,138 MB | 219 MB | te, ta, kn, ml, hi, ur, en |
| id | Bahasa Indonesia | 2,145 MB | 221 MB | id, te, ta, kn, ml, hi, ur, en |
| it | Italiano | 2,151 MB | 227 MB | it, te, ta, kn, ml, hi, ur, en |
| ja | 日本語 | 2,141 MB | 221 MB | ja, te, ta, kn, ml, hi, ur, en |
| ko | 한국어 | 2,140 MB | 220 MB | ko, te, ta, kn, ml, hi, ur, en |
| nl | Nederlands | 2,143 MB | 220 MB | nl, te, ta, kn, ml, hi, ur, en |
| pl | Polski | 2,141 MB | 222 MB | pl, te, ta, kn, ml, hi, ur, en |
| pt | Português | 2,144 MB | 221 MB | pt, te, ta, kn, ml, hi, ur, en |
| ru | Русский | 2,147 MB | 223 MB | ru, te, ta, kn, ml, hi, ur, en |
| th | ไทย | 2,140 MB | 220 MB | th, te, ta, kn, ml, hi, ur, en |
| uk | Українська | 2,142 MB | 222 MB | uk, te, ta, kn, ml, hi, ur, en |
| vi | Tiếng Việt | 2,146 MB | 226 MB | vi, te, ta, kn, ml, hi, ur, en |
| zh | 中文 | 2,142 MB | 221 MB | zh, te, ta, kn, ml, hi, ur, en |
| te | తెలుగు | 2,138 MB | 219 MB | te, ta, kn, ml, hi, ur, en |
| ta | தமிழ் | 2,142 MB | 219 MB | te, ta, kn, ml, hi, ur, en |
| kn | ಕನ್ನಡ | 2,138 MB | 219 MB | te, ta, kn, ml, hi, ur, en |
| ml | മലയാളം | 2,138 MB | 219 MB | te, ta, kn, ml, hi, ur, en |
| ur | اردو | 2,138 MB | 219 MB | te, ta, kn, ml, hi, ur, en |

### South India (Light) (`india-south-light`)

Base `osm-india-south-light-2026-11-07.zim` 1.0 GB · packs 3.4 GB · 26 versions, 29.0 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,118 MB | 138 MB | ar, te, ta, kn, ml, hi, ur, en |
| de | Deutsch | 1,119 MB | 138 MB | de, te, ta, kn, ml, hi, ur, en |
| el | Ελληνικά | 1,114 MB | 138 MB | el, te, ta, kn, ml, hi, ur, en |
| en | English | 1,117 MB | 137 MB | te, ta, kn, ml, hi, ur, en |
| es | Español | 1,119 MB | 138 MB | es, te, ta, kn, ml, hi, ur, en |
| fa | فارسی | 1,115 MB | 138 MB | fa, te, ta, kn, ml, hi, ur, en |
| fr | Français | 1,119 MB | 138 MB | fr, te, ta, kn, ml, hi, ur, en |
| he | עברית | 1,115 MB | 138 MB | he, te, ta, kn, ml, hi, ur, en |
| hi | हिन्दी | 1,114 MB | 137 MB | te, ta, kn, ml, hi, ur, en |
| id | Bahasa Indonesia | 1,118 MB | 138 MB | id, te, ta, kn, ml, hi, ur, en |
| it | Italiano | 1,119 MB | 138 MB | it, te, ta, kn, ml, hi, ur, en |
| ja | 日本語 | 1,115 MB | 138 MB | ja, te, ta, kn, ml, hi, ur, en |
| ko | 한국어 | 1,115 MB | 138 MB | ko, te, ta, kn, ml, hi, ur, en |
| nl | Nederlands | 1,119 MB | 138 MB | nl, te, ta, kn, ml, hi, ur, en |
| pl | Polski | 1,115 MB | 138 MB | pl, te, ta, kn, ml, hi, ur, en |
| pt | Português | 1,118 MB | 138 MB | pt, te, ta, kn, ml, hi, ur, en |
| ru | Русский | 1,119 MB | 138 MB | ru, te, ta, kn, ml, hi, ur, en |
| th | ไทย | 1,114 MB | 138 MB | th, te, ta, kn, ml, hi, ur, en |
| uk | Українська | 1,115 MB | 138 MB | uk, te, ta, kn, ml, hi, ur, en |
| vi | Tiếng Việt | 1,115 MB | 138 MB | vi, te, ta, kn, ml, hi, ur, en |
| zh | 中文 | 1,116 MB | 138 MB | zh, te, ta, kn, ml, hi, ur, en |
| te | తెలుగు | 1,114 MB | 137 MB | te, ta, kn, ml, hi, ur, en |
| ta | தமிழ் | 1,118 MB | 137 MB | te, ta, kn, ml, hi, ur, en |
| kn | ಕನ್ನಡ | 1,114 MB | 137 MB | te, ta, kn, ml, hi, ur, en |
| ml | മലയാളം | 1,114 MB | 137 MB | te, ta, kn, ml, hi, ur, en |
| ur | اردو | 1,114 MB | 137 MB | te, ta, kn, ml, hi, ur, en |

### South Korea (`south-korea`)

Base `osm-south-korea-2026-10-24b.zim` 0.8 GB · packs 1.4 GB · 22 versions, 19.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 895 MB | 102 MB | ar, ko, en |
| de | Deutsch | 897 MB | 104 MB | de, ko, en |
| el | Ελληνικά | 891 MB | 93 MB | el, ko, en |
| en | English | 892 MB | 92 MB | ko, en |
| es | Español | 898 MB | 105 MB | es, ko, en |
| fa | فارسی | 894 MB | 95 MB | fa, ko, en |
| fr | Français | 908 MB | 115 MB | fr, ko, en |
| he | עברית | 891 MB | 93 MB | he, ko, en |
| hi | हिन्दी | 890 MB | 92 MB | hi, ko, en |
| id | Bahasa Indonesia | 896 MB | 103 MB | id, ko, en |
| it | Italiano | 899 MB | 106 MB | it, ko, en |
| ja | 日本語 | 913 MB | 120 MB | ja, ko, en |
| ko | 한국어 | 890 MB | 92 MB | ko, en |
| nl | Nederlands | 894 MB | 101 MB | nl, ko, en |
| pl | Polski | 893 MB | 95 MB | pl, ko, en |
| pt | Português | 894 MB | 101 MB | pt, ko, en |
| ru | Русский | 900 MB | 107 MB | ru, ko, en |
| th | ไทย | 892 MB | 94 MB | th, ko, en |
| tr | Türkçe | 895 MB | 102 MB | tr, ko, en |
| uk | Українська | 893 MB | 94 MB | uk, ko, en |
| vi | Tiếng Việt | 904 MB | 105 MB | vi, ko, en |
| zh | 中文 | 911 MB | 118 MB | zh, ko, en |

### Southern Africa (`southern-africa`)

Base `osm-southern-africa-2026-10-26b.zim` 2.5 GB · packs 3.4 GB · 27 versions, 70.1 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,597 MB | 122 MB | ar, zu, xh, af, en |
| de | Deutsch | 2,606 MB | 146 MB | de, zu, xh, af, en |
| el | Ελληνικά | 2,593 MB | 120 MB | el, zu, xh, af, en |
| en | English | 2,594 MB | 119 MB | zu, xh, af, en |
| es | Español | 2,601 MB | 126 MB | es, zu, xh, af, en |
| fa | فارسی | 2,595 MB | 122 MB | fa, zu, xh, af, en |
| fr | Français | 2,608 MB | 132 MB | fr, zu, xh, af, en |
| he | עברית | 2,594 MB | 121 MB | he, zu, xh, af, en |
| hi | हिन्दी | 2,593 MB | 120 MB | hi, zu, xh, af, en |
| id | Bahasa Indonesia | 2,597 MB | 122 MB | id, zu, xh, af, en |
| it | Italiano | 2,607 MB | 131 MB | it, zu, xh, af, en |
| ja | 日本語 | 2,596 MB | 122 MB | ja, zu, xh, af, en |
| ko | 한국어 | 2,594 MB | 121 MB | ko, zu, xh, af, en |
| nl | Nederlands | 2,598 MB | 122 MB | nl, zu, xh, af, en |
| pl | Polski | 2,600 MB | 126 MB | pl, zu, xh, af, en |
| pt | Português | 2,599 MB | 123 MB | pt, zu, xh, af, en |
| ru | Русский | 2,601 MB | 125 MB | ru, zu, xh, af, en |
| th | ไทย | 2,593 MB | 120 MB | th, zu, xh, af, en |
| tr | Türkçe | 2,596 MB | 121 MB | tr, zu, xh, af, en |
| uk | Українська | 2,597 MB | 123 MB | uk, zu, xh, af, en |
| vi | Tiếng Việt | 2,593 MB | 120 MB | vi, zu, xh, af, en |
| zh | 中文 | 2,599 MB | 124 MB | zh, zu, xh, af, en |
| zu | isiZulu | 2,592 MB | 119 MB | zu, xh, af, en |
| xh | isiXhosa | 2,592 MB | 119 MB | zu, xh, af, en |
| af | Afrikaans | 2,592 MB | 119 MB | zu, xh, af, en |
| ny | Chichewa | 2,592 MB | 119 MB | ny, zu, xh, af, en |
| sn | chiShona | 2,593 MB | 120 MB | sn, zu, xh, af, en |

### Switzerland & the Alps (Light) (`switzerland-light`)

Base `osm-switzerland-light-2026-11-07.zim` 1.4 GB · packs 4.6 GB · 23 versions, 37.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,642 MB | 232 MB | ar, de, fr, it, rm, en |
| de | Deutsch | 1,635 MB | 214 MB | de, fr, it, rm, en |
| el | Ελληνικά | 1,639 MB | 215 MB | el, de, fr, it, rm, en |
| en | English | 1,636 MB | 214 MB | de, fr, it, rm, en |
| es | Español | 1,644 MB | 234 MB | es, de, fr, it, rm, en |
| fa | فارسی | 1,639 MB | 215 MB | fa, de, fr, it, rm, en |
| fr | Français | 1,636 MB | 214 MB | de, fr, it, rm, en |
| he | עברית | 1,637 MB | 214 MB | he, de, fr, it, rm, en |
| hi | हिन्दी | 1,636 MB | 214 MB | hi, de, fr, it, rm, en |
| id | Bahasa Indonesia | 1,641 MB | 231 MB | id, de, fr, it, rm, en |
| it | Italiano | 1,637 MB | 214 MB | de, fr, it, rm, en |
| ja | 日本語 | 1,640 MB | 215 MB | ja, de, fr, it, rm, en |
| ko | 한국어 | 1,641 MB | 216 MB | ko, de, fr, it, rm, en |
| nl | Nederlands | 1,647 MB | 234 MB | nl, de, fr, it, rm, en |
| pl | Polski | 1,643 MB | 233 MB | pl, de, fr, it, rm, en |
| pt | Português | 1,641 MB | 216 MB | pt, de, fr, it, rm, en |
| ru | Русский | 1,647 MB | 233 MB | ru, de, fr, it, rm, en |
| th | ไทย | 1,636 MB | 214 MB | th, de, fr, it, rm, en |
| tr | Türkçe | 1,641 MB | 232 MB | tr, de, fr, it, rm, en |
| uk | Українська | 1,644 MB | 233 MB | uk, de, fr, it, rm, en |
| vi | Tiếng Việt | 1,639 MB | 215 MB | vi, de, fr, it, rm, en |
| zh | 中文 | 1,651 MB | 234 MB | zh, de, fr, it, rm, en |
| rm | Rumantsch | 1,636 MB | 214 MB | de, fr, it, rm, en |

### Switzerland and the Alps (`switzerland`)

Base `osm-switzerland-2026-10-24b.zim` 2.3 GB · packs 6.3 GB · 23 versions, 70.8 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 3,069 MB | 778 MB | ar, de, fr, it, rm, en |
| de | Deutsch | 3,041 MB | 740 MB | de, fr, it, rm, en |
| el | Ελληνικά | 3,077 MB | 789 MB | el, de, fr, it, rm, en |
| en | English | 3,042 MB | 740 MB | de, fr, it, rm, en |
| es | Español | 3,114 MB | 824 MB | es, de, fr, it, rm, en |
| fa | فارسی | 3,068 MB | 780 MB | fa, de, fr, it, rm, en |
| fr | Français | 3,041 MB | 740 MB | de, fr, it, rm, en |
| he | עברית | 3,048 MB | 744 MB | he, de, fr, it, rm, en |
| hi | हिन्दी | 3,043 MB | 740 MB | hi, de, fr, it, rm, en |
| id | Bahasa Indonesia | 3,063 MB | 772 MB | id, de, fr, it, rm, en |
| it | Italiano | 3,043 MB | 740 MB | de, fr, it, rm, en |
| ja | 日本語 | 3,063 MB | 774 MB | ja, de, fr, it, rm, en |
| ko | 한국어 | 3,064 MB | 775 MB | ko, de, fr, it, rm, en |
| nl | Nederlands | 3,128 MB | 835 MB | nl, de, fr, it, rm, en |
| pl | Polski | 3,120 MB | 829 MB | pl, de, fr, it, rm, en |
| pt | Português | 3,114 MB | 825 MB | pt, de, fr, it, rm, en |
| ru | Русский | 3,110 MB | 814 MB | ru, de, fr, it, rm, en |
| th | ไทย | 3,045 MB | 743 MB | th, de, fr, it, rm, en |
| tr | Türkçe | 3,082 MB | 792 MB | tr, de, fr, it, rm, en |
| uk | Українська | 3,124 MB | 832 MB | uk, de, fr, it, rm, en |
| vi | Tiếng Việt | 3,089 MB | 801 MB | vi, de, fr, it, rm, en |
| zh | 中文 | 3,138 MB | 839 MB | zh, de, fr, it, rm, en |
| rm | Rumantsch | 3,042 MB | 740 MB | de, fr, it, rm, en |

### Taipei (`taipei`)

Base `osm-taipei-2026-11-09.zim` 0.1 GB · packs 0.3 GB · 22 versions, 3.3 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 149 MB | 24 MB | ar, zh, en |
| de | Deutsch | 150 MB | 24 MB | de, zh, en |
| el | Ελληνικά | 148 MB | 23 MB | el, zh, en |
| en | English | 148 MB | 23 MB | zh, en |
| es | Español | 149 MB | 24 MB | es, zh, en |
| fa | فارسی | 149 MB | 23 MB | fa, zh, en |
| fr | Français | 150 MB | 25 MB | fr, zh, en |
| he | עברית | 148 MB | 23 MB | he, zh, en |
| hi | हिन्दी | 148 MB | 23 MB | hi, zh, en |
| id | Bahasa Indonesia | 149 MB | 24 MB | id, zh, en |
| it | Italiano | 149 MB | 24 MB | it, zh, en |
| ja | 日本語 | 153 MB | 28 MB | ja, zh, en |
| ko | 한국어 | 150 MB | 25 MB | ko, zh, en |
| nl | Nederlands | 149 MB | 24 MB | nl, zh, en |
| pl | Polski | 149 MB | 24 MB | pl, zh, en |
| pt | Português | 149 MB | 24 MB | pt, zh, en |
| ru | Русский | 150 MB | 25 MB | ru, zh, en |
| th | ไทย | 148 MB | 23 MB | th, zh, en |
| tr | Türkçe | 149 MB | 24 MB | tr, zh, en |
| uk | Українська | 149 MB | 24 MB | uk, zh, en |
| vi | Tiếng Việt | 150 MB | 24 MB | vi, zh, en |
| zh | 中文 | 148 MB | 23 MB | zh, en |

### Taiwan (`taiwan`)

Base `osm-taiwan-2026-11-10.zim` 0.6 GB · packs 1.3 GB · 22 versions, 16.3 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 743 MB | 103 MB | ar, zh, en |
| de | Deutsch | 748 MB | 108 MB | de, zh, en |
| el | Ελληνικά | 740 MB | 96 MB | el, zh, en |
| en | English | 741 MB | 101 MB | zh, en |
| es | Español | 744 MB | 104 MB | es, zh, en |
| fa | فارسی | 741 MB | 96 MB | fa, zh, en |
| fr | Français | 746 MB | 106 MB | fr, zh, en |
| he | עברית | 740 MB | 96 MB | he, zh, en |
| hi | हिन्दी | 740 MB | 96 MB | hi, zh, en |
| id | Bahasa Indonesia | 744 MB | 104 MB | id, zh, en |
| it | Italiano | 745 MB | 105 MB | it, zh, en |
| ja | 日本語 | 755 MB | 116 MB | ja, zh, en |
| ko | 한국어 | 746 MB | 101 MB | ko, zh, en |
| nl | Nederlands | 742 MB | 102 MB | nl, zh, en |
| pl | Polski | 742 MB | 98 MB | pl, zh, en |
| pt | Português | 742 MB | 102 MB | pt, zh, en |
| ru | Русский | 744 MB | 104 MB | ru, zh, en |
| th | ไทย | 740 MB | 96 MB | th, zh, en |
| tr | Türkçe | 742 MB | 102 MB | tr, zh, en |
| uk | Українська | 741 MB | 97 MB | uk, zh, en |
| vi | Tiếng Việt | 744 MB | 100 MB | vi, zh, en |
| zh | 中文 | 739 MB | 95 MB | zh, en |

### Tehran (`tehran`)

Base `osm-tehran-2026-11-09.zim` 0.1 GB · packs 0.1 GB · 22 versions, 1.7 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 80 MB | 8 MB | ar, fa, en |
| de | Deutsch | 79 MB | 7 MB | de, fa, en |
| el | Ελληνικά | 78 MB | 6 MB | el, fa, en |
| en | English | 78 MB | 6 MB | fa, en |
| es | Español | 79 MB | 7 MB | es, fa, en |
| fa | فارسی | 78 MB | 6 MB | fa, en |
| fr | Français | 79 MB | 7 MB | fr, fa, en |
| he | עברית | 79 MB | 7 MB | he, fa, en |
| hi | हिन्दी | 78 MB | 6 MB | hi, fa, en |
| id | Bahasa Indonesia | 79 MB | 7 MB | id, fa, en |
| it | Italiano | 79 MB | 7 MB | it, fa, en |
| ja | 日本語 | 79 MB | 7 MB | ja, fa, en |
| ko | 한국어 | 78 MB | 6 MB | ko, fa, en |
| nl | Nederlands | 78 MB | 6 MB | nl, fa, en |
| pl | Polski | 78 MB | 6 MB | pl, fa, en |
| pt | Português | 78 MB | 7 MB | pt, fa, en |
| ru | Русский | 79 MB | 7 MB | ru, fa, en |
| th | ไทย | 78 MB | 6 MB | th, fa, en |
| tr | Türkçe | 79 MB | 7 MB | tr, fa, en |
| uk | Українська | 79 MB | 7 MB | uk, fa, en |
| vi | Tiếng Việt | 78 MB | 6 MB | vi, fa, en |
| zh | 中文 | 79 MB | 7 MB | zh, fa, en |

### Texas (`texas`)

Base `osm-texas-2026-10-26b.zim` 2.6 GB · packs 1.4 GB · 22 versions, 59.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,712 MB | 76 MB | ar, es, en |
| de | Deutsch | 2,712 MB | 76 MB | de, es, en |
| el | Ελληνικά | 2,701 MB | 67 MB | el, es, en |
| en | English | 2,697 MB | 64 MB | es, en |
| es | Español | 2,698 MB | 64 MB | es, en |
| fa | فارسی | 2,713 MB | 78 MB | fa, es, en |
| fr | Français | 2,719 MB | 84 MB | fr, es, en |
| he | עברית | 2,699 MB | 65 MB | he, es, en |
| hi | हिन्दी | 2,698 MB | 64 MB | hi, es, en |
| id | Bahasa Indonesia | 2,701 MB | 66 MB | id, es, en |
| it | Italiano | 2,719 MB | 83 MB | it, es, en |
| ja | 日本語 | 2,705 MB | 69 MB | ja, es, en |
| ko | 한국어 | 2,702 MB | 68 MB | ko, es, en |
| nl | Nederlands | 2,701 MB | 65 MB | nl, es, en |
| pl | Polski | 2,708 MB | 73 MB | pl, es, en |
| pt | Português | 2,701 MB | 66 MB | pt, es, en |
| ru | Русский | 2,707 MB | 71 MB | ru, es, en |
| th | ไทย | 2,698 MB | 64 MB | th, es, en |
| tr | Türkçe | 2,703 MB | 68 MB | tr, es, en |
| uk | Українська | 2,714 MB | 78 MB | uk, es, en |
| vi | Tiếng Việt | 2,708 MB | 74 MB | vi, es, en |
| zh | 中文 | 2,707 MB | 71 MB | zh, es, en |

### Thailand (`thailand`)

Base `osm-thailand-2026-11-10.zim` 1.2 GB · packs 1.1 GB · 21 versions, 26.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,249 MB | 80 MB | ar, th, en |
| de | Deutsch | 1,256 MB | 85 MB | de, th, en |
| el | Ελληνικά | 1,245 MB | 76 MB | el, th, en |
| en | English | 1,246 MB | 77 MB | th, en |
| es | Español | 1,250 MB | 80 MB | es, th, en |
| fa | فارسی | 1,247 MB | 78 MB | fa, th, en |
| fr | Français | 1,252 MB | 83 MB | fr, th, en |
| he | עברית | 1,246 MB | 76 MB | he, th, en |
| hi | हिन्दी | 1,245 MB | 76 MB | hi, th, en |
| it | Italiano | 1,254 MB | 84 MB | it, th, en |
| ja | 日本語 | 1,254 MB | 84 MB | ja, th, en |
| ko | 한국어 | 1,249 MB | 80 MB | ko, th, en |
| nl | Nederlands | 1,250 MB | 81 MB | nl, th, en |
| pl | Polski | 1,247 MB | 78 MB | pl, th, en |
| pt | Português | 1,248 MB | 79 MB | pt, th, en |
| ru | Русский | 1,251 MB | 81 MB | ru, th, en |
| th | ไทย | 1,244 MB | 75 MB | th, en |
| tr | Türkçe | 1,247 MB | 78 MB | tr, th, en |
| uk | Українська | 1,247 MB | 77 MB | uk, th, en |
| vi | Tiếng Việt | 1,252 MB | 83 MB | vi, th, en |
| zh | 中文 | 1,252 MB | 82 MB | zh, th, en |

### The Carolinas (`carolinas`)

Base `osm-carolinas-2026-10-25b.zim` 1.5 GB · packs 1.8 GB · 22 versions, 35.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,608 MB | 92 MB | ar, en |
| de | Deutsch | 1,606 MB | 90 MB | de, en |
| el | Ελληνικά | 1,598 MB | 68 MB | el, en |
| en | English | 1,532 MB | 0 MB | en |
| es | Español | 1,609 MB | 93 MB | es, en |
| fa | فارسی | 1,606 MB | 75 MB | fa, en |
| fr | Français | 1,614 MB | 98 MB | fr, en |
| he | עברית | 1,599 MB | 68 MB | he, en |
| hi | हिन्दी | 1,598 MB | 67 MB | hi, en |
| id | Bahasa Indonesia | 1,600 MB | 84 MB | id, en |
| it | Italiano | 1,611 MB | 95 MB | it, en |
| ja | 日本語 | 1,604 MB | 88 MB | ja, en |
| ko | 한국어 | 1,600 MB | 70 MB | ko, en |
| nl | Nederlands | 1,600 MB | 84 MB | nl, en |
| pl | Polski | 1,605 MB | 74 MB | pl, en |
| pt | Português | 1,601 MB | 85 MB | pt, en |
| ru | Русский | 1,605 MB | 89 MB | ru, en |
| th | ไทย | 1,598 MB | 67 MB | th, en |
| tr | Türkçe | 1,602 MB | 86 MB | tr, en |
| uk | Українська | 1,608 MB | 92 MB | uk, en |
| vi | Tiếng Việt | 1,600 MB | 69 MB | vi, en |
| zh | 中文 | 1,608 MB | 92 MB | zh, en |

### Turkey (`turkey`)

Base `osm-turkey-2026-10-26b.zim` 2.7 GB · packs 3.3 GB · 23 versions, 67.8 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,938 MB | 208 MB | tr, ku, ar, en |
| de | Deutsch | 2,959 MB | 224 MB | de, tr, ku, ar, en |
| el | Ελληνικά | 2,950 MB | 218 MB | el, tr, ku, ar, en |
| en | English | 2,939 MB | 208 MB | tr, ku, ar, en |
| es | Español | 2,952 MB | 221 MB | es, tr, ku, ar, en |
| fa | فارسی | 2,954 MB | 221 MB | fa, tr, ku, ar, en |
| fr | Français | 2,963 MB | 231 MB | fr, tr, ku, ar, en |
| he | עברית | 2,941 MB | 212 MB | he, tr, ku, ar, en |
| hi | हिन्दी | 2,938 MB | 209 MB | hi, tr, ku, ar, en |
| id | Bahasa Indonesia | 2,948 MB | 216 MB | id, tr, ku, ar, en |
| it | Italiano | 2,960 MB | 228 MB | it, tr, ku, ar, en |
| ja | 日本語 | 2,943 MB | 212 MB | ja, tr, ku, ar, en |
| ko | 한국어 | 2,941 MB | 212 MB | ko, tr, ku, ar, en |
| nl | Nederlands | 2,946 MB | 214 MB | nl, tr, ku, ar, en |
| pl | Polski | 2,947 MB | 217 MB | pl, tr, ku, ar, en |
| pt | Português | 2,949 MB | 218 MB | pt, tr, ku, ar, en |
| ru | Русский | 2,962 MB | 227 MB | ru, tr, ku, ar, en |
| th | ไทย | 2,939 MB | 210 MB | th, tr, ku, ar, en |
| tr | Türkçe | 2,936 MB | 208 MB | tr, ku, ar, en |
| uk | Українська | 2,956 MB | 225 MB | uk, tr, ku, ar, en |
| vi | Tiếng Việt | 2,974 MB | 247 MB | vi, tr, ku, ar, en |
| zh | 中文 | 2,948 MB | 217 MB | zh, tr, ku, ar, en |
| ku | Kurdî | 2,937 MB | 208 MB | tr, ku, ar, en |

### Ukraine, Moldova & Western Russia (`ukraine`)

Base `osm-ukraine-2026-10-26b.zim` 3.3 GB · packs 7.5 GB · 20 versions, 83.6 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 4,188 MB | 917 MB | ar, ru, uk, ro, en |
| de | Deutsch | 4,210 MB | 932 MB | de, ru, uk, ro, en |
| el | Ελληνικά | 4,172 MB | 889 MB | el, ru, uk, ro, en |
| en | English | 4,171 MB | 901 MB | ru, uk, ro, en |
| es | Español | 4,212 MB | 937 MB | es, ru, uk, ro, en |
| fa | فارسی | 4,181 MB | 897 MB | fa, ru, uk, ro, en |
| fr | Français | 4,216 MB | 942 MB | fr, ru, uk, ro, en |
| he | עברית | 4,177 MB | 893 MB | he, ru, uk, ro, en |
| hi | हिन्दी | 4,168 MB | 886 MB | hi, ru, uk, ro, en |
| id | Bahasa Indonesia | 4,179 MB | 907 MB | id, ru, uk, ro, en |
| it | Italiano | 4,201 MB | 928 MB | it, ru, uk, ro, en |
| ja | 日本語 | 4,179 MB | 910 MB | ja, ru, uk, ro, en |
| nl | Nederlands | 4,185 MB | 913 MB | nl, ru, uk, ro, en |
| pt | Português | 4,186 MB | 915 MB | pt, ru, uk, ro, en |
| ru | Русский | 4,151 MB | 868 MB | ru, uk, ro, en |
| th | ไทย | 4,170 MB | 888 MB | th, ru, uk, ro, en |
| tr | Türkçe | 4,181 MB | 909 MB | tr, ru, uk, ro, en |
| uk | Українська | 4,166 MB | 885 MB | ru, uk, ro, en |
| vi | Tiếng Việt | 4,181 MB | 899 MB | vi, ru, uk, ro, en |
| ro | Română | 4,171 MB | 901 MB | ru, uk, ro, en |

### Uttar Pradesh & Central India (`india-central`)

Base `osm-india-central-2026-11-05b.zim` 1.8 GB · packs 1.5 GB · 21 versions, 39.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,883 MB | 83 MB | ar, hi, ur, en |
| de | Deutsch | 1,885 MB | 85 MB | de, hi, ur, en |
| el | Ελληνικά | 1,879 MB | 80 MB | el, hi, ur, en |
| en | English | 1,880 MB | 81 MB | hi, ur, en |
| es | Español | 1,885 MB | 85 MB | es, hi, ur, en |
| fa | فارسی | 1,882 MB | 83 MB | fa, hi, ur, en |
| fr | Français | 1,887 MB | 87 MB | fr, hi, ur, en |
| he | עברית | 1,880 MB | 80 MB | he, hi, ur, en |
| hi | हिन्दी | 1,879 MB | 79 MB | hi, ur, en |
| id | Bahasa Indonesia | 1,882 MB | 82 MB | id, hi, ur, en |
| it | Italiano | 1,885 MB | 85 MB | it, hi, ur, en |
| ja | 日本語 | 1,882 MB | 82 MB | ja, hi, ur, en |
| ko | 한국어 | 1,880 MB | 81 MB | ko, hi, ur, en |
| nl | Nederlands | 1,882 MB | 82 MB | nl, hi, ur, en |
| pl | Polski | 1,882 MB | 82 MB | pl, hi, ur, en |
| ru | Русский | 1,884 MB | 84 MB | ru, hi, ur, en |
| th | ไทย | 1,880 MB | 81 MB | th, hi, ur, en |
| uk | Українська | 1,882 MB | 83 MB | uk, hi, ur, en |
| vi | Tiếng Việt | 1,882 MB | 83 MB | vi, hi, ur, en |
| zh | 中文 | 1,882 MB | 82 MB | zh, hi, ur, en |
| ur | اردو | 1,879 MB | 79 MB | hi, ur, en |

### Uttar Pradesh & Central India (Light) (`india-central-light`)

Base `osm-india-central-light-2026-11-07.zim` 0.9 GB · packs 1.5 GB · 22 versions, 20.5 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 935 MB | 70 MB | ar, hi, ur, en |
| de | Deutsch | 936 MB | 70 MB | de, hi, ur, en |
| el | Ελληνικά | 933 MB | 68 MB | el, hi, ur, en |
| en | English | 934 MB | 69 MB | hi, ur, en |
| es | Español | 935 MB | 70 MB | es, hi, ur, en |
| fa | فارسی | 933 MB | 69 MB | fa, hi, ur, en |
| fr | Français | 935 MB | 70 MB | fr, hi, ur, en |
| he | עברית | 933 MB | 68 MB | he, hi, ur, en |
| hi | हिन्दी | 933 MB | 68 MB | hi, ur, en |
| id | Bahasa Indonesia | 935 MB | 70 MB | id, hi, ur, en |
| it | Italiano | 935 MB | 70 MB | it, hi, ur, en |
| ja | 日本語 | 934 MB | 69 MB | ja, hi, ur, en |
| ko | 한국어 | 933 MB | 68 MB | ko, hi, ur, en |
| nl | Nederlands | 935 MB | 70 MB | nl, hi, ur, en |
| pl | Polski | 933 MB | 69 MB | pl, hi, ur, en |
| pt | Português | 935 MB | 70 MB | pt, hi, ur, en |
| ru | Русский | 936 MB | 70 MB | ru, hi, ur, en |
| th | ไทย | 933 MB | 68 MB | th, hi, ur, en |
| uk | Українська | 934 MB | 69 MB | uk, hi, ur, en |
| vi | Tiếng Việt | 933 MB | 69 MB | vi, hi, ur, en |
| zh | 中文 | 935 MB | 70 MB | zh, hi, ur, en |
| ur | اردو | 933 MB | 68 MB | hi, ur, en |

### Washington, D.C. (`washington-dc`)

Base `osm-washington-dc-2026-10-24b.zim` 0.1 GB · packs 0.1 GB · 22 versions, 3.2 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 147 MB | 11 MB | ar, es, en |
| de | Deutsch | 148 MB | 12 MB | de, es, en |
| el | Ελληνικά | 145 MB | 9 MB | el, es, en |
| en | English | 144 MB | 8 MB | es, en |
| es | Español | 144 MB | 8 MB | es, en |
| fa | فارسی | 147 MB | 11 MB | fa, es, en |
| fr | Français | 150 MB | 14 MB | fr, es, en |
| he | עברית | 147 MB | 11 MB | he, es, en |
| hi | हिन्दी | 145 MB | 9 MB | hi, es, en |
| id | Bahasa Indonesia | 146 MB | 10 MB | id, es, en |
| it | Italiano | 148 MB | 12 MB | it, es, en |
| ja | 日本語 | 147 MB | 11 MB | ja, es, en |
| ko | 한국어 | 146 MB | 10 MB | ko, es, en |
| nl | Nederlands | 146 MB | 10 MB | nl, es, en |
| pl | Polski | 146 MB | 10 MB | pl, es, en |
| pt | Português | 146 MB | 10 MB | pt, es, en |
| ru | Русский | 148 MB | 12 MB | ru, es, en |
| th | ไทย | 145 MB | 9 MB | th, es, en |
| tr | Türkçe | 145 MB | 9 MB | tr, es, en |
| uk | Українська | 147 MB | 11 MB | uk, es, en |
| vi | Tiếng Việt | 145 MB | 9 MB | vi, es, en |
| zh | 中文 | 149 MB | 13 MB | zh, es, en |

### West Africa (`west-africa`)

Base `osm-west-africa-2026-10-26b.zim` 2.6 GB · packs 3.0 GB · 27 versions, 74.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 2,756 MB | 124 MB | ar, pcm, ha, yo, en |
| de | Deutsch | 2,765 MB | 130 MB | de, pcm, ha, yo, en |
| el | Ελληνικά | 2,749 MB | 120 MB | el, pcm, ha, yo, en |
| en | English | 2,750 MB | 119 MB | pcm, ha, yo, en |
| es | Español | 2,763 MB | 130 MB | es, pcm, ha, yo, en |
| fa | فارسی | 2,753 MB | 123 MB | fa, pcm, ha, yo, en |
| fr | Français | 2,774 MB | 139 MB | fr, pcm, ha, yo, en |
| he | עברית | 2,749 MB | 120 MB | he, pcm, ha, yo, en |
| hi | हिन्दी | 2,748 MB | 119 MB | hi, pcm, ha, yo, en |
| id | Bahasa Indonesia | 2,754 MB | 122 MB | id, pcm, ha, yo, en |
| it | Italiano | 2,764 MB | 131 MB | it, pcm, ha, yo, en |
| ja | 日本語 | 2,754 MB | 124 MB | ja, pcm, ha, yo, en |
| ko | 한국어 | 2,751 MB | 122 MB | ko, pcm, ha, yo, en |
| nl | Nederlands | 2,756 MB | 124 MB | nl, pcm, ha, yo, en |
| pl | Polski | 2,756 MB | 126 MB | pl, pcm, ha, yo, en |
| pt | Português | 2,756 MB | 124 MB | pt, pcm, ha, yo, en |
| ru | Русский | 2,759 MB | 126 MB | ru, pcm, ha, yo, en |
| th | ไทย | 2,748 MB | 119 MB | th, pcm, ha, yo, en |
| tr | Türkçe | 2,752 MB | 121 MB | tr, pcm, ha, yo, en |
| uk | Українська | 2,754 MB | 124 MB | uk, pcm, ha, yo, en |
| vi | Tiếng Việt | 2,750 MB | 121 MB | vi, pcm, ha, yo, en |
| zh | 中文 | 2,755 MB | 125 MB | zh, pcm, ha, yo, en |
| pcm | Naijá | 2,747 MB | 119 MB | pcm, ha, yo, en |
| ha | Hausa | 2,747 MB | 119 MB | pcm, ha, yo, en |
| yo | Yorùbá | 2,747 MB | 119 MB | pcm, ha, yo, en |
| ig | Igbo | 2,751 MB | 122 MB | ig, pcm, ha, yo, en |
| ff | Fulfulde | 2,751 MB | 122 MB | ff, pcm, ha, yo, en |

### West Coast US (`west-coast-us`)

Base `osm-west-coast-us-2026-10-26b.zim` 4.8 GB · packs 4.4 GB · 22 versions, 110.3 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 5,021 MB | 218 MB | ar, es, en |
| de | Deutsch | 5,037 MB | 233 MB | de, es, en |
| el | Ελληνικά | 5,002 MB | 186 MB | el, es, en |
| en | English | 4,999 MB | 184 MB | es, en |
| es | Español | 5,000 MB | 184 MB | es, en |
| fa | فارسی | 5,028 MB | 210 MB | fa, es, en |
| fr | Français | 5,051 MB | 248 MB | fr, es, en |
| he | עברית | 5,005 MB | 188 MB | he, es, en |
| hi | हिन्दी | 5,000 MB | 184 MB | hi, es, en |
| id | Bahasa Indonesia | 5,008 MB | 189 MB | id, es, en |
| it | Italiano | 5,031 MB | 228 MB | it, es, en |
| ja | 日本語 | 5,018 MB | 214 MB | ja, es, en |
| ko | 한국어 | 5,009 MB | 192 MB | ko, es, en |
| nl | Nederlands | 5,013 MB | 210 MB | nl, es, en |
| pl | Polski | 5,019 MB | 202 MB | pl, es, en |
| pt | Português | 5,009 MB | 190 MB | pt, es, en |
| ru | Русский | 5,020 MB | 216 MB | ru, es, en |
| th | ไทย | 5,001 MB | 185 MB | th, es, en |
| tr | Türkçe | 5,011 MB | 192 MB | tr, es, en |
| uk | Українська | 5,027 MB | 208 MB | uk, es, en |
| vi | Tiếng Việt | 5,006 MB | 190 MB | vi, es, en |
| zh | 中文 | 5,033 MB | 225 MB | zh, es, en |

### West India (`india-west`)

Base `osm-india-west-2026-11-05b.zim` 1.1 GB · packs 1.0 GB · 25 versions, 29.4 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 1,177 MB | 53 MB | ar, mr, gu, hi, ur, en |
| de | Deutsch | 1,178 MB | 60 MB | de, mr, gu, hi, ur, en |
| el | Ελληνικά | 1,174 MB | 52 MB | el, mr, gu, hi, ur, en |
| en | English | 1,175 MB | 51 MB | mr, gu, hi, ur, en |
| es | Español | 1,179 MB | 55 MB | es, mr, gu, hi, ur, en |
| fa | فارسی | 1,176 MB | 53 MB | fa, mr, gu, hi, ur, en |
| fr | Français | 1,180 MB | 56 MB | fr, mr, gu, hi, ur, en |
| he | עברית | 1,175 MB | 52 MB | he, mr, gu, hi, ur, en |
| hi | हिन्दी | 1,174 MB | 51 MB | mr, gu, hi, ur, en |
| id | Bahasa Indonesia | 1,176 MB | 53 MB | id, mr, gu, hi, ur, en |
| it | Italiano | 1,179 MB | 55 MB | it, mr, gu, hi, ur, en |
| ja | 日本語 | 1,176 MB | 53 MB | ja, mr, gu, hi, ur, en |
| ko | 한국어 | 1,175 MB | 52 MB | ko, mr, gu, hi, ur, en |
| nl | Nederlands | 1,176 MB | 52 MB | nl, mr, gu, hi, ur, en |
| pl | Polski | 1,176 MB | 53 MB | pl, mr, gu, hi, ur, en |
| pt | Português | 1,177 MB | 53 MB | pt, mr, gu, hi, ur, en |
| ru | Русский | 1,177 MB | 60 MB | ru, mr, gu, hi, ur, en |
| th | ไทย | 1,175 MB | 52 MB | th, mr, gu, hi, ur, en |
| tr | Türkçe | 1,175 MB | 52 MB | tr, mr, gu, hi, ur, en |
| uk | Українська | 1,176 MB | 53 MB | uk, mr, gu, hi, ur, en |
| vi | Tiếng Việt | 1,177 MB | 54 MB | vi, mr, gu, hi, ur, en |
| zh | 中文 | 1,176 MB | 53 MB | zh, mr, gu, hi, ur, en |
| mr | मराठी | 1,174 MB | 51 MB | mr, gu, hi, ur, en |
| gu | ગુજરાતી | 1,174 MB | 51 MB | mr, gu, hi, ur, en |
| ur | اردو | 1,174 MB | 51 MB | mr, gu, hi, ur, en |

### West India (Light) (`india-west-light`)

Base `osm-india-west-light-2026-11-07.zim` 0.6 GB · packs 1.0 GB · 25 versions, 15.1 GB if each were its own file

| Lang | Name | Size | Download with the base already held | Wikipedia |
|---|---|---:|---:|---|
| ar | العربية | 605 MB | 37 MB | ar, mr, gu, hi, ur, en |
| de | Deutsch | 605 MB | 37 MB | de, mr, gu, hi, ur, en |
| el | Ελληνικά | 604 MB | 37 MB | el, mr, gu, hi, ur, en |
| en | English | 604 MB | 37 MB | mr, gu, hi, ur, en |
| es | Español | 605 MB | 37 MB | es, mr, gu, hi, ur, en |
| fa | فارسی | 604 MB | 37 MB | fa, mr, gu, hi, ur, en |
| fr | Français | 605 MB | 37 MB | fr, mr, gu, hi, ur, en |
| he | עברית | 604 MB | 37 MB | he, mr, gu, hi, ur, en |
| hi | हिन्दी | 604 MB | 37 MB | mr, gu, hi, ur, en |
| id | Bahasa Indonesia | 605 MB | 37 MB | id, mr, gu, hi, ur, en |
| it | Italiano | 605 MB | 37 MB | it, mr, gu, hi, ur, en |
| ja | 日本語 | 604 MB | 37 MB | ja, mr, gu, hi, ur, en |
| ko | 한국어 | 604 MB | 37 MB | ko, mr, gu, hi, ur, en |
| nl | Nederlands | 605 MB | 37 MB | nl, mr, gu, hi, ur, en |
| pl | Polski | 604 MB | 37 MB | pl, mr, gu, hi, ur, en |
| pt | Português | 605 MB | 37 MB | pt, mr, gu, hi, ur, en |
| ru | Русский | 605 MB | 37 MB | ru, mr, gu, hi, ur, en |
| th | ไทย | 604 MB | 37 MB | th, mr, gu, hi, ur, en |
| tr | Türkçe | 605 MB | 37 MB | tr, mr, gu, hi, ur, en |
| uk | Українська | 604 MB | 37 MB | uk, mr, gu, hi, ur, en |
| vi | Tiếng Việt | 604 MB | 37 MB | vi, mr, gu, hi, ur, en |
| zh | 中文 | 605 MB | 37 MB | zh, mr, gu, hi, ur, en |
| mr | मराठी | 604 MB | 37 MB | mr, gu, hi, ur, en |
| gu | ગુજરાતી | 604 MB | 37 MB | mr, gu, hi, ur, en |
| ur | اردو | 604 MB | 37 MB | mr, gu, hi, ur, en |
