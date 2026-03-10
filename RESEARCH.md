# StreetZIM Research Notes

Research into ZIM file format, Kiwix ecosystem, and approaches for packaging
OpenStreetMap data for offline use.

## ZIM File Internal Structure

The ZIM format is an open archive format by the openZIM project for storing web content offline.

**Structure:**
- **Header** — starts with magic number `72173914` (little-endian), followed by UUID, version numbers, entry counts, and offsets
- **MIME Type List** — immediately follows the header
- **Directory Entries** — metadata about all entries (articles, images, etc.); paths/titles stored as zero-terminated UTF-8 strings
- **Path Pointer List** — 8-byte offsets to directory entries for random access
- **Title Pointer List** — 4-byte entry numbers ordered by title
- **Clusters and Blobs** — actual content data. Multiple entries can be compressed together in one cluster (~1 MB typical). A directory entry specifies cluster number + blob index, enabling random reads without scanning the whole file

**Compression:** Defaults to Zstandard (since 2021); also supports LZMA2/XZ. Compression ratios up to 3x, applied at the cluster level.

**Namespaces:** In ZIM v6.1+, a dedicated 1-byte field separates content types (e.g., `C/` for content, `M/` for metadata).

## Creating ZIM Files Programmatically

### python-libzim (recommended)

```bash
pip install libzim  # v3.7.0+, requires Python >= 3.10
```

Use the `Creator` class as a context manager. Subclass `Item` to define entries with `get_path()`, `get_title()`, `get_mimetype()`, `get_contentprovider()`, `get_hints()`. Content providers: `StringProvider` (in-memory) and `FileProvider` (files on disk).

### zimwriterfs (part of zim-tools)

CLI tool: takes a local directory of self-contained HTML/JS/CSS/images and packs into ZIM.

```bash
zimwriterfs --welcome=index.html --favicon=favicon.png --language=eng \
    --title="My App" --description="..." --creator=me --publisher=me \
    ./html_dir output.zim
```

### Zimit / warc2zim

Crawls a live website (via Browsertrix/Puppeteer), produces WARC files, converts to ZIM. Good for capturing existing JS-heavy web apps. POST requests and server-dependent features won't work offline.

### libzim (C++)

Low-level C++ reference implementation, GPLv2. The foundation for all other tools.

## JavaScript Capabilities in Kiwix

### Native Kiwix iOS App (kiwix-apple)

