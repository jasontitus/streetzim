# Viewer slots: patching a ZIM's viewer without re-packing it

## The problem

`cloud/swap_viewer_rust.py` replaces the three viewer files by rewriting the
**entire** ZIM: every entry is re-emitted and every cluster re-compressed.
Changing 600 KB of viewer therefore costs:

| region | size | re-pack time |
|---|---|---|
| washington-dc | 226 MB | ~75 s |
| switzerland | 2.2 GB | ~5 min |
| east-coast-us | 11 GB | ~33 min |
| europe | 71 GB | hours |

On 2026-09-19 five viewer iterations landed across 49 regions. That is the
cost this removes.

## The mechanism

The three viewer files (`index.html`, `places.html`, `routing-worker.js`) are
written into fixed-size **uncompressed** slots, padded to a known length,
with a marker that records that length:

```
<content><opener>SZVSLOT1:<name padded 24>:<slot len 12 digits>:<newlines><closer>
```

`<opener>/<closer>` are `<!--`/`-->` for HTML and `/*`/`*/` for JS, so the
padding is inert to the parser and the file still parses from byte zero.

**Uncompressed is the mechanism, not an oversight.** Bytes inside a
compressed cluster do not map to file offsets, so there would be nothing to
overwrite in place.

`cloud/patch_viewer_inplace.py` then:

1. scans for the marker (a linear read; the marker carries the slot length so
   nothing is inferred),
2. refuses if the new viewer does not fit,
3. writes `content + header + filler + closer` over exactly the slot,
4. recomputes the MD5 that libzim stores in the final 16 bytes.

Single source of truth for the format is `cloud/viewer_slots.py`; the writer
and the patcher both import it. They previously built the header
independently, which would have drifted.

## Cost

Slots are right-sized per file rather than a flat 1 MB each:

| file | slot | typical content | headroom |
|---|---|---|---|
| index.html | 1 MB | ~450 KB | 2.3x |
| places.html | 256 KB | ~101 KB | 2.5x |
| routing-worker.js | 128 KB | ~68 KB | 1.9x |

**1.4 MB uncompressed per ZIM** (a flat 1 MB each cost 3.1 MB). That is 0.06%
of switzerland and 0.002% of europe. The browser is served the padding too,
which is why it lives inside a comment — a 1 MB `index.html` carrying ~600 KB
of trailing comment boots and renders with no page errors.

## Usage

```sh
# Build a ZIM with slots (the normal swap path; slots are automatic)
python3 cloud/swap_viewer_rust.py <src.zim> <out.zim>

# Later, replace just the viewer -- 0.1 s instead of a re-pack
python3 cloud/patch_viewer_inplace.py <zim> --viewer-dir resources/viewer
python3 cloud/patch_viewer_inplace.py <zim> --dry-run   # show fit, write nothing

# Prove a slotted/patched ZIM differs from its source only in the viewer
python3 cloud/verify_slot_integrity.py <source.zim> <slotted.zim>
```

## What it does NOT do

**It does not shrink the upload.** Archive.org has no partial update, so the
whole file still ships. This saves the re-pack, not the transfer. For europe
that is hours saved on pack and ~1 h still spent on upload.

## Verification (2026-09-20, washington-dc, 9,010 entries)

Structural, source vs slotted vs slotted+patched:

- entries 9010 -> 9010 -> 9010; articles 1553 unchanged
- fulltext + title Xapian indexes present in all three
- `routing-data/` 11 cells preserved
- main entry `index.html` unchanged; all metadata hashes identical
- blob diff: **exactly 3 entries differ** (the viewer files) and nothing else
- every entry fetched: 9,009 blobs + 1 redirect, **0 errors**, 1,105 MB read

External validators, all three files:

| tool | verdict |
|---|---|
| `zimcheck -A` (zimru, Rust) | Overall Test Status: **Pass** |
| `zimru check` | checksum OK |
| libzim `Archive.check()` | True |
| `kiwix-manage add` (independent parser) | rc=0 |
| `cloud/validate_zim.py` | PASS |
| `cloud/route_cli.py` | route OK 0.2 s |
| search / find gates | 15 / 7048 (identical across all three) |

Adversarial cases, all passing:

- **double-patch** — marker relocates when content length changes; second
  patch restores cleanly and `check()` stays true
- **overflow** — refuses at 1,650,734 B against a 1,048,576 B slot and leaves
  the file valid rather than truncating
- **no-slot ZIM** — rejected with a message naming `swap_viewer_rust.py`
- **mimetype / title / main-entry drift** — checked explicitly; a `text/html`
  entry served as octet-stream would otherwise have passed a bytes-only diff

## Known gaps

1. **The patch is not atomic.** It writes in place, so a crash or power loss
   mid-write leaves a structurally valid ZIM with a stale checksum — corrupt,
   and only detectable by a full verify. Either patch a copy and `rename()`,
   or hard-require that the file is neither serving nor uploading. Not yet
   implemented.
2. **Xapian goes stale.** `index.html`'s bytes change but the fulltext index
   is not rebuilt, so Kiwix's own search can return stale text for the app
   shell. Harmless today; it matters once city stubs land (see the city-stub
   TODO in STATUS-2026-09-18.md).
3. **A slotted ZIM gets a new UUID.** `swap_viewer_rust.py` creates a fresh
   archive, so Kiwix treats the result as a different book rather than an
   update. This is already true of every swap shipped to date — not a
   regression, but worth knowing.

## Gate

`.allzims-nearfix.sh` now fails a region whose output lacks
`SZVSLOT1:index.html` / `SZVSLOT1:places.html`, so a ZIM cannot ship without
patchable slots and quietly cost a full re-pack at the next viewer change.
