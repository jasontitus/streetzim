# Rebuild follow-ups

`cloud/rebuild_old_regions.sh` snapshots its work list once, at line 63
(`ORDER=$(cat rebuild-old.list)`), before the loop. Appending to
`rebuild-old.list` while the queue runs therefore does nothing, and
restarting the queue to pick it up would re-run all 50 regions. Queue
follow-ups here instead and run them after the current pass finishes.

## rebuild-followup.list

- **switzerland-light** — dropped from the main list in `fe02ced` because the
  variant path did not exist yet and it would have shipped a full satellite
  build under the "Light" label. The recipe now works
  (`cloud/region-variants.tsv`, `build-region-fast.sh`) and it shipped
  successfully as a one-off on 2026-09-26, so it only needs to rejoin the
  queue for the next data refresh, not urgently: the live 1.5 GB build is
  current.