Uses WKWebView. Renders ZIM HTML content but has **limited JavaScript support** for dynamic ZIM content. Service Worker support for ZIM content (Issue #341) is still open/unresolved. Handles static/MediaWiki content well but struggles with Zimit-based or heavily dynamic ZIMs.

### Kiwix JS PWA via Safari on iOS

Better path for JS-heavy content on iOS. In **ServiceWorker mode** (Safari only on iOS), it intercepts fetch calls and serves content from ZIM — **dynamic content and JavaScript are fully supported**. Requires iOS 15+.

**Key limitation:** ServiceWorker mode only works in Safari on iOS. The native Kiwix app does not yet fully support Service Worker-based ZIM content.

### kiwix-serve

Acts as a standard HTTP web server serving ZIM content over the network. Since the browser handles all script execution natively, **JavaScript-heavy content works as-is**. Most reliable way to serve dynamic ZIM content.

### kiwix-js Modes

- **Restricted Mode:** Injects HTML into an iframe via DOM manipulation. JavaScript assets are **not extracted or run**. Dynamic UIs are broken.
- **ServiceWorker Mode:** Intercepts browser fetch calls and serves content directly from ZIM. **JavaScript and dynamic content are fully supported.**

## Existing ZIM Files with Interactive JS Applications

- **Zimit-based ZIMs:** Any website captured via Zimit preserves its JavaScript. Require ServiceWorker mode.
- **openzim/maps:** Official openZIM project packaging OpenStreetMap into ZIM files with a Leaflet-based interactive JS map viewer. Supports pan, zoom, search. Uses raster tiles.
- **Offline World Map for Kiwix** by Anthony Karam: Uses pre-rendered raster tiles (OSM + Sentinel-2 satellite imagery) with Leaflet.
- **AtlasZIM** (atlaszim.com): A searchable world atlas as a .zim file.

## Service Worker and Advanced Web API Support

### Kiwix JS / PWA (browser-based)

- Full Service Worker support in ServiceWorker mode
- Uses File System Access API and Origin Private File System (OPFS) for storing large archives
- Cache API for offline functionality
- WebAssembly potentially usable from ServiceWorker

### Native Apps

- **kiwix-apple (iOS/macOS):** Service Worker support for ZIM content still open issue (#341)
- **kiwix-android:** Supports Zimit/SW-based ZIMs
- **kiwix-desktop:** Has workaround but not full native support

### iOS-specific limitations

- OPFS nominally supported in Safari but WriteableFileStream API not yet available
- ServiceWorker mode only works in Safari, not other iOS browsers

## Maximum Practical ZIM File Size on iOS

No hard file format or app-imposed size limit. Practical constraints:

- iOS uses APFS, no 4 GB file limit (unlike FAT32 on some Android SD cards)
- Limit is device's available storage. Full English Wikipedia with images: ~97 GB as ZIM
- Downloading large ZIMs (16+ GB) through the app can be unreliable — transfers via iTunes/Finder file sharing more practical
- External storage (USB drives) may not be reliably accessible from Kiwix on iOS

## Existing Projects: Maps in ZIM Files

### 1. openzim/maps (official)

Creates ZIM files from OpenStreetMap subsets using Docker. Packages OSM tiles with a Leaflet-based viewer. Supports region selection via Geofabrik polygon files, configurable default view/zoom. Most mature, officially-supported approach. Uses **raster tiles**.

### 2. Offline World Map for Kiwix (Anthony Karam)

Uses pre-rendered raster tiles (OSM + Sentinel-2 satellite imagery) with Leaflet. Deliberately chose raster over vector tiles for consistent performance across low-power devices. Large file size is the tradeoff.

### 3. AtlasZIM

Searchable world atlas ZIM at atlaszim.com.

### Why raster over vector in existing projects?

The raster-tile approach is favored in existing projects because vector tiles require client-side geometry decoding and rendering, which is unpredictable across the wide range of devices running Kiwix. Our vector-tile approach (MapLibre GL JS) is novel in this space.

## Key Takeaway for StreetZIM

For interactive JS applications (like maps) packaged in ZIM:

| Delivery Method | JS Support | Best For |
|----------------|-----------|----------|
| kiwix-serve | Full | Desktop/server setups |
| Kiwix JS PWA (Safari, SW mode) | Full | iOS via Safari |
| Native Kiwix iOS app | Limited | Static content only |
| Kiwix Android | Full | Android devices |

**Recommendation:** Target kiwix-serve for desktop and Kiwix JS PWA in Safari for iOS. The native iOS Kiwix app may not fully support MapLibre GL JS rendering from ZIM content until Service Worker support lands.

## References

- [ZIM file format - openZIM Wiki](https://wiki.openzim.org/wiki/ZIM_file_format)
- [python-libzim on PyPI](https://pypi.org/project/libzim/)
- [openzim/zim-tools GitHub](https://github.com/openzim/zim-tools)
- [openzim/zimit GitHub](https://github.com/openzim/zimit)
- [kiwix/kiwix-apple GitHub](https://github.com/kiwix/kiwix-apple)
- [kiwix/kiwix-js GitHub](https://github.com/kiwix/kiwix-js)
- [openzim/maps GitHub](https://github.com/openzim/maps)
- [Offline World Map project](https://anthonykaram.github.io/offline-world-map/)
- [Kiwix PWA case study - web.dev](https://web.dev/case-studies/kiwix)
