# Language packs on the build host: one-time setup

`ops/langpack/install.sh` does all of it; without `--apply` it only says what
it would do (exit 3 when something is missing, 0 when nothing is).

```bash
cd /storage/streetzim                # the checkout that runs ship_packs.sh
ops/langpack/install.sh              # the plan; changes nothing
ops/langpack/install.sh --apply      # does it; every step is skipped when already done
```

What it sets up, and why:

1. **zim-tools 3.8.0** at `/storage/streetzim/tools/zim-tools_linux-x86_64-3.8.0/`.
   The gates accept only the official zimcheck (the `zimcheck` on PATH here
   is zimru's, which misreads hrefs with HTML entities). The tarball must have
   SHA-256 `9d4cbf68675670176fe72f096364c7f4ecf09950340e769bdb81dcbb5030da00`;
   the copy already on the host (`/storage/experiments/langpack/merge/tools/dl/zim-tools.tar.gz`)
   is used when it matches, else
   `https://download.openzim.org/release/zim-tools/zim-tools_linux-x86_64-3.8.0.tar.gz`
   is downloaded and checked.
2. **playwright-core 1.59.1** for the browser gates: `npm ci` in
   `ops/langpack/` only, from its own `package.json` and lock (npm run at the
   repository root, or in a folder without a `package.json`, walks up and
   rewrites production's `package.json`; the script refuses that). It uses
   the npm next to `.browser-libs`' node 20. The Chromium is playwright's
   `~/.cache/ms-playwright/chromium-1217` with `.browser-libs`' libraries,
   already on the host; puppeteer (the smoke) is the repository's own.
3. **`/storage/streetzim/langpack-work`**, with `wpcache/` seeded from the
   Greece end-to-end's cache (`/storage/experiments/langpack-prod/e2e/wpcache`,
   8.9 GB: SQL dumps, title maps and Wikidata text for the 21 pack languages,
   and Greece's base scan; a reflink copy where the filesystem allows), only
   when `wpcache/` does not exist yet. New languages' dumps (Romansh, the
   region Wikipedias) are fetched by `prefetch.py` on first use.
4. **`langpack_env_check`** (`ops/langpack/host-env.sh`): every path the
   pipeline reads.

Overrides (environment): `TOOLS`, `ZIM_TOOLS_TARBALL`, `ZIM_TOOLS_URL`,
`ZIM_TOOLS_SHA256`, `SEED_WPCACHE`, `LANGPACK_WORK`, `NPM`.
`tests/langpack/test_install.py` runs the script in a sandbox.

Nothing here serves anything from the host, and no merged ZIM is uploaded:
only packs and search packs go to archive.org; visitors' browsers assemble
the maps.
