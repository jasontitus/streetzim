# attic/

Retired scripts, kept for their history and as worked examples. Nothing in
the live tree runs anything here. They reference machines, accounts and
paths that no longer exist (the author's Mac, AWS terrain spot instances,
April-2026 rollouts), or they were one-offs, or they had declared
themselves deprecated.

The inventory, and the reason each script was retired, is in
[docs/scripts.md](../docs/scripts.md) (Phase 1). To bring one back:
`git mv attic/<path> <path>`.

Legacy format writers retired later (docs/formats.md, "Version support and
retirement"):

- `upgrade_spatial_zim.py` (+ `test_upgrade_spatial_zim.py`): upgraded SZCI
  v1 spatial ZIMs to v2 in place. Current builds write SZCI v3, so a ZIM that
  needed it is rebuilt instead. It imports `cloud.search_shards`, so running
  it again means moving it back to `cloud/`.
