# libzim macOS read() size limit causes "Cannot read chars" on large ZIM files

## Summary

ZIM files with more than ~268 million entries fail to open on macOS with:
```
Cannot read chars.
 - Reading offset at 120075518264
 - size is 3049426704
 - error is Cannot read file: Invalid argument
```

## Root Cause

The URL pointer table in the ZIM header is `entry_count * 8` bytes. When entry count exceeds ~268M, this table exceeds 2 GB. libzim attempts to read the entire table in a single `read()` system call. On macOS (and some other platforms), `read()` returns `EINVAL` for sizes exceeding `INT32_MAX` (2,147,483,647 bytes).

**Reproduction:**
- ZIM with 381,178,338 entries
- URL pointer table: 381,178,338 × 8 = 3,049,426,704 bytes (2.84 GB)
- `read()` call with size 3,049,426,704 → `EINVAL` on macOS

The title pointer table has the same issue at ~536M entries (entry_count × 4).

## Affected Code

The issue is in `src/file_reader.cpp` (or equivalent), where bulk reads don't chunk across the 2 GB boundary. The relevant code path is:

```
zim::Archive constructor
  → reads URL pointer table
    → file_reader.read(offset=120075518264, size=3049426704)
      → ::read() syscall → EINVAL
```

## Affected Platforms

- macOS: `read()` limited to INT32_MAX bytes per call
- Some Linux configurations may have similar limits
- Windows: `ReadFile()` limited to DWORD (4 GB) but typically works for 2-4 GB

## Proposed Fix

In the file reader's `read()` method, chunk reads larger than 2 GB:

```cpp
// In file_reader.cpp - wherever the raw read() is called
size_t total_read = 0;
while (total_read < size) {
    // Chunk to 1 GB max per read() call (well under any platform limit)
    size_t chunk = std::min(size - total_read, (size_t)(1ULL << 30));
    ssize_t n = ::read(fd, buffer + total_read, chunk);
    if (n <= 0) {
        // handle error
    }
    total_read += n;
}
```

This is a minimal, backwards-compatible fix. No format changes needed.

## Verification

Test file: 381M-entry ZIM (117 GB world OpenStreetMap with vector tiles, terrain, and search index)

```bash
# Reproduce
./test_open osm-world.zim
# Expected: "Cannot read chars" exception
# After fix: "ALL OK - file should open in Kiwix"
```

## Workarounds

Until libzim is patched:
1. Keep entry count below 268M (URL table under 2 GB)
2. Reduce search/Xapian page entries (the largest contributor to entry count)
3. Bundle search data into fewer, larger entries instead of one per page

## Environment

- libzim 9.5.0 (bundled in Kiwix macOS 3.13.0)
- macOS 26.3 (Darwin 25.3.0), Apple Silicon
- ZIM format version 6.3
