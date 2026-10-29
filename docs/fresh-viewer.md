# Today's viewer without a new upload

A map's viewer (the three files `index.html`, `places.html`,
`routing-worker.js`) changes far more often than its data. Shipping a viewer
fix used to mean a new file on archive.org: a viewer swap is quick
(`cloud/swap_viewer_rust.py`, under a minute for Taipei), but the upload is
the whole map (17 GB for Africa, 60 GB for Europe), archive.org takes hours
to list it on a busy day, and every language version needs new recipes.

Since 2026-10-29 a map's Download can be **the archive.org file with today's
viewer**, served by the build worker (`build-proxy/merged-webseed.js`, the
Cloudflare Worker that already serves language versions), with nothing
uploaded.

## How

A map's viewer files sit in fixed-size uncompressed slots
(`cloud/viewer_slots.py`, docs/viewer-slots.md), so the map with another
viewer is the same bytes except the three slots, the UUID and the trailing
MD5. `tools/langpack/fresh_viewer.py REGION ZIM ...`:

1. checks ZIM is the file archive.org serves (size and MD5 as the item lists);
2. makes the map with this checkout's viewer (`tools/langpack/swap_base_viewer.py`:
   a reflink copy, the slots written, a fresh UUID derived from the old one
   and the viewer's revision, the MD5; every item but the viewer compared
   byte for byte). Kiwix tells books apart by UUID, so the file is a new
   book; the same viewer again gives the same file (the same torrent);
3. gates it as Kiwix runs it (`tools/langpack/fresh_viewer_gates.sh`:
   kiwix-serve, the UI, viewer and smoke gates of a base, and
   `cloud/viewer_scroll_check.mjs`). Routing, search and zimcheck see the
   same bytes as the live file, which passed them when it was built;
4. writes, into `web/build/recipes/REGION-viewer/` (a folder of its own:
   `ops/langpack/publish_site.sh` replaces `REGION/` whole):
   - `L.ws.json`, the worker's segment index (streetzim-webseed/1): byte
     ranges of the archive.org file and blobs `ws-<sha>.bin` for the bytes
     that differ (half a MB: the new `index.html` region, the UUID, the
     MD5). The worker serves it at
     `/merged/REGION-viewer/L/<uuid>/<file>`, with ranges, If-Range against
     archive.org's ETag (a replaced file is refused, never spliced), 8
     subrequests for the whole file;
   - `L.torrent` (webseed: that URL);
   - `fresh.json`: which file it was made from (name, MD5, ETag), the new
     file's name (`<map>.v<viewer rev>.zim`), UUID, MD5s and URL.

   The index's bytes are read once, for the torrent's pieces and two MD5s:
   that of all but the last 16 bytes must equal those 16 bytes, so the
   worker serves exactly the file that passed the gates.

`web/generate.py` (`fresh_viewer`) points a card's Download (and the
library page's) at the worker while `fresh.json` was made from the very
file archive.org serves for the map; a newer map has none until it is made
again. The page asks the worker first (a HEAD, which it answers without
fetching the map) and downloads archive.org's file instead when the worker
cannot serve it (its daily budget, switched off, no answer in 4 s).

## Rollout

After a viewer change merges: for each live map, `fresh_viewer.py` with the
local copy (every live map has one on the build host), then one site deploy.
Per map: the MD5 check, the swap's MD5 and comparison, and the index read
are about four reads of the file (Taipei: 74 s with the gates; all 75 maps,
430 GB, a few hours of disk), and no upload.

## What it does not change

- archive.org's file, its torrent and the mirrors keep the viewer the map
  was built with until the next rebuild; the Torrent button is still the
  archive.org file's.
- A map built before viewer slots (2026-09-20) cannot get one; every live
  map was built after.
- The worker's daily request budget is shared with the language versions'
  webseeds; a browser download is one request.

## Composable ZIMs (kiwix/overview#182)

The Kiwix discussion of composable ZIMs raised three points this follows:

- **Hashing cost on the device.** Nothing is hashed on the phone here: the
  worker serves a file whose checksum was computed once, on the build host.
  (The /build/ page, which does assemble in the browser, checks every
  16 MiB chunk against the recipe as it writes.)
- **Offline users.** Composing needs the network, but the result is an
  ordinary ZIM that can be copied and passed on like any other, and the
  part that changes is small: the three viewer files, about 1 MiB. A
  reader that resolved entries across a base and an "update pack" would
  not need the composed file at all; the segment index here (a base's byte
  ranges plus small blobs) describes the same thing as a file.
- **Vocabulary and the catalog.** Here: a *base* (the map on archive.org),
  *language packs* (Wikipedia, search) and the *viewer*; a *language
  version* or a *map with today's viewer* is a base composed with them,
  described by a recipe (`streetzim-recipe/2`) or a segment index
  (`streetzim-webseed/1`), each naming its inputs by file, size and hash.
