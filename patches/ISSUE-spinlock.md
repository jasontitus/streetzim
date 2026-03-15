# Issue: Writer pipeline uses spin-loop polling instead of condition variables

## Summary

The writer pipeline in `queue.h`, `workers.cpp`, and `workers.h` uses `microsleep()`-based spin loops for all thread synchronization. When compression workers are slow relative to the rate items are added, the spin loops waste CPU cycles and can cause thread starvation, leading to apparent hangs.

## Environment

- libzim 9.x (tested on commit f8cc2cb / v9.2.3)
- macOS ARM64 (Apple Silicon), 10+ compression workers
- Large ZIM files (56M+ tiles, 15+ GB source data)
- ZSTD compression (level 19, the hardcoded default)

## Problem

Four sites in the writer pipeline use busy-wait polling with `microsleep()`:

1. **`queue.h` `pushToQueue()`** — main thread spins waiting for queue space
2. **`queue.h` `waitAndPop()`** — worker threads spin waiting for tasks
3. **`workers.cpp` `taskRunner()`** — workers spin-poll the task queue
4. **`workers.cpp` `clusterWriter()`** — writer thread spins checking if head cluster is compressed
5. **`workers.h` `TrackableTask::waitNoMoreTask()`** — spins until all tasks complete

When the main thread fills the cluster queue (max size 10) faster than ZSTD compression can drain it, `pushToQueue()` enters a spin loop. With `microsleep()` granularity, this burns 100% CPU per spinning thread with zero I/O progress. On a machine with 10 worker threads, all idle workers also spin-wait, leading to 100% CPU across all cores with the ZIM file not growing.

This was initially misdiagnosed as a hang because the symptoms (100% CPU, no I/O, no progress) are identical to a deadlock. Thread sampling reveals the truth: all threads are in `microsleep()` or similar polling loops, not blocked on locks.

## Proposed Fix

Replace all five spin-loop sites with `std::condition_variable` waits:

- **`Queue`**: Add `m_pushCV` and `m_popCV` condition variables. `pushToQueue()` waits on `m_popCV` (signaled when a consumer pops). `waitAndPop()` waits on `m_pushCV` (signaled when a producer pushes).
- **`taskRunner()`**: Block on queue's `m_pushCV` until a task is available.
- **`clusterWriter()`**: Block on a dedicated `m_clusterClosedCV` until the head cluster's `isClosed()` returns true. Signal this CV from `Cluster::close()` or `ClusterTask::run()`.
- **`TrackableTask::waitNoMoreTask()`**: Block on a CV signaled when `waitingTaskCount` reaches 0.

This eliminates all CPU waste during queue backpressure and idle waiting, while preserving the existing threading semantics. The fix has been tested on US and World ZIM builds (56M and 345M tiles respectively).

## Patch

See the attached `0001-Replace-spin-loop-polling-with-condition-variables-t.patch`, which applies cleanly against current `main` (commit efc4e2a).

Files changed: `queue.h`, `workers.cpp`, `workers.h`, `creatordata.h`, `creator.cpp`, `clusterWorker.cpp`
