# libzim Patches

Two patches for [openzim/libzim](https://github.com/openzim/libzim), both discovered while building large ZIM files (56M+ tiles, US and World map builds).

Apply against libzim 9.x (tested on commit `f8cc2cb` / v9.2.3).

## Patch 1: Replace spin-loop polling with condition variables

**File:** `0001-Replace-spin-loop-polling-with-condition-variables-t.patch`

**Problem:** The writer pipeline uses `microsleep()` spin loops for all thread synchronization — task dispatch, cluster write ordering, queue backpressure, and task completion waiting. When compression workers are slow (e.g. ZSTD level 19 on 2 MiB clusters with 10 workers), the main thread fills the cluster queue faster than workers can drain it. Once the queue is full, the main thread enters `pushToQueue()` which spin-waits with `microsleep()`, burning 100% CPU with zero I/O. All worker threads also spin-wait when idle. On macOS with 56M tiles, this manifests as every thread at 100% CPU usage with the ZIM file not growing — appearing identical to a hang.

**Root cause:** `queue.h` `pushToQueue()` and `waitAndPop()` use a `while(!condition) { microsleep(); }` polling pattern instead of blocking on a synchronization primitive. Same pattern in `workers.cpp` `taskRunner()` and `clusterWriter()`, and in `workers.h` `TrackableTask::waitNoMoreTask()`.

**Fix:** Replace all four spin-loop sites with `std::condition_variable` waits:
- `Queue`: CV-based blocking for push backpressure (`m_popCV`) and pop notification (`m_pushCV`)
- `taskRunner`: blocks on queue CV until a task is available
- `clusterWriter`: blocks on dedicated CV (`m_clusterClosedCV`) until the head cluster is compressed
- `TrackableTask::waitNoMoreTask`: blocks on CV until task count reaches 0

**Files changed:** `queue.h`, `workers.cpp`, `workers.h`, `creatordata.h`, `creator.cpp`, `clusterWorker.cpp`, `meson.build`

---

## Patch 2: Fix infinite loop in Compressor when output buffer is exactly full

**File:** `0001-Fix-infinite-loop-in-Compressor-feed-when-output-buf.patch`

**Problem:** When building a ZIM file, one specific cluster causes ZSTD compression to spin at 100% CPU forever. The ZIM file stops growing and the build hangs indefinitely. The hang is deterministic — it occurs at the exact same cluster every time, regardless of worker count, ZSTD level, or libzim version (stock or patched).

**Root cause:** `Compressor::feed()` in `compression.h` has a `while(true)` loop that calls `stream_run_encode()` and checks the return status. When `CompStatus::OK` is returned with `avail_out == 0` (output buffer full), the code does `continue` to retry. This was designed for LZMA, which returns OK before BUF_ERROR when the output is full.

However, when **both** `avail_in == 0` (all input consumed) **and** `avail_out == 0` (output buffer exactly full), the `continue` creates an infinite loop:

1. `ZSTD_compressStream()` is called with 0-size input and 0-size output buffers
2. ZSTD makes no progress, returns OK (not an error)
3. `avail_in` is still 0, `avail_out` is still 0
4. Code hits `continue`, goes back to step 1

This triggers when the compressed output **exactly** equals the output buffer size (initially 1 MiB, or any power-of-2 after doubling). In our case, cluster 193 of a US map build (~2 MiB of Mapbox Vector Tiles) produced exactly 1,048,576 bytes of compressed output at ZSTD level 19, deterministically triggering the infinite loop at blob 1736 (a 55-byte tile).

**Reproduction:** Feed tiles from a US OpenStreetMap mbtiles through libzim's `Creator.add_item()` with 2 MiB cluster size, ZSTD compression, 10 workers. The build hangs at tile ~431,760 (accounting for the 10-slot write queue pipeline delay from the actual stuck cluster at tile ~417,090).

**Fix:** In the `CompStatus::OK` handler, check `avail_in > 0` before continuing. If `avail_in == 0` (input consumed) and `avail_out == 0` (buffer full), expand the output buffer (same logic as the `BUF_ERROR` handler) and return `NEED_MORE`. The LZMA-specific `continue` is preserved for the case where `avail_in > 0`.

**File changed:** `compression.h`
