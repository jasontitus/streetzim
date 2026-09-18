# Plan: the preview proxy on Cloudflare Workers

The online preview (`docs/online-preview.md`) needs one small service
that Firebase Hosting cannot provide: something that forwards a byte
range request to archive.org and returns the bytes with CORS headers.
This is the plan for running it on Cloudflare Workers, start to finish,
with the checks that tell you each step worked. Budget: about half an
hour the first time, most of it account setup; the deploy itself is one
command.

## Why Cloudflare Workers

- **Free at this scale.** The Workers Free plan allows 100,000 requests
  per day and, unlike Cloud Run or Functions, charges nothing for the
  bytes streamed through. A preview session is a few dozen to a few
  hundred requests (Washington DC's first view is 18, Europe's 126), so
  the free plan covers hundreds to a few thousand sessions a day. The
  paid plan is $5/month for 10 M requests, then $0.30 per million.
- **Streams without limits.** The worker hands archive.org's response
  body straight through; there is no response-size cap and no wall-clock
  timeout for a streaming response (the 10 ms CPU budget is nowhere near
  touched — the worker does no work per byte).
- **HTTP/2 and HTTP/3 to the browser** on `*.workers.dev`, so the
  viewer's parallel lookups are not throttled by the six-connection cap
  browsers apply to HTTP/1.1 hosts.
- **Nothing to maintain.** No server, no OS, no certificates; the code is
  the 100-line `preview-proxy/ia-range-proxy.js` already in this repo.

The one thing it cannot do is answer on `streetzim.web.app`: that host
belongs to Firebase. The proxy answers on its own hostname, which
visitors never see — it appears only in `preview-config.js`.

## Step 1 — account and CLI (once)

1. Create a Cloudflare account at <https://dash.cloudflare.com/sign-up>
   (free plan; no domain or payment method needed for Workers). On first
   use of Workers the dashboard asks you to pick a `*.workers.dev`
   subdomain, e.g. `streetzim` → your workers live under
   `https://<name>.streetzim.workers.dev`.
2. On the Mac (anything with Node 18+):

   ```sh
   cd ~/experiments/streetzim/preview-proxy   # wherever the repo lives
   npx wrangler login                          # opens the browser, grants the CLI access
   npx wrangler whoami                         # prints the account — confirms login
   ```

   `wrangler` is Cloudflare's CLI; `npx` fetches it, nothing to install.

## Step 2 — deploy

```sh
cd preview-proxy
npx wrangler deploy
```

`wrangler.toml` names the worker `streetzim-preview-proxy` and points at
`worker.js`, which imports the shared handler. The command prints the
URL, of the form

```
https://streetzim-preview-proxy.<subdomain>.workers.dev
```

Re-running `npx wrangler deploy` after a code change replaces the worker
in place, globally, in a few seconds.

## Step 3 — check the worker alone

From any machine:

```sh
W=https://streetzim-preview-proxy.<subdomain>.workers.dev
curl -sI -H 'Range: bytes=0-79' \
  $W/streetzim-washington-dc/osm-washington-dc-2026-09-08.zim
```

Expected: `HTTP/2 206`, `content-range: bytes 0-79/226265704`,
`content-length: 80`, `access-control-allow-origin: *`,
`access-control-expose-headers: Content-Range, …`. Then the full check
list, over the network against the deployed worker:

```sh
PROXY_URL=$W node tests/test_preview_proxy.mjs
```

It verifies ranged GETs (header and `?bytes=` forms), HEAD, the CORS
preflight, and that non-StreetZim items, non-.zim files, whole-file
GETs and multi-range requests are refused. Any current file name works;
the DC one above is the default and small.

## Step 4 — switch the site on

1. Put the worker URL in `web/drive/preview-config.js`:

   ```js
   window.STREETZIM_PREVIEW_PROXY = 'https://streetzim-preview-proxy.<subdomain>.workers.dev';
   ```

2. Regenerate and deploy the site the usual way:

   ```sh
   python3 web/generate.py --deploy
   ```

   `generate.py` reads that file and renders a **Preview** button on
   every live card, right after Download (`/drive/?zim=<the card's
   download URL>`); the deploy ships `preview-config.js` with the PWA.
   Commit the config change so the next deploy from any machine keeps it.

3. Check the live site end to end from a browser: open a card's Preview
   button, or run the harness against production:

   ```sh
   SITE_URL=https://streetzim.web.app CHROME_PATH=... \
     node cloud/preview_smoke_test.mjs proxy
   ```

   It drives `streetzim.web.app/drive/?zim=…` for the DC file (or
   `IA_ZIM=<another download URL>`), waits for the redirect into the
   viewer, the search index coming through the service worker, the
   worker's range-request tally, and the banner, and fails on any
   console error.

Turning it off again is the reverse: empty string in
`preview-config.js`, redeploy — the buttons disappear and the picker
refuses archive.org URLs. The worker can stay; unused, it costs nothing.

## Step 5 — watch it

- **Dashboard:** Workers & Pages → `streetzim-preview-proxy` → Metrics
  shows requests, errors and CPU time per day; the free-plan counter
  (100k/day) is on the same page. Errors here almost always mean
  archive.org answered 5xx or timed out — the worker passes those
  statuses through and never caches them.
- **Live logs** while testing: `npx wrangler tail` streams every request
  with status and timing.
- **Quota exhausted** (an unexpectedly popular day): the worker answers
  `429` until midnight UTC; the picker reports "Failed to open ZIM from
  archive.org" and the site is otherwise unaffected. Upgrading to the
  $5/month plan in the dashboard lifts it immediately.

## Later, if wanted

- **Own hostname.** If a domain is ever moved to Cloudflare DNS, add
  `routes = [{ pattern = "preview.<domain>", custom_domain = true }]` to
  `wrangler.toml` and redeploy; the URL in `preview-config.js` changes,
  nothing else. `streetzim.web.app` itself cannot be used — it is
  Firebase's hostname.
- **Edge caching.** Every block the viewer asks for is an aligned range
  under a URL that carries it (`?bytes=start-end`), so popular blocks —
  the top of every region's directory, the first view of its map —
  could be kept at the edge with the Workers Cache API and served
  without a trip to archive.org. Cloudflare's cache will not store 206
  responses as such; the worker would store the partial body as a 200
  under the ranged URL and turn it back into a 206 on a hit. Not needed
  for the free tier's numbers; noted here because it is a twenty-line
  change if archive.org ever becomes the slow part.
- **Rate limiting.** The worker already refuses everything that is not
  a StreetZim `.zim` range request. If someone still hammers it, the
  Workers Rate Limiting binding (per-IP, a few lines) is the next step.

## Alternatives, for the record

- **Firebase Cloud Function behind a Hosting rewrite** — keeps
  everything under `streetzim.web.app` (`/ia/<item>/<file>`), needs the
  Blaze plan, bills Hosting data transfer at $0.15/GB after 360 MB/day
  and function egress at $0.12/GB after 5 GB/month, and Hosting cuts
  requests off at 60 s. `preview-proxy/firebase/` has the packaging;
  `docs/online-preview.md` has the details.
- **Deno Deploy / any Node host** — the handler is plain Fetch API;
  `preview-proxy/serve-local.mjs` shows the Node wrapper. Same code,
  different pricing.
