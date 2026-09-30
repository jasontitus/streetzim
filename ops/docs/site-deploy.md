# Updating streetzim.web.app

The Firebase site shows the catalog of region cards (DC, California, etc.)
backed by the `streetzim-<id>` items on archive.org. After uploading a new
ZIM (or any time you want a fresh card grid), the site needs to be
regenerated and redeployed.

The deploy step needs `firebase` CLI logged in. The Linux build host has it
(`/usr/local/bin/firebase`, logged in): `cloud/upload_validated.sh` runs
`web/generate.py --deploy` there after every upload it completes. By hand,
run it from the build host or any machine with `firebase login` done.

## Prereqs

One-time setup, on the deploy machine:

- Repo checked out (anywhere)
- `firebase login` completed
- `node` / `npm` (for firebase-tools)
- `firebase.json`, `.firebaserc`, `web/template.html`, `scripts/sync-drive-viewer.sh` — already in the repo

## Deploy

After uploads finish on the build host:

```sh
cd ~/experiments/streetzim    # or wherever your repo lives
git pull                       # pick up upload-pipeline + viewer changes
python3 web/generate.py --deploy
```

`web/generate.py --deploy` does three things in order:

1. **Queries archive.org** at `https://archive.org/advancedsearch.php` for
   all items with identifier `streetzim-*`. This is the source of truth
   for which regions are live and what their sizes are. Newly uploaded
   ZIMs show up here automatically — no client-side state to update.
2. **Renders `web/index.html`** from `web/template.html` using the fetched
   item list, joined with the static `REGIONS` registry inside
   `web/generate.py` (id, tier, title, `zim_file` and description per
   region).
3. **Runs `firebase deploy --only hosting`**, which fires the predeploy
   hook `bash scripts/sync-drive-viewer.sh` (pulls the right MapLibre /
   fzstd versions into `web/drive/viewer/`) and then pushes everything
   under `web/` to Firebase Hosting.

## Preview without deploying

```sh
python3 web/generate.py        # generates web/index.html locally, no deploy
open web/index.html            # local preview in browser
```

## Common gotchas

- **The build host's deploy output is never committed.** Every deploy
  there rewrites `web/index.html` (and `cloud/deploy_pwa.sh` rewrites
  `web/drive/build-info.js`, `web/drive/sw.js`, `web/drive/viewer/.version`),
  so `git status` on the host shows them modified. Leave them; don't pull
  them to another machine — `web/generate.py` re-queries archive.org.
- **Item metadata is eventually consistent.** Right after `ia upload`
  finishes, archive.org's metadata API can lag by minutes. If a brand-
  new ZIM doesn't appear in the homepage card grid after a deploy,
  wait 5–10 min and re-run `web/generate.py --deploy`.
- **Firebase auth expired?** `firebase logout && firebase login`. The
  CLI sometimes silently uses a stale token; the deploy step exits
  with `Failed to authenticate, have you run firebase login?`.
- **`node` / `npm` mismatch.** Firebase-tools needs Node 18+. If
  `firebase deploy` fails on `Unsupported engine`, upgrade Node first.
- **Preview buttons missing / unwanted:** `web/generate.py` only renders
  a Preview button on live cards when `web/drive/preview-config.js`
  names the range proxy the picker streams archive.org ZIMs through.
  Empty string = no buttons. Deploying the proxy and switching it on is
  covered in `docs/online-preview.md`.
- **Adding a new region:** edit `REGIONS` in `web/generate.py` (a new
  entry needs `id` matching `streetzim-<id>`, plus `tier` (one of the
  ids in `TIERS`), `title`, `zim_file`, and `description`; see the
  existing entries). The card won't appear on
  the homepage until both (a) the entry is in `REGIONS` and (b) the
  archive.org item exists. `web/generate.py` refuses to generate (and so
  to deploy) when a `cloud/regions.tsv` region is live on archive.org but
  has no `REGIONS` entry. Push the registry change and redeploy.

## What runs where

| Step | Machine | Why |
|---|---|---|
| `cloud/upload_validated.sh` (validate + ia upload + torrent + prune + deploy) | Linux build host | Has the freshly built ZIMs locally; ia and firebase configured. |
| `web/generate.py --deploy` by hand (after a deferred listing, or a site-only change) | build host, or any host with `firebase login` | Source of truth is archive.org, not the build host's filesystem. |

An upload that exits 6 ("listing pending") skips the deploy;
`cloud/finish_pending_uploads.sh` completes it later. No file transfer is
needed between hosts.
