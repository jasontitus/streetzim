# Online preview — streaming a ZIM off archive.org

The `/drive/` PWA renders a `.zim` the visitor picks from disk. This
feature lets it render one it never downloaded: the viewer reads the ZIM
straight off archive.org with HTTP range requests, so a visitor can look
at a region — pan, zoom, search, open a place — before deciding to fetch
a multi-gigabyte file. The catalog offers it as a **Preview** button next
to each Download button.

- `https://streetzim.web.app/drive/?zim=https://archive.org/download/streetzim-washington-dc/osm-washington-dc-2026-09-08.zim`
  streams that file and goes straight into the viewer.
- `?zim=streetzim-washington-dc` (or the item's `/details/` URL) looks up
  the item's newest `.zim` first, with the same dated-filename rule
  `web/generate.py` uses for the catalog.
- The picker page also has a URL box for the same thing.
- Inside the viewer a banner names the streamed file and its host and
  carries the Download link; everything else — search, chips, places,
  wiki popups, directions — works as it does on a local file, only
  slower.

Nothing is stored except the URL: the service worker keeps it in
IndexedDB like it keeps a picked File, and re-opens the stream after it
has been idle.

## Why there is a proxy

A browser can only do this if the file server answers `Range` requests
*and* sends CORS headers. Measured against
`streetzim-washington-dc/osm-washington-dc-2026-09-08.zim` on
2026-09-18, with an `Origin: https://streetzim.web.app` header:

| URL | Range honoured | CORS headers |
|---|---|---|
| `archive.org/download/<item>/<file>` → 302 → `ia8xxxxx.us.archive.org/…` | yes, `206` + `Content-Range` | none on either hop |
| `archive.org/serve/<item>/<file>` | same as download | none |
| `archive.org/cors/<item>/<file>` | **no** — `200` and the whole 226 MB body | yes (`Access-Control-Allow-Origin`, `Range` allowed) |
| `archive.org/metadata/<item>` (JSON) | n/a | yes |

No archive.org URL offers both, so `preview-proxy/` sits in between: a
few dozen lines that forward a ranged `GET`/`HEAD` for one StreetZim
file to `archive.org/download/`, follow the redirect to the storage
node, stream the body back and add the CORS headers. It does not buffer
or store anything, and it refuses everything that is not a
`streetzim-*` item, a `.zim` file, or a single-range request, so it is
not a general archive.org proxy.

Until the proxy is deployed the feature stays off: the picker reports
that no proxy is configured and the catalog renders no Preview buttons.

## Switching it on

1. Deploy the proxy. Cloudflare Workers is the intended home — the free
   plan allows 100,000 requests a day with no bandwidth charge, and a
   preview session is a few hundred requests (see *Costs*):

   ```sh
   cd preview-proxy
   npx wrangler login
   npx wrangler deploy      # → https://streetzim-preview-proxy.<account>.workers.dev
   ```

   Check it: `curl -sI -H 'Range: bytes=0-79' https://<worker>/streetzim-washington-dc/osm-washington-dc-2026-09-08.zim`
   should say `206`, `content-range: bytes 0-79/…` and
   `access-control-allow-origin: *`. `node tests/test_preview_proxy.mjs`
   runs the handler itself against archive.org.

   The handler is plain Fetch API (`handleRequest(Request) → Response`),
   so Deno Deploy or any Node ≥ 18 host works too
   (`preview-proxy/serve-local.mjs` is the Node adapter). A Firebase
   Cloud Function / Cloud Run rewrite under the same origin would also
   work but needs the Blaze plan, pays egress per GB, and must stream
   the response (routing chunks are up to 100 MB).

2. Put the worker URL in `web/drive/preview-config.js`:

   ```js
   window.STREETZIM_PREVIEW_PROXY = 'https://streetzim-preview-proxy.<account>.workers.dev';
   ```

3. Regenerate and deploy the site as usual (`python3 web/generate.py
   --deploy`). `generate.py` reads that file and adds the Preview
   buttons; the deploy ships the config with the PWA. An empty string
   turns the feature off again the same way.

## How it works

`web/drive/zim-reader.js` gained `StreetZimHttpSource`, a Blob
look-alike — it has `size` and `slice(a, b).arrayBuffer()`, which is all
`StreetZimReader` ever calls — implemented on HTTP range requests:

- Reads shorter than 128 KiB are served from **aligned 128 KiB blocks**
  held in an LRU (256 blocks, 32 MB). The dirent binary search revisits
  the same few blocks for every lookup, and identical aligned ranges let
  the browser HTTP cache answer repeats after the service worker (and
  its memory) has been torn down — the proxy marks responses
  `Cache-Control: public, max-age=86400`, safe because filenames are
  dated and never change content.
- Reads of a block or more — whole clusters, routing chunks — fetch
  their exact range and are not cached.
- Concurrent requests for the same block or range share one fetch (the
  viewer asks for twenty tiles at once and their lookups walk the same
  dirents).
- **Lookups probe 3 entries per round in parallel** instead of one. A
  binary search is strictly sequential — one dependent read per level,
  ~25 levels on a 39 M-entry continent — and on a remote source every
  level is a network round trip. Three evenly spaced probes per round
  need log₄ rounds instead of log₂ levels; probes that are further
  apart than a block read only their own few bytes, probes that have
  crowded together read blocks that the final steps and neighbouring
  lookups reuse; and parsed dirents are memoised by index, so the top
  rounds — identical for every lookup — are fetched once per reader.
  Why 3 and not more: see the fanout table below. Local Blobs keep the
  plain binary search, which touches fewer bytes.
- **Raw (uncompressed) clusters are read piecemeal**: info byte, blob
  offset table, then just the blob. Satellite, terrain and routing data
  live in such clusters, and a whole-cluster read would have pulled
  2 MiB (or a 100 MB routing chunk's neighbours) for one tile. zstd
  clusters must be fetched whole because the frame decodes from its
  start; that is one 0.7–2 MB request per cluster and then every blob
  in it is free.
- A server that answers a `Range` request with `200` is refused before
  its body is read — exactly what `archive.org/cors/` would otherwise
  have done to a 28 GB Canada file.
- Transient failures (network errors, `5xx`, `429`) are retried three
  times.

`web/drive/sw.js` stores `{url, sourceUrl, name}` instead of `{blob}`
when the picker sends a URL, builds the reader on an `HttpRangeSource`,
and answers its `status` message with `source: 'url'`, the archive.org
URL, the size and the request tally. The picker (`web/drive/index.html`)
resolves `?zim=` and the URL box onto the proxy; the viewer
(`resources/viewer/index.html`) asks the SW on `/drive/viewer/` and shows
the banner when the source is a URL. Inside Kiwix there is no such
service worker, so nothing changes there.

### What a preview costs

Washington, D.C. (216 MB, 9,010 entries, 331 clusters) in headless
Chrome, measured by `cloud/preview_smoke_test.mjs`:

| | local range server | archive.org via the proxy |
|---|---|---|
| picker → viewer (header + MIME list read) | 1.3 s | 2.3 s |
| viewer API ready | 1.6 s | 5.0 s |
| search index loaded | 1.9 s | 5.8 s |
| first view rendered | 18 range requests, 4.8 MB | 18 range requests, 4.8 MB |

The bytes are dominated by whole zstd clusters (tiles, fonts, the
search manifest). Panning fetches new clusters as it goes; tiles already
seen are cached in the reader like they are for a local file.

Larger ZIMs cost more per *cold* lookup, not per byte of file. Europe —
62.1 GiB, 38.9 M entries, 81,881 clusters, the largest file on
archive.org — in the same headless Chrome through the local proxy
(HTTP/2): viewer ready 16 s after the click, the 211-million-place
search index loaded at 40 s, 126 range requests / 6.9 MB for the first
view. Read straight off archive.org from Node, one thing at a time
(each round trip ~0.5 s from the test host, two proxies deep; a browser
talking to a Cloudflare edge should see roughly half that):

| Europe, cold | time | requests | fetched |
|---|---|---|---|
| open (header + MIME list) | 1.3 s | 1 | 128 KiB |
| first lookup ever (`map-config.json`) | 8.1 s | 45 | 1.9 MiB |
| next lookup in a new part of the tree (`search-data/manifest.json`, 1.3 MB) | 5.5 s | 36 | 0.8 MiB |
| 3×3 tiles at z4, concurrent | 6.5 s | 41 | 4.1 MiB |
| the 16 tiles around them | 1.2 s | 2 | 0.9 MiB |
| 3×3 tiles at z14, concurrent | 5.8 s | 40 | 4.3 MiB |
| the 16 tiles around them | 2.3 s | 7 | 2.6 MiB |

Neighbouring tiles share all but the last probe rounds, so a viewport
settles after its first few lookups. The cluster pointer table (8 bytes
per cluster, 640 KiB for Europe) is read once per reader instance.

How many probes per round was measured, not guessed. The viewer starts
some twenty lookups at once, so a wider fan-out shortens each lookup's
dependent chain but multiplies the requests competing for the same
connections; archive.org served about 15 range requests in parallel
from the test host. Europe in Chrome through the HTTP/2 proxy:

| probes per round | viewer ready | search index | requests / bytes, first view |
|---|---|---|---|
| 1 (plain binary search) | 15 s | 32 s | 95 / 15.3 MB |
| **3** | 16 s | 37–40 s | 126 / 6.9 MB |
| 6 | 21 s | 48 s | 159 / 8.4 MB |
| 16 | 33 s | 72 s | 231 / 10.4 MB |

Three ties with the binary search on time, moves less than half the
bytes (the sparse probes read a kilobyte where the binary search reads
a 128 KiB block), and shortens an isolated cold lookup — a search
keystroke, a tapped place — by a third. In Node, alone, the 16-way
probe was the fastest single lookup (5.9 s vs 15.5 s), which is the
measurement to distrust: real viewers never do one lookup at a time.
The knob is `fanout` on `StreetZimHttpSource`.

Chrome stops an idle service worker after ~30 s. The next request
re-opens the source (one 128 KiB request) and re-reads the cluster
pointer table; the browser HTTP cache usually answers both.

### Costs on the proxy

A preview session is one request per block touched plus one per cluster
loaded — a few hundred for a look around, more for a long Directions
route (the routing cells index and chunks are tens of MB and stream
through the same path). On the Cloudflare free plan (100,000
requests/day, no bandwidth charge) that is several hundred sessions a
day; the paid plan is $5/month for 10 M requests. Cloudflare does count
the whole body streamed through the worker against nothing but CPU time,
which is negligible here.

## Running it on the StreetZim site itself

Everything visitors see already runs on streetzim.web.app: the picker,
the service worker, the viewer banner and the catalog buttons are static
files under `web/` deployed by the usual `firebase deploy`. The one
piece Firebase Hosting cannot run is the proxy, because it has to execute
code per request. Two ways to host it, and the bill is what separates
them — the proxy's traffic *is* the visitors' data usage:

| | Cloudflare Worker | Firebase Function behind a Hosting rewrite |
|---|---|---|
| hostname | `streetzim-preview-proxy.<acct>.workers.dev` (visitors never see it) | `streetzim.web.app/ia/…` |
| plan | free (100k requests/day; paid $5/month for 10 M) | Blaze (billing account) |
| bytes served | **not billed** | Hosting data transfer $0.15/GB after 360 MB/day, plus function outbound $0.12/GB after 5 GB/month if the function→CDN leg counts as internet egress (not confirmed) |
| 1,000 preview sessions at ~20 MB each | $0 | roughly $3–5 |
| long transfers | streams without a size or wall-clock limit | Hosting cuts a request off at 60 s |
| `Range` header reaching the code | yes | not documented; the `?bytes=` query form is what makes it work regardless |

**Cloudflare is the recommendation**; the step-by-step plan is
[`preview-proxy-cloudflare.md`](preview-proxy-cloudflare.md).

The Firebase route is packaged in `preview-proxy/firebase/`
(`index.mjs`, a 2nd-gen `onRequest` wrapping the same handler, with its
`package.json`; the `firebase.json` additions are in the file's header
comment and are deliberately not applied, so a plain `firebase deploy
--only hosting` keeps working without a functions setup). Its state:
the function loads in the Firebase emulator and answers a ranged request
correctly (206, CORS, `Content-Range`, cache headers) when called
directly; the Hosting→function hop could not be exercised from the
build sandbox, whose egress proxy the emulator's internal call insists
on using, and nothing has been deployed to the real project. Because
Firebase's docs do not promise to forward the `Range` header to a
function, the viewer also sends the range as `?bytes=start-end` and the
proxy prefers that form: Hosting forwards query strings and keys its CDN
cache on them, so partial responses cache correctly there without
`Vary` tricks. Either way the picker only needs the base URL in
`preview-config.js` (`https://streetzim.web.app/ia` for the rewrite).

## Security

ZIM content — search pages, wiki articles — renders on the
`streetzim.web.app` origin, so a link must not be able to point the
viewer at a ZIM on a hostile server. The picker only streams
`https://archive.org/download/streetzim-*/….zim` (rewritten onto the
proxy) or same-origin URLs (used by the local smoke test); everything
else is refused with a message. The proxy independently refuses
anything that is not a `streetzim-*` item and a `.zim` file, requires a
single-range `Range` header on `GET`, and never follows a caller-supplied
URL.

## Testing

```sh
node tests/test_zim_http_source.mjs [some.zim]     # range source: synthetic ZIM + real one
node tests/test_preview_proxy.mjs [some.zim]       # proxy handler against archive.org (network)
node tests/test_sw_streaming.mjs                   # the worker's data path in Chromium (synthetic ZIM)
node tests/test_picker.mjs                         # the picker's flows in Chromium
python3 -m pytest tests/test_generate_preview_button.py
ZIM_FILE=/path/osm-washington-dc-2026-09-08.zim CHROME_PATH=... SHOT_DIR=/tmp \
  H2=1 node cloud/preview_smoke_test.mjs all       # picker → viewer, both modes
IA_ZIM=https://archive.org/download/streetzim-europe/osm-europe-2026-05-06.zim \
  CHROME_PATH=... H2=1 node cloud/preview_smoke_test.mjs proxy   # a big one
```

`H2=1` runs the local proxy as HTTPS/HTTP-2 on a throw-away
certificate, like a deployed worker; without it Chrome's six-connection
limit per HTTP/1.1 host makes the timings pessimistic. Two more modes
for a deployment: `PROXY_URL=https://<worker> node
tests/test_preview_proxy.mjs` runs the proxy checks over the network
against a deployed proxy, and `SITE_URL=https://streetzim.web.app node
cloud/preview_smoke_test.mjs proxy` drives the live site's picker with
whatever `preview-config.js` it serves.

For hands-on local testing, serve `web/` with `scripts/serve-web-local.py`
(it answers `Range`), run `node preview-proxy/serve-local.mjs 8766`, set
`window.STREETZIM_PREVIEW_PROXY = 'http://127.0.0.1:8766'` in your local
copy of `preview-config.js`, and open
`http://127.0.0.1:8765/drive/?zim=https://archive.org/download/streetzim-washington-dc/osm-washington-dc-2026-09-08.zim`.
A same-origin URL (`?zim=http://127.0.0.1:8765/some.zim` with the file
next to `drive/`) needs no proxy at all.

## Limits

- Needs a connection the whole time; there is no "keep what you saw".
- Slower than a downloaded file by the network round trip per cold
  block or cluster — fine for looking around, not a replacement for the
  offline file. The banner says so and links the download.
- Directions on a big region streams the routing cells it needs; expect
  tens of MB and long spinners for cross-country routes.
- archive.org occasionally serves `5xx` under load; the source retries
  five times over ~8 s. A failed open shows on the picker as "Failed to
  open ZIM from archive.org"; once the viewer is up, a failing source
  turns into a 503 with `X-Streetzim-Upstream` from the worker, a note
  in the banner (including the proxy's 429 daily limit) and, if the map
  itself cannot start, a "not answering" line with Try again.
- Phones with Data Saver on are not auto-streamed from a catalog link;
  the picker fills the URL in and waits for Stream. The banner counts
  the bytes streamed so far.
