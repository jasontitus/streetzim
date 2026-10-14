# Assembling a language ZIM in the browser, from parts on archive.org

Status: design, 2026-10-13, an alternative delivery for `docs/language-packs.md`. It replaces the on-demand assembly server with the site's own web app. It is not built.

## The idea

**Hosting.** Only static files are hosted, on archive.org or somewhere like it:
- one base ZIM per region;
- one small language pack per (region, language);
- one tiny recipe per (region, language).

**Assembly.** The streetzim.web.app PWA (`/drive/`) assembles the ZIM in the visitor's browser:
- It reads the recipe, then fetches the ranges it lists from the base and the pack, in order.
- It writes them to a file on the visitor's disk.
- It computes the MD5 while writing, and appends it as the last 16 bytes.

Then the visitor opens the file in Kiwix, as with any downloaded ZIM. Nothing runs on a server of ours except the existing range proxy, which stores nothing.

## Why this is simple: the recipe

The merge prototype (`/storage/experiments/langpack/merge/`) showed what a merged ZIM is when it uses the "prefix" layout and a pack whose new paths sort after the base's (`~xx/…`, no deletions). It is exactly:

```
generated head (header 80 B + MIME list)          ~ 0.1-64 KB
base bytes [first cluster .. end of base dirents)  verbatim, same offsets
pack clusters                                      verbatim
generated tail (merge cluster, pointer lists,      ~ 3 KB + pointer lists
                new dirents)
MD5 of everything above                            16 B
```

Switzerland is 23 segments, Europe 34. A recipe lists them as `{source: base|pack|inline, offset, length}`, with the inline bytes base64-encoded. It also gives the expected total size and the expected MD5.

The browser does no ZIM logic at all. It splices ranges and hashes them.

**The recipe is made on the build host when a pack is published:**
- Merge base + pack once, with `streetzim merge`.
- Run the gates:
  - entry-level verification
  - zimcheck
  - kiwix-serve `/suggest` and `/search`
  - the viewer smoke
- Record the segments, the size and the MD5. The MD5 comes from that one verified merge.
- Publish the recipe next to the pack. A recipe is a few KB, plus the pointer lists for a large base, where those are generated.

**Integrity is end to end.** The UUID is derived from the base, the pack and the merger version, so a correct browser assembly is byte-identical to the merge that passed the gates. The browser compares its MD5 with the recipe's before calling the file done. A mismatch means a corrupt download, not a bad merge: the visitor sees "try again", and the file is not offered.

## Pieces in the browser

**1. Fetching ranges.** There are three candidate hosts:
- **archive.org.** It answers range requests, but not with CORS headers on the same responses. The site's existing range proxy, a Cloudflare Worker on the free plan (`preview-proxy/`), adds them and stores nothing, as the Preview button already does.
- **Hugging Face datasets.** Free public storage of large files, a CDN, and CORS on downloads. Needs checking: ranges, and whether the terms fit.
- **Cloudflare R2.** Native CORS and ranges, free egress, about $0.015 per GB-month of storage.

Requests are sequential, chunked (for example 64 MB), and retried. Two to four requests in flight improve throughput while keeping the order.

**2. MD5.** WebCrypto has no MD5, so a vendored WASM MD5 (hash-wasm, MIT) is used. The prototype measured 420 MB/s in V8, faster than any home download. The hash state can be saved and restored (hash-wasm `save()`/`load()`) for resuming.

**3. Writing the file.** The best available sink, in order:
- **File System Access** (`showSaveFilePicker` + `createWritable()`): Chrome and Edge on the desktop. The visitor picks the folder; writes stream straight to disk. A failed download can resume from the last written offset with the saved hash state.
- **A service-worker streamed download:** the page hands the stream to the service worker, which answers a navigation with `Content-Disposition: attachment` and the known Content-Length. The browser's own download manager saves it, with no storage quota. This is for Firefox, and Chrome on Android if File System Access is missing there.
- **OPFS, then a download link to the finished file:** for Safari. It is limited by the origin's storage quota, and the file is stored twice until the copy is done.

**4. Opening it.** The finished file opens in Kiwix like any downloaded ZIM:
- desktop: open the file;
- Android: Kiwix → add from storage;
- iOS: the Files app → share to Kiwix.

The web app can also open it at once, from the same file.

## What the visitor sees

On a region's card, a language menu sits next to Download. Picking "Français" on "United States" opens `/drive/build?region=united-states&lang=fr`, which shows:
- the file size;
- the expected time at the measured speed;
- "Save as…".

Then come a progress bar, the speed and "keep this tab open", and at the end "Verified (MD5 matches)" with an "Open in the web app" button. The top languages remain ordinary downloads and torrents of prebuilt merged files.

## Costs

| part | cost |
|---|---|
| Storage | Each base once, plus packs: map-only packs a few MB; with Wikipedia ~0.9 GB for Swiss German; with Kiwix full-text ~1.5 GB. No merged copies, except the prebuilt top languages. |
| Server compute | None at download time. At publish time, one merge and gate per (region, language): Switzerland seconds, the US about a minute plus gates. |
| Bandwidth | archive.org's, through the free Worker. |

## Limits to verify

- archive.org throughput per connection, and through the Worker.
- The Workers free plan's request limit: 100,000 requests a day, so about 50 US-sized downloads a day at 64 MB per request.
- Whether File System Access exists on Chrome for Android.
- Safari's service-worker stream downloads and its OPFS quota.
- Mobile tabs being suspended in the background, and the screen needing to stay on.
- Hugging Face's range and CORS behaviour, and its terms.
- Whether Kiwix on each platform takes a file the browser saved.
- Memory on a real phone. The one-click prototype (/lab/build/) streaming the 2.26 GB California map raised Chrome's RSS by about 415 MB on a desktop (measured locally, 2026-10-14). Its low-memory profile (2 chunks in flight, a 16 MiB service-worker queue) saves only about 50-90 MB of that: most of the rest is garbage-collection lag on the small buffers fetch hands back while a 16 MiB chunk arrives, plus WebCrypto's copy of each chunk for SHA-256. Check peak memory, and whether the tab is killed, on a 3-4 GB Android phone and an iPhone before relying on one-click there. If it is too much: a BYOB reader straight into the chunk buffer (loses the partial chunk on a network error, so it needs another way to resume) or an incremental SHA-256 (hash-wasm) instead of WebCrypto.
- One-click survives sleep and outages within a build (watchdogs restart after a late tick; downloads wait up to 30 min for the connection), but a build that stops starts over. Resuming across attempts (the written offset and the saved MD5 state, in OPFS) is not built.
