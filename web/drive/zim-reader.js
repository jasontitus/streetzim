// StreetZim Drive — in-browser ZIM reader.
//
// Consumed by the service worker (importScripts) to turn a local .zim
// Blob into HTTP responses. Exposes StreetZimReader on the global scope.
//
// Supported: ZIM v5/v6 with uncompressed (flag 1/2) and zstd (flag 5)
//            clusters. Requires fzstd (loaded alongside via importScripts
//            or <script>) for zstd.
// Not yet:   xz clusters (throws), full-text search, title index,
//            redirect chains > 1 hop.
//
// Format ref: https://wiki.openzim.org/wiki/ZIM_file_format
// If we hit weird ZIMs in the wild, cross-reference kiwix-js:
//   https://github.com/kiwix/kiwix-js/tree/main/app/js  (zimfile.js,
//   zimArchive.js, zimDirEntry.js). Its reader handles more edge cases
//   than we need here (split archives, titleList search, xz clusters).

(function(global) {
  'use strict';

  const MAGIC = 0x44D495A;

  function u8 (v, o) { return v.getUint8(o); }
  function u16(v, o) { return v.getUint16(o, true); }
  function u32(v, o) { return v.getUint32(o, true); }
  function u64(v, o) {
    // Safe up to 2^53 ≈ 9 PB — plenty for any ZIM we'll see.
    return v.getUint32(o, true) + v.getUint32(o + 4, true) * 0x100000000;
  }

  function readCString(view, offset) {
    const u8a = new Uint8Array(view.buffer, view.byteOffset + offset,
                               view.byteLength - offset);
    let end = 0;
    while (end < u8a.length && u8a[end] !== 0) end++;
    const str = new TextDecoder('utf-8').decode(u8a.subarray(0, end));
    return { str: str, nextOffset: offset + end + 1 };
  }

  class LRU {
    constructor(limit) { this.limit = limit; this.map = new Map(); }
    get(k) {
      if (!this.map.has(k)) return undefined;
      const v = this.map.get(k);
      this.map.delete(k); this.map.set(k, v);
      return v;
    }
    set(k, v) {
      if (this.map.has(k)) this.map.delete(k);
      this.map.set(k, v);
      while (this.map.size > this.limit) {
        this.map.delete(this.map.keys().next().value);
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
      this.clusterCache = new LRU(8);       // clusterNum → {data, extended}
      this.blobCache = new LRU(512);        // "c:b"      → Uint8Array
      this.entryCache = new LRU(1024);      // "ns/url"   → {mime, cluster, blob}
    }

    async _readRange(offset, length) {
      if (offset < 0 || offset + length > this.size) {
        throw new Error(`ZimReader: out-of-range read (${offset}+${length} of ${this.size})`);
      }
      return new Uint8Array(await this.file.slice(offset, offset + length).arrayBuffer());
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
      const buf = await this._readRange(this.header.urlPtrPos + idx * 8, 8);
      return u64(new DataView(buf.buffer), 0);
    }

    async _readClusterPointer(idx) {
      const buf = await this._readRange(this.header.clusterPtrPos + idx * 8, 8);
      return u64(new DataView(buf.buffer), 0);
    }

    async _readDirEntryAt(offset) {
      // Over-read — DirEntries are typically < 512 B including URL + title.
      // If a string ran past our buffer we re-fetch larger.
      const initial = Math.min(1024, this.size - offset);
      let buf = await this._readRange(offset, initial);
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
        buf = await this._readRange(offset, Math.min(8192, this.size - offset));
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

    // Binary search URL pointer list for an entry whose (ns, url) matches.
    // ZIM sorts by (ns, url) ascending.
    async findEntry(path, namespace) {
      namespace = namespace || 'C';
      const cacheKey = namespace + '/' + path;
      const cached = this.entryCache.get(cacheKey);
      if (cached !== undefined) return cached;

      let lo = 0, hi = this.header.articleCount - 1;
      while (lo <= hi) {
        const mid = (lo + hi) >>> 1;
        const off = await this._readUrlPointer(mid);
        const de = await this._readDirEntryAt(off);
        const cmp = cmpNsUrl(de.namespace, de.url, namespace, path);
        if (cmp === 0) {
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
        if (cmp < 0) lo = mid + 1;
        else         hi = mid - 1;
      }
      this.entryCache.set(cacheKey, null);
      return null;
    }

    async _loadCluster(clusterNum) {
      const cached = this.clusterCache.get(clusterNum);
      if (cached) return cached;

      const start = await this._readClusterPointer(clusterNum);
      const end = (clusterNum + 1 < this.header.clusterCount)
        ? await this._readClusterPointer(clusterNum + 1)
        : this.header.checksumPos;
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
      const out = data.subarray(start, stop);
      this.blobCache.set(key, out);
      return out;
    }

    // Main entry point — look up a content path. Returns null if not found.
    async read(path, namespace) {
      // Normalize: strip leading slash / "./"; percent-decode (tiles use
      // percent-encoding for spaces in font names etc.).
      path = String(path || '').replace(/^\.?\//, '');
      try { path = decodeURIComponent(path); } catch (_) { /* keep raw */ }
      const entry = await this.findEntry(path, namespace);
      if (!entry) return null;
      const data = await this._readBlob(entry.cluster, entry.blob);
      return { mime: entry.mime, data: data, url: entry.url };
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

  function cmpNsUrl(aNs, aUrl, bNs, bUrl) {
    if (aNs !== bNs) return aNs < bNs ? -1 : 1;
    if (aUrl === bUrl) return 0;
    return aUrl < bUrl ? -1 : 1;
  }

  global.StreetZimReader = ZimReader;
})(typeof self !== 'undefined' ? self : globalThis);
