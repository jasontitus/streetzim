# Rolling a viewer change into every shipped region

Companion to [viewer-slots.md](viewer-slots.md), which explains the slot
mechanism. This page is the operational procedure: how to get a viewer change
into all ~57 live regions, what it costs, and what the gates are.

## The shape of the cost

Patching is free; **uploading is the entire cost.**

`cloud/patch_viewer_inplace.py` is a seek + write + checksum against the
uncompressed slots — 2.6 s on a 1.2 GB ZIM, and it does not depend on region
size in any meaningful way. But archive.org has no partial update, so a
patched region ships **the whole file** again. As of 2026-09-24 that is
425 GB across 56 patchable regions.

Measured upload rates vary by an order of magnitude between sessions
(2.5–17 MB/s on the same host, same week), so quote a range, not an ETA.

## Order: smallest first

`tools/make_rollout_list.py` emits the work list sorted by file size
ascending, so early regions land in minutes while the tail is still running:

```
washington-dc     223 MB
hawaii            236 MB
...
united-states      39 GB
europe             65 GB
```

Source of truth is the **rendered site** (`web/index.html`), which names per
region the exact file its Download button points at. Deriving the list from
the local directory instead picks up rejected builds and superseded dates.

Generate the list at rollout start, not in advance: regions that ship while
the rollout waits belong in it. A region that already carries the current
viewer costs nothing — the patch rewrites identical bytes and
`ia upload --checksum` skips the transfer.

## Per region

```
patch → validate_zim → markers → gates → upload_validated.sh
```

A region failing any step is recorded in `rollout-viewer-status.tsv` and
**skipped**; the run continues. The patch is in place and idempotent (the
slot is overwritten wholesale, never appended), so a failed gate leaves a
patched-but-unshipped local file and the next run simply re-patches it.

The marker check asserts the change being rolled out is actually **in** the
file. Without that a region can burn a 65 GB upload having changed nothing:

```python
"lakes layer":  b"water-lowzoom" in idx and b"_SZ_LAKES" in idx,
"chip nearest": b"_ranked.sort" in idx,
```

Update those two lines for whatever the next rollout ships.

Gates are the same four the build queues use — overlap at 320/390/430 px,
the 7-device matrix, the **render gate** (`tmp/map-health.mjs`, ≥100 rendered
features: the only gate that fails a blank map) and
`cloud/kiwix_viewer_gate.sh`.

## The work list is a data file

`cloud/rollout_viewer_patch.sh` reads `rollout-viewer.tsv` and re-reads it before
every region. This is deliberate: **bash reads a running script
incrementally**, so editing the script mid-run corrupts the run. To reorder,
extend or trim the remaining work, edit the TSV — never the script.

Same reason the queue scripts resolve each other by PID from pidfiles
(`.europe-countries.pid`, `.africa-rest.pid`, `.rollout-viewer.pid`) rather
than `pgrep -f`: a script's own `bash -c` cmdline contains every string it
searches for, so `pgrep -f` matches itself. See the `[x]pattern` trap note in
`feedback_process_scan_self_match`.

## Chaining

Builds own the CPU; the rollout owns the uplink. Running them together means
the upload starves the build's tile writer and vice versa, on a host shared
with another tenant. Each stage waits on the previous stage's pidfile:

```
europe countries → african regions → viewer rollout → canada re-pack
```

## Regions without slots

A region built before 2026-09-21 has no slots and `patch_viewer_inplace.py`
cannot touch it. `make_rollout_list.py` writes those to the tail of the TSV
commented out (`#noslot <id>`) so the rollout skips them rather than burning
an upload on a failed patch.

As of 2026-09-24 there is exactly one: **canada** (29.9 GB).

Fixing one costs a full `cloud/swap_viewer_rust.py` re-pack (~3 h for 30 GB)
— but that re-pack *pads the viewer into slots on the way out*, so it is the
last expensive one that region ever needs. `cloud/canada_viewer_repack.sh` does
this, and asserts the slots exist afterwards:

```python
"slot marker idx": b"SZVSLOT1:index.html" in idx,
"slot size idx":   a.get_entry_by_path("index.html").get_item().size == 1048576,
```

It writes a **new dated filename** and takes the new UUID that
`swap_viewer_rust` mints. That is correct here: Kiwix dedupes books by UUID,
so a same-UUID rewrite would leave anyone who already has the region on the
old viewer. (The opposite is true for an in-place patch, which deliberately
preserves the UUID so it reads as an update.)

It also refuses to start unless `2 × source size` is free — the re-pack holds
source and destination side by side.

## Listing lag

`upload_validated.sh` exits **6** = "transferred, listing pending" when
archive.org has not registered the file yet. That is not a failure: prune,
torrent and site deploy are correctly skipped (all three need the listing)
and the region is appended to `pending-uploads.tsv`.

This is common for multi-GB files — austria-czech, iberia and nordics each
waited hours on 2026-09-23. Finish them with:

```sh
bash cloud/finish_pending_uploads.sh          # all listed ones
# or one region, once `ia metadata streetzim-<id>` lists the file:
flock .retrofit-upload.lock env PROJECT_DIR=/storage/streetzim \
  bash cloud/upload_validated.sh <id> <zim>
```

The re-run is cheap: `ia upload --checksum` skips the transfer because the
md5 already matches.
