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

---

## Build & Installation Notes

### Architecture

Two repos are involved:

1. **`/Users/jasontitus/experiments/python-libzim`** — OpenZIM Python bindings (Cython wrapper + bundled `libzim.9.dylib`)
2. **`/Users/jasontitus/experiments/streetzim/patches/`** — Our patches for the C++ libzim library

### How the Python libzim Package Works

The `python-libzim` package bundles a pre-built `libzim.9.dylib` (from openzim.org) alongside
a Cython wrapper (`libzim.cpython-312-darwin.so`). The wrapper dynamically links to the dylib.

**Critical: dylib linkage.** The wrapper's load path must use `@loader_path/libzim/libzim.9.dylib`
(relative to the .so file). If it instead shows an absolute path like `/tmp/...` or `/opt/homebrew/...`,
the library will fail to load when that path is cleaned up. Check with:

```bash
otool -L venv312/lib/python3.12/site-packages/libzim.cpython-312-darwin.so
```

Fix a broken path with:

```bash
install_name_tool -change \
  /tmp/libzim-install/opt/homebrew/lib/libzim.9.dylib \
  @loader_path/libzim/libzim.9.dylib \
  venv312/lib/python3.12/site-packages/libzim.cpython-312-darwin.so
```

Replace the first argument with whatever `otool -L` shows as the broken path.

Verify:

```bash
source venv312/bin/activate
python3 -c "from libzim.writer import Creator; print('libzim loaded OK')"
```

### Rebuilding the Python Package from Scratch

```bash
cd /Users/jasontitus/experiments/python-libzim
source /Users/jasontitus/experiments/streetzim/venv312/bin/activate

# Download the pre-built libzim binary (stock, unpatched)
LIBZIM_DL_VERSION=9.4.0-1 python setup.py download_libzim

# Build the Cython wrapper
python setup.py build_ext --inplace

# Install into streetzim venv
pip install -e /Users/jasontitus/experiments/python-libzim

# Fix linkage if needed (check with otool -L first)
install_name_tool -change \
  <OLD_PATH>/libzim.9.dylib \
  @loader_path/libzim/libzim.9.dylib \
  venv312/lib/python3.12/site-packages/libzim.cpython-312-darwin.so
```

### Building Patched libzim from C++ Source

To apply our spin-lock and compressor patches to the C++ libzim itself:

```bash
git clone https://github.com/openzim/libzim.git
cd libzim
git checkout v9.4.0

# Apply patches
git apply /Users/jasontitus/experiments/streetzim/patches/0001-Replace-spin-loop-polling-with-condition-variables-t.patch
git apply /Users/jasontitus/experiments/streetzim/patches/0001-Fix-infinite-loop-in-Compressor-feed-when-output-buf.patch

# Build with meson (install into project dir, NOT /tmp)
meson setup build --prefix=/Users/jasontitus/experiments/streetzim/libzim-install
ninja -C build
ninja -C build install

# Replace the dylib in python-libzim
cp /Users/jasontitus/experiments/streetzim/libzim-install/lib/libzim.9.dylib \
   /Users/jasontitus/experiments/python-libzim/libzim/

# Then reinstall python-libzim into the venv (see above)
```

### Verifying the Patched Library Is Installed

**CRITICAL: Always verify before starting a build.** The stock libzim from openzim.org does NOT have our patches and will hang at tile ~431,760 on US builds.

```bash
# 1. Check for condition_variable symbols (proves spin-lock patch is applied)
nm venv312/lib/python3.12/site-packages/libzim/libzim.9.dylib | grep condition_variable
# Should show 3+ symbols. Stock libzim has ZERO.

# 2. Check file size — patched local build is ~789K, stock download is ~9.7M
ls -lh venv312/lib/python3.12/site-packages/libzim/libzim.9.dylib

# 3. Check MD5 matches our installed copy
md5 venv312/lib/python3.12/site-packages/libzim/libzim.9.dylib
md5 libzim-install/lib/libzim.9.dylib
# Must match.

# 4. Run a ZIM creation test
source venv312/bin/activate
python3 -c "
from libzim.writer import Creator, Item, StringProvider, Hint
import tempfile, os
class T(Item):
    def __init__(s,p,c): super().__init__(); s._p=p; s._c=c
    def get_path(s): return s._p
    def get_title(s): return s._p
    def get_mimetype(s): return 'text/plain'
    def get_contentprovider(s): return StringProvider(s._c)
    def get_hints(s): return {Hint.FRONT_ARTICLE:False, Hint.COMPRESS:True}
f=tempfile.mktemp(suffix='.zim')
with Creator(f).config_indexing(True,'en') as c:
    for i in range(500): c.add_item(T(f'i/{i}','x'*1000))
print(f'OK: {os.path.getsize(f)} bytes')
os.unlink(f)
"
```

### Key Locations

| File | Purpose |
|------|---------|
| `libzim-install/lib/libzim.9.dylib` | Patched build output (permanent, NOT in /tmp) |
| `venv312/lib/python3.12/site-packages/libzim/libzim.9.dylib` | Active copy used by Python |
| `/Users/jasontitus/experiments/libzim/` | C++ source with patches applied |
| `/Users/jasontitus/experiments/python-libzim/` | Python bindings source |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `LIBZIM_DL_VERSION` | Which libzim version to download (default: `9.4.0-1`) |
| `USE_SYSTEM_LIBZIM` | Use system-installed libzim instead of bundled |
| `DONT_DOWNLOAD_LIBZIM` | Skip download, use existing libzim in python-libzim/libzim/ |
| `ZSTD_CLEVEL` | ZSTD compression level used by libzim at runtime (we use 22) |
