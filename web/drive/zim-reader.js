// StreetZim Drive — in-browser ZIM reader.
//
// Consumed by the service worker (importScripts) to turn a local .zim
// Blob into HTTP responses. Exposes StreetZimReader on the global scope,
// plus StreetZimHttpSource: a Blob look-alike over HTTP range requests
// that lets the same reader stream a ZIM off a web server (the online
// preview of the archive.org-hosted regions — docs/online-preview.md).
//
// Supported: ZIM v6 (new-namespace layout: content under 'C', which is
//            what `read()` defaults to) with uncompressed (flag 1/2) and
//            zstd (flag 5) clusters. Requires fzstd (loaded alongside via
//            importScripts or <script>) for zstd.
// Not yet:   ZIM v5 legacy namespaces ('A'/'-'), xz clusters (throws),
//            full-text search, title index, redirect chains > 1 hop.
//
// Format ref: https://wiki.openzim.org/wiki/ZIM_file_format
// If we hit weird ZIMs in the wild, cross-reference kiwix-js:
//   https://github.com/kiwix/kiwix-js/tree/main/app/js  (zimfile.js,
//   zimArchive.js, zimDirEntry.js). Its reader handles more edge cases
//   than we need here (split archives, titleList search, xz clusters).

(function(global) {
  'use strict';

  const MAGIC = 0x44D495A;
  const EXACT = { exact: true };   // HttpRangeSource.slice() option

  function u8 (v, o) { return v.getUint8(o); }
  function u16(v, o) { return v.getUint16(o, true); }
  function u32(v, o) { return v.getUint32(o, true); }
  function u64(v, o) {
    // Safe up to 2^53 ≈ 9 PB — plenty for any ZIM we'll see.
    return v.getUint32(o, true) + v.getUint32(o + 4, true) * 0x100000000;
  }

  function readCString(view, offset) {
    if (offset >= view.byteLength) {
      // String ran off the end of the buffer we over-read (a dirent
      // with a >8 KB URL/title). Return empty rather than constructing
      // a negative-length view, which throws RangeError.
      return { str: '', nextOffset: offset + 1 };
    }
    const u8a = new Uint8Array(view.buffer, view.byteOffset + offset,
                               view.byteLength - offset);
    let end = 0;
    while (end < u8a.length && u8a[end] !== 0) end++;
    const str = new TextDecoder('utf-8').decode(u8a.subarray(0, end));
    return { str: str, nextOffset: offset + end + 1 };
  }

  // Count-bounded, and optionally byte-bounded when `sizeOf` is given: an
  // entry larger than the whole budget is not kept at all (a one-shot
  // read), and older entries go until the total fits.
  class LRU {
    constructor(limit, maxBytes, sizeOf) {
      this.limit = limit;
      this.maxBytes = maxBytes || 0;
      this.sizeOf = sizeOf || null;
      this.bytes = 0;
      this.map = new Map();
    }
    get(k) {
      if (!this.map.has(k)) return undefined;
      const v = this.map.get(k);
      this.map.delete(k); this.map.set(k, v);
      return v;
    }
    _drop(k) {
      if (this.sizeOf) this.bytes -= this.sizeOf(this.map.get(k));
      this.map.delete(k);
    }
    set(k, v) {
      const size = this.sizeOf ? this.sizeOf(v) : 0;
      if (this.map.has(k)) this._drop(k);
      if (this.maxBytes && size > this.maxBytes) return;
      this.map.set(k, v);
      this.bytes += size;
      while (this.map.size > this.limit || (this.maxBytes && this.bytes > this.maxBytes)) {
        this._drop(this.map.keys().next().value);
      }
    }
  }

  class ZimReader {
    constructor(file) {
      if (!file || typeof file.slice !== 'function') {
        throw new Error('ZimReader: expected a File or Blob');
      }
      this.file = file;
      this.size = file.size;
      this.header = null;
      this.mimeList = null;
      // Byte budgets keep a phone's service worker alive. A cluster
      // usually decompresses to ~2 MiB, but a Wikidata bucket is a
      // 30–45 MB blob in a cluster of its own and a raw routing cluster
      // can be 100 MB+; an east-coast-us browse with one search held
      // 52 MB of clusters under the old count-only bound, and iOS or a
      // low-RAM Android kills a worker that grows past a few hundred MB.
      // Halved where the device reports ≤ 2 GB (Chrome exposes
      // deviceMemory to workers; WebKit does not and gets the full
      // budget). See docs/mobile-browser-review.md.
      const lowMem = (typeof navigator !== 'undefined' && navigator &&
                      navigator.deviceMemory && navigator.deviceMemory <= 2);
      const MB = 1048576;
      this.clusterCache = new LRU(8, (lowMem ? 32 : 64) * MB,   // clusterNum → {data, extended}
                                  (c) => c.data.byteLength);
      this.blobCache = new LRU(512, (lowMem ? 16 : 32) * MB,    // "c:b" → Uint8Array
                               (u) => u.byteLength);
      this.entryCache = new LRU(1024);      // "ns/url"   → {mime, cluster, blob}
      this.urlPtrPageCache = new LRU(256);  // page index → Uint8Array
      this.rawClusterMeta = new LRU(64);    // clusterNum → {start, extended, table} | false (remote sources)
      this.direntByIndex = new LRU(4096);   // entry index → parsed dirent (remote sources)
    }

    // `exact` (remote sources only): fetch just these bytes instead of the
    // surrounding cache blocks — for probes far from anything else we
    // will want.
    async _readRange(offset, length, exact) {
      if (offset < 0 || offset + length > this.size) {
        throw new Error(`ZimReader: out-of-range read (${offset}+${length} of ${this.size})`);
      }
      const part = (exact && this.file.remote)
        ? this.file.slice(offset, offset + length, EXACT)
        : this.file.slice(offset, offset + length);
      return new Uint8Array(await part.arrayBuffer());
    }

    async open() {
      const buf = await this._readRange(0, 80);
      const v = new DataView(buf.buffer);
      const magic = u32(v, 0);
      if (magic !== MAGIC) {
        throw new Error('Not a ZIM file (magic=0x' + magic.toString(16) + ')');
      }
      this.header = {
        majorVersion:   u16(v, 4),
        minorVersion:   u16(v, 6),
        articleCount:   u32(v, 24),
        clusterCount:   u32(v, 28),
        urlPtrPos:      u64(v, 32),
        titlePtrPos:    u64(v, 40),
        clusterPtrPos:  u64(v, 48),
        mimeListPos:    u64(v, 56),
        mainPage:       u32(v, 64),
        layoutPage:     u32(v, 68),
        checksumPos:    u64(v, 72)
      };
      await this._loadMimeList();
      return this.header;
    }

    async _loadMimeList() {
      const len = Math.max(0, this.header.urlPtrPos - this.header.mimeListPos);
      const buf = await this._readRange(this.header.mimeListPos, Math.min(len, 65536));
      const view = new DataView(buf.buffer);
      const list = [];
      let off = 0;
      while (off < buf.length) {
        const r = readCString(view, off);
        if (r.str === '') break;
        list.push(r.str);
        off = r.nextOffset;
      }
      this.mimeList = list;
    }

    async _readUrlPointer(idx) {
      const pageSize = 4096; // pointers; 32 KiB per page
      const page = Math.floor(idx / pageSize);
      const inPage = idx - page * pageSize;
      let buf = this.urlPtrPageCache.get(page);
      if (buf === undefined) {
        const first = page * pageSize;
        const count = Math.min(pageSize, this.header.articleCount - first);
        buf = await this._readRange(this.header.urlPtrPos + first * 8, count * 8);
        this.urlPtrPageCache.set(page, buf);
      }
      return u64(new DataView(buf.buffer, buf.byteOffset, buf.byteLength), inPage * 8);
    }

    // Smallest boundary strictly greater than `start`. Candidates are the
    // other cluster offsets AND the structures the writer puts after the
    // cluster data — the URL pointer list, title index, cluster pointer
    // list and MIME list. The array is built and sorted once —
    // clusterCount * 8 bytes, tens of KB for a regional ZIM — and reused
    // for every later cluster read.
    //
    // The LAST cluster by file offset has no cluster after it, and falling
    // back to `checksumPos` handed fzstd the cluster plus everything
    // between it and the checksum: 215 MB of index structures on
    // east-coast-us, which the one-shot decoder rejects with "invalid
    // zstd data". libzim never noticed because it decompresses streaming
    // and reads the cluster's own blob table, never needing an end.
    // Every ZIM has this over-read (korea-mongolia 236 MB, washington-dc
    // 675 KB); it only breaks a page when something it fetches lands in
    // that last cluster, as category-index/manifest.json did.
    async _clusterEnd(start) {
      if (!this._sortedClusterOffsets) {
        const n = this.header.clusterCount;
        const buf = await this._readRange(this.header.clusterPtrPos, n * 8);
        const v = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
        const offs = new Array(n);
        for (let i = 0; i < n; i++) offs[i] = u64(v, i * 8);
        for (const after of [this.header.urlPtrPos, this.header.titlePtrPos,
                             this.header.clusterPtrPos, this.header.mimeListPos]) {
          if (after) offs.push(after);
        }
        offs.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
        this._sortedClusterOffsets = offs;
      }
      const offs = this._sortedClusterOffsets;
      let lo = 0, hi = offs.length;
      while (lo < hi) {                      // first boundary > start
        const mid = (lo + hi) >> 1;
        if (offs[mid] > start) hi = mid; else lo = mid + 1;
      }
      return lo < offs.length ? offs[lo] : this.header.checksumPos;
    }

    async _readClusterPointer(idx) {
      const buf = await this._readRange(this.header.clusterPtrPos + idx * 8, 8);
      return u64(new DataView(buf.buffer), 0);
    }

    async _readDirEntryAt(offset, exact) {
      // Over-read — DirEntries are typically < 512 B including URL + title.
      // If a string ran past our buffer we re-fetch larger.
      const initial = Math.min(1024, this.size - offset);
      let buf = await this._readRange(offset, initial, exact);
      let v = new DataView(buf.buffer);
      const mimeType = u16(v, 0);
      const isRedirect = mimeType === 0xFFFF;
      const isSpecial  = mimeType >= 0xFFFD;
      const namespace = String.fromCharCode(u8(v, 3));
      let headEnd = 8;
      const extra = {};
      if (isRedirect) {
        extra.redirectIndex = u32(v, 8);
        headEnd = 12;
      } else if (!isSpecial) {
        extra.clusterNumber = u32(v, 8);
        extra.blobNumber    = u32(v, 12);
        headEnd = 16;
      }
      let r = readCString(v, headEnd);
      let url = r.str;
      let nextOff = r.nextOffset;
      if (nextOff >= buf.length && buf.length < this.size - offset) {
        buf = await this._readRange(offset, Math.min(8192, this.size - offset), exact);
        v = new DataView(buf.buffer);
        r = readCString(v, headEnd);
        url = r.str; nextOff = r.nextOffset;
      }
      const tr = readCString(v, nextOff);
      return Object.assign({
        mimeType, namespace, url, title: tr.str,
        isRedirect, isSpecial
      }, extra);
    }

    // Search the URL pointer list for an entry whose (ns, url) matches.
    // ZIM sorts by (ns, url) ascending.
    async findEntry(path, namespace) {
      namespace = namespace || 'C';
      const cacheKey = namespace + '/' + path;
      const cached = this.entryCache.get(cacheKey);
      if (cached !== undefined) return cached;

      let lo = 0, hi = this.header.articleCount - 1;
      // A binary search is strictly sequential — one dependent read per
      // level, ~25 levels on a 39 M-entry continent ZIM — and on a remote
      // source every level is a network round trip. There, probe FANOUT
      // evenly spaced entries per round in parallel: log(FANOUT+1) rounds.
      // Measured on Europe (62 GiB) in Chrome against archive.org, where
      // the viewer starts ~20 lookups at once: fanout 1 and 3 reach the
      // map in the same time (15 s here), 3 moves half the bytes (7 vs
      // 15 MB) and shortens an isolated lookup's chain by a third; 6 and
      // 16 only add requests (21 s, 33 s). Local Blobs keep the plain
      // binary search: reads are cheap and it touches fewer bytes.
      const fanout = this.file.remote ? (this.file.fanout || 3) : 1;
      while (lo <= hi) {
        if (fanout > 1 && hi - lo >= 2 * fanout) {
          const step = (hi - lo + 1) / (fanout + 1);
          const idxs = [];
          for (let i = 1; i <= fanout; i++) idxs.push(lo + Math.floor(step * i));
          const des = await Promise.all(idxs.map((i) => this._direntAtIndex(i, sparseFor(step))));
          let found = null;
          for (let j = 0; j < idxs.length; j++) {
            const cmp = cmpNsUrl(des[j].namespace, des[j].url, namespace, path);
            if (cmp === 0) { found = des[j]; break; }
            if (cmp < 0) lo = idxs[j] + 1;       // probe sorts before the target
            else { hi = idxs[j] - 1; break; }    // first probe after it bounds hi
          }
          if (found) return this._resolveEntry(cacheKey, found);
          continue;
        }
        const mid = (lo + hi) >>> 1;
        const de = await this._direntAtIndex(mid, sparseFor((hi - lo) / 2));
        const cmp = cmpNsUrl(de.namespace, de.url, namespace, path);
        if (cmp === 0) return this._resolveEntry(cacheKey, de);
        if (cmp < 0) lo = mid + 1;
        else         hi = mid - 1;
      }
      this.entryCache.set(cacheKey, null);
      return null;
    }

    // The dirent at URL-pointer index `idx`. Remote sources memoise the
    // parsed result by index: the top rounds of every lookup probe the
    // same indices, so after the first lookup they cost nothing.
    async _direntAtIndex(idx, sparse) {
      const hit = this.direntByIndex.get(idx);
      if (hit) return hit;
      let off;
      if (sparse && sparse.ptr) {
        const b = await this._readRange(this.header.urlPtrPos + idx * 8, 8, true);
        off = u64(new DataView(b.buffer, b.byteOffset, b.byteLength), 0);
      } else {
        off = await this._readUrlPointer(idx);
      }
      const de = await this._readDirEntryAt(off, !!(sparse && sparse.dirent));
      if (this.file.remote) this.direntByIndex.set(idx, de);
      return de;
    }

    // Follow one redirect hop and memoise the (mime, cluster, blob) triple.
    async _resolveEntry(cacheKey, de) {
      let resolved = de;
      if (de.isRedirect) {
        const targetOff = await this._readUrlPointer(de.redirectIndex);
        resolved = await this._readDirEntryAt(targetOff);
        if (resolved.isRedirect || resolved.isSpecial) {
          this.entryCache.set(cacheKey, null);
          return null;
        }
      }
      if (resolved.isSpecial) {
        this.entryCache.set(cacheKey, null);
        return null;
      }
      const info = {
        mime: this.mimeList[resolved.mimeType] || 'application/octet-stream',
        cluster: resolved.clusterNumber,
        blob: resolved.blobNumber,
        namespace: resolved.namespace,
        url: resolved.url
      };
      this.entryCache.set(cacheKey, info);
      return info;
    }

    async _loadCluster(clusterNum) {
      const cached = this.clusterCache.get(clusterNum);
      if (cached) return cached;

      const start = await this._readClusterPointer(clusterNum);
      // A cluster's length is "up to wherever the next cluster STARTS IN
      // THE FILE" — which is not necessarily clusterPtr[n+1]. The ZIM
      // format does not require clusters to be laid out in pointer order,
      // and a writer that emits them concurrently does not: a zimru build
      // of California had 2182 of 5182 transitions going backwards, so
      // `clusterPtr[n+1] - clusterPtr[n]` came out as -2215017 and fzstd
      // was handed garbage ("invalid zstd data"). libzim never noticed
      // because it decompresses streaming from the start and reads the
      // cluster's own blob-offset table, never needing an end offset.
      // Take the next-highest offset instead of the next index.
      const end = await this._clusterEnd(start);
      const raw = await this._readRange(start, end - start);
      const info = raw[0];
      const compression = info & 0x0F;
      const extended = (info & 0x10) !== 0;
      let payload;
      if (compression === 1 || compression === 2) {
        payload = raw.subarray(1);
      } else if (compression === 5) {
        if (!global.fzstd || typeof global.fzstd.decompress !== 'function') {
          throw new Error('zstd decoder (fzstd) not loaded');
        }
        payload = global.fzstd.decompress(raw.subarray(1));
      } else if (compression === 4) {
        throw new Error('xz-compressed ZIMs are not supported');
      } else {
        throw new Error('Unknown cluster compression: ' + compression);
      }
      const cluster = { data: payload, extended };
      this.clusterCache.set(clusterNum, cluster);
      return cluster;
    }

    async _readBlob(clusterNum, blobNum) {
      const key = clusterNum + ':' + blobNum;
      const cached = this.blobCache.get(key);
      if (cached) return cached;

      // Remote source + raw cluster: fetch the blob's own bytes instead of
      // the whole cluster (null → compressed, take the normal path).
      let out = (this.file.remote && !this.clusterCache.get(clusterNum))
        ? await this._readRawBlobRemote(clusterNum, blobNum)
        : null;
      if (!out) {
        const cluster = await this._loadCluster(clusterNum);
        const { data, extended } = cluster;
        const v = new DataView(data.buffer, data.byteOffset, data.byteLength);
        const wordSize = extended ? 8 : 4;
        const readOff = extended
          ? (o) => v.getUint32(o, true) + v.getUint32(o + 4, true) * 0x100000000
          : (o) => v.getUint32(o, true);
        const firstOffset = readOff(0);
        const numBlobs = (firstOffset / wordSize) - 1;
        if (blobNum < 0 || blobNum >= numBlobs) {
          throw new Error('blobNumber ' + blobNum + ' out of range (' + numBlobs + ')');
        }
        const start = readOff(blobNum * wordSize);
        const stop  = readOff((blobNum + 1) * wordSize);
        // Copy, don't subarray: a subarray pins the whole decompressed
        // cluster buffer for as long as the blob sits in blobCache, so
        // 512 cached tiles could keep 512 evicted clusters (2 MB each,
        // far more for search/routing clusters) alive inside the SW.
        out = data.slice(start, stop);
      }
      // Only memoise small blobs; big search/routing payloads are
      // one-shot reads and would blow the count-based LRU's budget.
      if (out.byteLength <= 512 * 1024) this.blobCache.set(key, out);
      return out;
    }

    // Raw (uncompressed) cluster on a remote source: a whole-cluster read
    // would pull up to 2 MiB — or a 100 MB routing chunk's neighbours —
    // over the network for one blob. Read the info byte, then the blob
    // offset table, then just the blob. Returns null for compressed
    // clusters, whose zstd frame has to be decoded from the start anyway.
    async _readRawBlobRemote(clusterNum, blobNum) {
      let meta = this.rawClusterMeta.get(clusterNum);
      if (meta === undefined) {
        const start = await this._readClusterPointer(clusterNum);
        const head = await this._readRange(start, Math.min(9, this.size - start));
        const compression = head[0] & 0x0F;
        if (compression !== 1 && compression !== 2) {
          meta = false;
        } else {
          const extended = (head[0] & 0x10) !== 0;
          const hv = new DataView(head.buffer, head.byteOffset, head.byteLength);
          const firstOffset = extended
            ? hv.getUint32(1, true) + hv.getUint32(5, true) * 0x100000000
            : hv.getUint32(1, true);
          // The offset table is the first `firstOffset` bytes of the
          // cluster payload: (numBlobs + 1) little-endian words.
          const table = await this._readRange(start + 1, firstOffset);
          meta = { start, extended, table };
        }
        this.rawClusterMeta.set(clusterNum, meta);
      }
      if (!meta) return null;
      const { start, extended, table } = meta;
      const wordSize = extended ? 8 : 4;
      const tv = new DataView(table.buffer, table.byteOffset, table.byteLength);
      const readOff = extended
        ? (o) => tv.getUint32(o, true) + tv.getUint32(o + 4, true) * 0x100000000
        : (o) => tv.getUint32(o, true);
      const numBlobs = (table.byteLength / wordSize) - 1;
      if (blobNum < 0 || blobNum >= numBlobs) {
        throw new Error('blobNumber ' + blobNum + ' out of range (' + numBlobs + ')');
      }
      const bStart = readOff(blobNum * wordSize);
      const bStop  = readOff((blobNum + 1) * wordSize);
      return this._readRange(start + 1 + bStart, bStop - bStart);
    }

    // Main entry point — look up a content path. Returns null if not found.
    async read(path, namespace) {
      // Normalize: strip leading slash / "./". Percent-decoding is the
      // caller's job (sw.js decodes exactly once) — decoding here as
      // well made any entry whose real path contains a literal %XX
      // (e.g. a Wikipedia title with "%20" in it) unreachable.
      path = String(path || '').replace(/^\.?\//, '');
      const entry = await this.findEntry(path, namespace);
      if (!entry) return null;
      const data = await this._readBlob(entry.cluster, entry.blob);
      return { mime: entry.mime, data: data, url: entry.url };
    }

    // What the caches hold right now. The SW's status message surfaces
    // it so a phone's memory can be reasoned about with numbers rather
    // than guesses (docs/mobile-browser-review.md).
    get cacheStats() {
      const tally = (lru, size) => {
        let n = 0, bytes = 0;
        for (const v of lru.map.values()) { n++; bytes += size(v); }
        return { n, bytes };
      };
      const clusters = tally(this.clusterCache, (c) => c.data.byteLength);
      const blobs = tally(this.blobCache, (u) => u.byteLength);
      const blocks = (this.file && this.file.blocks)
        ? tally(this.file.blocks, (u) => u.byteLength) : { n: 0, bytes: 0 };
      return { clusters, blobs, blocks, totalBytes: clusters.bytes + blobs.bytes + blocks.bytes };
    }

    get info() {
      if (!this.header) return null;
      return {
        version: this.header.majorVersion + '.' + this.header.minorVersion,
        articles: this.header.articleCount,
        clusters: this.header.clusterCount,
        sizeMB: (this.size / (1024 * 1024)).toFixed(1)
      };
    }
  }

  // ZIM sorts dirents by their UTF-8 bytes, which is code-point order.
  // JS `<` on strings compares UTF-16 code units, which disagrees for
  // any character above U+FFFF (CJK Extension B, emoji) versus one in
  // U+E000–U+FFFF — enough to misdirect the binary search.
  function cmpCodePoints(a, b) {
    const la = a.length, lb = b.length;
    let i = 0, j = 0;
    while (i < la && j < lb) {
      const ca = a.codePointAt(i);
      const cb = b.codePointAt(j);
      if (ca !== cb) return ca < cb ? -1 : 1;
      i += ca > 0xFFFF ? 2 : 1;
      j += cb > 0xFFFF ? 2 : 1;
    }
    if (i < la) return 1;
    if (j < lb) return -1;
    return 0;
  }

  // Which reads of a lookup probe `step` entries from its neighbours
  // should skip the block cache: further apart than a cache block, a
  // probe reads just its own bytes (nothing near it will be wanted);
  // once probes crowd together, block reads pay off because the final
  // steps and neighbouring lookups land on the same blocks. 8 B per
  // pointer; dirents average well under 128 B.
  function sparseFor(step) {
    return { ptr: step > 16384, dirent: step > 1024 };
  }

  function cmpNsUrl(aNs, aUrl, bNs, bUrl) {
    if (aNs !== bNs) return aNs < bNs ? -1 : 1;
    if (aUrl === bUrl) return 0;
    return cmpCodePoints(aUrl, bUrl);
  }

  // ---------- HTTP range source ----------
  //
  // Duck-types the two Blob members ZimReader uses — `size` and
  // `slice(start, end).arrayBuffer()` — on top of HTTP byte-range
  // requests, so the reader above streams a ZIM straight off a web
  // server without knowing the difference. This is what the online
  // preview of the archive.org-hosted regions runs on: the picker hands
  // the service worker a URL instead of a File (docs/online-preview.md).
  //
  // Small reads are served from fixed, aligned blocks kept in an LRU:
  // the dirent binary search revisits the same few blocks for every
  // lookup, and an aligned range is byte-identical from one request to
  // the next, so the browser HTTP cache can answer repeats after the
  // service worker (and this cache with it) has been torn down. Reads of
  // a block or more — whole clusters, routing chunks — fetch their exact
  // range and are not cached here.
  //
  // The server must answer `Range: bytes=a-b` with a 206 (a 200 means it
  // is about to stream the whole file, which is refused before the body
  // is read) and, cross-origin, send CORS headers exposing Content-Range.
  class HttpRangeSource {
    constructor(url, opts) {
      opts = opts || {};
      if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) {
        throw new Error('HttpRangeSource: expected an http(s) URL');
      }
      this.url = url;
      this.remote = true;                 // ZimReader reads raw clusters piecemeal
      this.size = 0;                      // known after open()
      this.blockSize = opts.blockSize || 128 * 1024;
      this.fanout = opts.fanout || 0;     // lookup probes per round (0 = reader default)
      this.blocks = new LRU(opts.maxBlocks || 256);   // block index → Uint8Array
      this.inflight = new Map();          // block index → Promise<Uint8Array>
      this.stats = { requests: 0, bytes: 0 };
      this.etag = null;
      this.lastModified = null;
    }

    // Fetch block 0 (the header and, for every ZIM we ship, the whole
    // MIME list live there) and learn the file size from Content-Range.
    async open() {
      const res = await this._fetch(0, this.blockSize - 1);
      const cr = parseContentRange(res.headers.get('Content-Range'));
      this.etag = res.headers.get('ETag');
      this.lastModified = res.headers.get('Last-Modified');
      const buf = new Uint8Array(await res.arrayBuffer());
      this.size = (cr && cr.total > 0) ? cr.total : await this._sizeFromHead();
      if (!(this.size > 0)) {
        throw new Error('HttpRangeSource: could not determine the file size ' +
          '(no Content-Range or Content-Length exposed by ' + this.url + ')');
      }
      if (buf.byteLength !== Math.min(this.blockSize, this.size)) {
        throw new Error('HttpRangeSource: short read opening ' + this.url +
          ' (' + buf.byteLength + ' of ' + Math.min(this.blockSize, this.size) + ')');
      }
      this.blocks.set(0, buf);
      return this;
    }

    // Blob.prototype.slice look-alike; only .arrayBuffer() is provided.
    // `opts.exact` fetches just those bytes, bypassing the block cache.
    slice(start, end, opts) {
      const self = this;
      const exact = !!(opts && opts.exact);
      return { arrayBuffer: function() { return self._read(start, end - start, exact); } };
    }

    async _read(offset, length, exact) {
      if (length <= 0) return new ArrayBuffer(0);
      if (offset < 0 || offset + length > this.size) {
        throw new Error('HttpRangeSource: out-of-range read (' + offset + '+' +
          length + ' of ' + this.size + ')');
      }
      const bs = this.blockSize;
      if (exact || length >= bs) return this._exact(offset, length);
      const first = Math.floor(offset / bs);
      const last = Math.floor((offset + length - 1) / bs);
      const pending = [];
      for (let i = first; i <= last; i++) pending.push(this._block(i));
      const blocks = await Promise.all(pending);
      if (first === last) {
        const o = offset - first * bs;
        return blocks[0].slice(o, o + length).buffer;
      }
      const out = new Uint8Array(length);
      let pos = 0;
      for (let i = first; i <= last; i++) {
        const b = blocks[i - first];
        const from = (i === first) ? offset - first * bs : 0;
        const to = (i === last) ? (offset + length) - last * bs : b.byteLength;
        out.set(b.subarray(from, to), pos);
        pos += to - from;
      }
      return out.buffer;
    }

    // One uncached range, shared with any identical read in flight.
    _exact(offset, length) {
      const key = offset + ':' + length;
      const running = this.inflight.get(key);
      if (running) return running;
      const p = this._fetch(offset, offset + length - 1)
        .then((res) => res.arrayBuffer())
        .then((buf) => {
          if (buf.byteLength !== length) {
            throw new Error('HttpRangeSource: short read (' + buf.byteLength +
              ' of ' + length + ')');
          }
          return buf;
        })
        .finally(() => { if (this.inflight.get(key) === p) this.inflight.delete(key); });
      this.inflight.set(key, p);
      return p;
    }

    _block(i) {
      const hit = this.blocks.get(i);
      if (hit) return Promise.resolve(hit);
      const running = this.inflight.get(i);
      if (running) return running;
      const start = i * this.blockSize;
      const end = Math.min(start + this.blockSize, this.size) - 1;
      const p = this._fetch(start, end)
        .then((res) => res.arrayBuffer())
        .then((buf) => {
          const u8 = new Uint8Array(buf);
          if (u8.byteLength !== end - start + 1) {
            throw new Error('HttpRangeSource: short block ' + i + ' (' +
              u8.byteLength + ' of ' + (end - start + 1) + ')');
          }
          this.blocks.set(i, u8);
          return u8;
        })
        .finally(() => { if (this.inflight.get(i) === p) this.inflight.delete(i); });
      this.inflight.set(i, p);
      return p;
    }

    // One ranged GET, retried on network errors and 5xx/429. Resolves
    // with a verified 206 whose body has not been read yet.
    async _fetch(start, end) {
      const want = end - start + 1;
      let lastErr = null;
      for (let attempt = 0; attempt < 4; attempt++) {
        if (attempt) await new Promise((r) => setTimeout(r, 400 * attempt));
        let res;
        try {
          // The range goes in the header (what any file server needs)
          // and, for the preview proxy, in the query string as well: a
          // CDN in front of the proxy keys its cache on the URL, and
          // Firebase Hosting is not documented to forward Range to a
          // function. Static servers ignore the query.
          const sep = this.url.indexOf('?') < 0 ? '?' : '&';
          res = await fetch(this.url + sep + 'bytes=' + start + '-' + end, {
            headers: { Range: 'bytes=' + start + '-' + end },
            credentials: 'omit'
          });
        } catch (err) {
          lastErr = err;                  // network error, CORS failure
          continue;
        }
        this.stats.requests++;
        if (res.status === 206) {
          const cr = parseContentRange(res.headers.get('Content-Range'));
          const cl = parseInt(res.headers.get('Content-Length') || '', 10);
          // A range past EOF is legitimately answered short, with
          // Content-Range telling us so (open() asks for a whole block
          // before it knows the size).
          const crOk = !!cr && cr.start === start &&
            (cr.end === end || (cr.total > 0 && cr.end === cr.total - 1 && end > cr.end));
          if (crOk || (!cr && cl === want)) {
            this.stats.bytes += cr ? cr.end - cr.start + 1 : want;
            return res;
          }
          discardBody(res);
          throw new Error('HttpRangeSource: asked for bytes=' + start + '-' + end +
            ' but got ' + (cr ? 'bytes ' + cr.start + '-' + cr.end : 'no usable Content-Range') +
            ' (Content-Length ' + cl + ')');
        }
        discardBody(res);
        if (res.status === 200) {
          throw new Error('HttpRangeSource: server ignores Range requests ' +
            '(200 for bytes=' + start + '-' + end + ') — refusing to stream the whole file');
        }
        if (res.status >= 500 || res.status === 429) {
          lastErr = new Error('HTTP ' + res.status + ' from ' + this.url);
          continue;
        }
        throw new Error('HTTP ' + res.status + ' from ' + this.url);
      }
      throw lastErr || new Error('HttpRangeSource: fetch failed');
    }

    // Fallback when the 206 carried no readable Content-Range (a
    // cross-origin server not exposing it): Content-Length on a HEAD is
    // always readable.
    async _sizeFromHead() {
      try {
        const res = await fetch(this.url, { method: 'HEAD', credentials: 'omit' });
        this.stats.requests++;
        const cl = parseInt(res.headers.get('Content-Length') || '', 10);
        return (res.ok && cl > 0) ? cl : 0;
      } catch (e) {
        return 0;
      }
    }
  }

  function discardBody(res) {
    try {
      if (res.body && typeof res.body.cancel === 'function') res.body.cancel();
    } catch (e) {}
  }

  // "bytes 0-131071/226265704" → {start, end, total} (total -1 for "*").
  function parseContentRange(h) {
    const m = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i.exec(String(h || '').trim());
    if (!m) return null;
    return {
      start: parseInt(m[1], 10),
      end: parseInt(m[2], 10),
      total: m[3] === '*' ? -1 : parseInt(m[3], 10)
    };
  }

  global.StreetZimReader = ZimReader;
  global.StreetZimHttpSource = HttpRangeSource;
})(typeof self !== 'undefined' ? self : globalThis);
