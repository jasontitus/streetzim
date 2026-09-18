# preview-proxy — range/CORS proxy for the online map preview

The `/drive/` viewer on streetzim.web.app can stream a ZIM straight off
archive.org instead of a local file, so a visitor can look at a region
before downloading it. archive.org's download servers answer HTTP Range
requests but send no CORS headers, and its `/cors/` endpoint sends CORS
headers but ignores Range, so the browser needs this small proxy in
between. It forwards ranged GET/HEAD requests for `streetzim-*` items to
`https://archive.org/download/…`, streams the bytes back and adds the CORS
headers. Nothing is buffered or stored.

Files:

- `ia-range-proxy.js` — the handler (`handleRequest(Request) → Response`),
  Fetch-API only, so it runs unchanged on Cloudflare Workers, Deno or Node.
- `worker.js`, `wrangler.toml` — Cloudflare Workers packaging.
- `node-adapter.mjs`, `serve-local.mjs` — run the handler on Node for
  local testing (`node preview-proxy/serve-local.mjs 8766`).
- `firebase/` — optional Cloud Functions (2nd gen) packaging
  (`index.mjs`, `package.json`) for serving it from streetzim.web.app
  itself via a Hosting rewrite. Blaze plan, and Hosting bills every
  byte served ($0.15/GB) where Workers bill none; see
  docs/online-preview.md for the comparison and its verification state.

The byte range is accepted as a `Range: bytes=start-end` header or as
`?bytes=start-end` in the query (the viewer sends both; the query wins).
The query form is there for CDNs in front of the proxy: it is part of
the cache key, and Firebase Hosting is not documented to forward `Range`
to a function.

Deploy on Cloudflare Workers (free tier: 100k requests/day, no bandwidth
charge — a preview session is a few hundred requests):

```sh
cd preview-proxy
npx wrangler login
npx wrangler deploy          # prints https://streetzim-preview-proxy.<acct>.workers.dev
```

Then set that URL in `web/drive/preview-config.js`, regenerate and deploy
the site (`python3 web/generate.py --deploy`). Step-by-step plan with the
checks for each step: `docs/preview-proxy-cloudflare.md`. Design,
alternatives and measurements: `docs/online-preview.md`.
