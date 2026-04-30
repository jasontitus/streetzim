# Updating streetzim.web.app

The Firebase site shows the catalog of region cards (DC, California, etc.)
backed by the `streetzim-<id>` items on archive.org. After uploading a new
ZIM (or any time you want a fresh card grid), the site needs to be
regenerated and redeployed.

The deploy step needs `firebase` CLI logged in. The Linux build host doesn't
have firebase configured — run from your Mac (or any machine with `firebase
login` done).

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
   `web/generate.py` (which has display names, descriptions, and the
   default zoom).
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

- **The Linux build host's `web/index.html` is stale.** The upload
  pipeline regenerates it on each upload but the firebase deploy step
  fails (no firebase login there) and the file just sits. Don't pull
  it back to your Mac — let your Mac re-query archive.org freshly.
- **Item metadata is eventually consistent.** Right after `ia upload`
  finishes, archive.org's metadata API can lag by minutes. If a brand-
  new ZIM doesn't appear in the homepage card grid after a deploy,
  wait 5–10 min and re-run `web/generate.py --deploy`.
- **Firebase auth expired?** `firebase logout && firebase login`. The
  CLI sometimes silently uses a stale token; the deploy step exits
  with `Failed to authenticate, have you run firebase login?`.
- **`node` / `npm` mismatch.** Firebase-tools needs Node 18+. If
  `firebase deploy` fails on `Unsupported engine`, upgrade Node first.
- **Adding a new region:** edit `REGIONS` in `web/generate.py` (a new
  entry needs `id` matching `streetzim-<id>`, plus `name`,
  `description`, optional `default_zoom`). The card won't appear on
  the homepage until both (a) the entry is in `REGIONS` and (b) the
  archive.org item exists. Push the registry change and redeploy.

## What runs where

| Step | Machine | Why |
|---|---|---|
| `cloud/upload_validated.sh` (validate + ia upload + cleanup) | Linux build host | Has the freshly built ZIMs locally; ia configured. |
| `web/generate.py --deploy` (regenerate + firebase deploy) | Mac (or any host with `firebase login`) | Source of truth is archive.org, not the build host's filesystem. |

After uploads complete on the build host, deploying is a single command
on your Mac. No file transfer needed between hosts.
