//! streetzim-pack — read a JSONL manifest, emit a ZIM via zimru.
//!
//! Manifest schema (one JSON record per line; order matters only for
//! `config`, which must precede any `item`/`metadata`/etc. record):
//!
//! ```jsonl
//! {"kind":"config","compression":"zstd","compression_level":3,"cluster_strategy":"by_mime","cluster_size_target":2097152,"max_in_flight_bytes":536870912,"main_path":"index.html"}
//! {"kind":"metadata","name":"Title","value":"OSM Bay Area"}
//! {"kind":"metadata","name":"CustomBlob","mimetype":"application/octet-stream","body_b64":"AAECAw…"}
//! {"kind":"illustration","size":48,"body_b64":"iVBORw0KGgo…"}
//! {"kind":"item","path":"index.html","title":"Map","mime":"text/html","content":"<html>…</html>","front":true}
//! {"kind":"item","path":"tiles/14/x/y.avif","title":"","mime":"image/avif","body_b64":"AAAAGGZ0eXA…"}
//! {"kind":"item","path":"routing-data/graph-chunk-0001.bin","title":"","mime":"application/octet-stream","file":"/abs/path/chunk.bin","streaming":true,"size":104857600}
//! {"kind":"redirect","path":"home","title":"Home","target":"index.html"}
//! ```
//!
//! Body sources (exactly one per item / per binary metadata):
//! - `content`  — inline UTF-8 string. Cheapest path; used for HTML,
//!   JSON, JS, CSS, SVG, plain text.
//! - `body_b64` — inline base64-encoded bytes. The default for
//!   everything binary that fits in memory (tiles, PNGs,
//!   small PBFs). 33 % file-size inflation buys us:
//!   no per-item `open()` syscalls (a 320 s win at
//!   Japan-scale on APFS), no temp-file staging on the
//!   Python side, and one big sequential read on the
//!   Rust side instead of millions of random opens.
//! - `file`     — path on disk. Reserved for `streaming: true` items
//!   (multi-GB routing chunks where zimru reads a chunk
//!   at a time instead of loading whole-file). Also a
//!   back-compat path for legacy manifests still using
//!   the old per-body staged-files layout.
//!
//! Notes:
//! - `streaming: true` avoids base64 transport for >=64 MiB files. Raw bodies
//!   use the disk-backed chunked API; compressed bodies retain a full-body
//!   buffer in the byte-budgeted regular pipeline. This avoids uncontrolled
//!   overlap of upstream streamed zstd finalizers. Encoder state depends on
//!   workers/level; final verification can map the output into resident memory.
//! - `cluster_break` (`{"kind":"cluster_break","cluster_size_target":N}`)
//!   closes the current cluster and optionally changes the size target;
//!   the flush is compiled in with `--features cluster_break`, which needs
//!   zimru's `Creator::flush_cluster()`.
//! - `compress` is a per-item override: `false` forces the cluster
//!   uncompressed even when `config.compression` is zstd/xz; omitting
//!   it (or `true`) honours the Creator default. zimru groups items by
//!   effective compression so a single ZIM can mix compressed and raw
//!   clusters (use case: streetzim's >500 MB routing chunks that bust
//!   PWA fzstd's per-cluster cap).

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::PathBuf;

use anyhow::{anyhow, bail, Context, Result};
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use clap::Parser;
use serde::Deserialize;
use zimru::writer::{ClusterStrategy, Creator, Item};
use zimru::Compression;

const DEFAULT_STREAM_CHUNK: usize = 4 * 1024 * 1024; // 4 MiB
const STREAMING_THRESHOLD: usize = 64 * 1024 * 1024;
static NEXT_TEMP_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

#[derive(Parser, Debug)]
#[command(
    version,
    about = "Pack a streetzim manifest into a ZIM file via zimru."
)]
struct Cli {
    /// Path to the JSONL manifest produced by streetzim's Python pipeline.
    manifest: PathBuf,
    /// Path of the ZIM file to write.
    output: PathBuf,
    /// Print stats to stderr at finalize time.
    #[arg(long)]
    verbose: bool,
    /// Compression workers. Overrides RAYON_NUM_THREADS when supplied.
    #[arg(long, value_parser = positive_threads)]
    threads: Option<usize>,
}

fn positive_threads(value: &str) -> std::result::Result<usize, String> {
    match value.parse::<usize>() {
        Ok(n) if n > 0 => Ok(n),
        _ => Err("threads must be a positive integer".into()),
    }
}

/// A unique sibling output keeps an existing archive intact on every error.
/// The guard also removes partial output when a library call unwinds.
struct StagedOutput(PathBuf);

impl StagedOutput {
    fn new(output: &PathBuf) -> Result<Self> {
        let parent = output
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| std::path::Path::new("."));
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let nonce = NEXT_TEMP_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        for attempt in 0..100 {
            let path = parent.join(format!(
                ".streetzim-pack-{}-{stamp}-{nonce}-{attempt}.zim",
                std::process::id()
            ));
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(_) => return Ok(Self(path)),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e).context("create staged ZIM beside output"),
            }
        }
        bail!("could not reserve a unique staged output beside {output:?}")
    }

    fn publish(self, output: &PathBuf) -> Result<()> {
        File::open(&self.0)?.sync_all()?;
        std::fs::rename(&self.0, output)
            .with_context(|| format!("publish staged ZIM to {output:?}"))
    }
}

impl Drop for StagedOutput {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Record {
    Config(ConfigRec),
    Metadata(MetadataRec),
    Illustration(IllustrationRec),
    Item(ItemRec),
    Redirect(RedirectRec),
    ClusterBreak(ClusterBreakRec),
}

/// `{"kind":"cluster_break","cluster_size_target":2097152}` — close the
/// cluster being filled so the next item opens a new one, and optionally
/// change the cluster size target from here on. Meant to be written between
/// zoom levels of tiles/satellite/terrain (the builder's designed
/// --tile-order zoom-hilbert, not yet ported: nothing writes it on main) so
/// each zoom is a run of whole clusters that cloud/derive_zim.py can copy or
/// drop without re-encoding. The flush needs a `Creator::flush_cluster()`
/// on zimru and is compiled in only with `--features cluster_break`; without
/// it the record warns once, and its size target reaches the running
/// streamer only if zimru carries patches/zimru-flush-cluster.patch.
#[derive(Debug, Deserialize)]
struct ClusterBreakRec {
    #[serde(default)]
    cluster_size_target: Option<usize>,
}

#[derive(Debug, Deserialize, Default)]
struct ConfigRec {
    #[serde(default, rename = "_indexing_requested")]
    indexing_requested: bool,
    #[serde(default)]
    compression: Option<String>,
    #[serde(default)]
    compression_level: Option<i32>,
    #[serde(default)]
    cluster_strategy: Option<String>,
    #[serde(default)]
    cluster_size_target: Option<usize>,
    #[serde(default)]
    max_in_flight_bytes: Option<usize>,
    #[serde(default)]
    main_path: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MetadataRec {
    name: String,
    #[serde(default)]
    mimetype: Option<String>,
    #[serde(default)]
    value: Option<String>,
    /// Base64-encoded inline body. Mutually exclusive with `value` /
    /// `file`. Used for binary metadata blobs that need to ride
    /// inline alongside the string-valued metadata.
    #[serde(default)]
    body_b64: Option<String>,
    #[serde(default)]
    file: Option<PathBuf>,
}

#[derive(Debug, Deserialize)]
struct IllustrationRec {
    size: u32,
    /// Base64-encoded PNG body. Mutually exclusive with `file`.
    #[serde(default)]
    body_b64: Option<String>,
    #[serde(default)]
    file: Option<PathBuf>,
}

#[derive(Debug, Deserialize)]
struct ItemRec {
    path: String,
    #[serde(default)]
    title: String,
    mime: String,
    /// Inline UTF-8 body. Used for text mimes that round-trip
    /// through JSON without escaping cost (HTML, JSON, JS, CSS, SVG).
    #[serde(default)]
    content: Option<String>,
    /// Inline base64-encoded body. Default path for binary items
    /// (tiles, PNGs, small PBFs) — see crate docstring for the full
    /// rationale and tradeoffs.
    #[serde(default)]
    body_b64: Option<String>,
    /// On-disk path. Reserved for `streaming: true` items (huge
    /// routing chunks). Legacy manifests may also reference small
    /// staged files here.
    #[serde(default)]
    file: Option<PathBuf>,
    #[serde(default)]
    front: bool,
    #[serde(default)]
    streaming: bool,
    #[serde(default)]
    size: Option<u64>,
    #[serde(default)]
    namespace: Option<u8>,
    /// Per-item compression override. `Some(false)` forces an
    /// uncompressed cluster regardless of the build-wide setting;
    /// `None` (or `Some(true)`) honours the Creator default. zimru
    /// groups items by effective compression so mixing values inside
    /// a single manifest is supported.
    #[serde(default)]
    compress: Option<bool>,
}

#[derive(Debug, Deserialize)]
struct RedirectRec {
    path: String,
    #[serde(default)]
    title: String,
    target: String,
}

fn parse_compression(s: &str) -> Result<Compression> {
    match s.to_ascii_lowercase().as_str() {
        "none" => Ok(Compression::None),
        "zstd" => Ok(Compression::Zstd),
        "xz" => Ok(Compression::Xz),
        other => bail!("unknown compression: {other:?} (expected none|zstd|xz)"),
    }
}

fn parse_cluster_strategy(s: &str) -> Result<ClusterStrategy> {
    match s.to_ascii_lowercase().as_str() {
        "single" => Ok(ClusterStrategy::Single),
        "by_mime" => Ok(ClusterStrategy::ByMime),
        "by_extension" => Ok(ClusterStrategy::ByExtension),
        "by_first_path_segment" => Ok(ClusterStrategy::ByFirstPathSegment),
        other => bail!(
            "unknown cluster_strategy: {other:?} (expected single|by_mime|by_extension|by_first_path_segment)"
        ),
    }
}

fn read_file_bytes(path: &PathBuf) -> Result<Vec<u8>> {
    std::fs::read(path).with_context(|| format!("read {path:?}"))
}

fn decode_body_b64(s: &str) -> Result<Vec<u8>> {
    BASE64
        .decode(s.as_bytes())
        .map_err(|e| anyhow!("base64 decode: {e}"))
}

fn apply_config(creator: &mut Creator, cfg: &ConfigRec) -> Result<()> {
    if cfg.indexing_requested {
        bail!(
            "Rust packer cannot run libzim's Xapian indexer; use --xapian=builder or --xapian=none"
        );
    }
    if let Some(ref c) = cfg.compression {
        creator.set_compression(parse_compression(c)?);
    }
    if let Some(level) = cfg.compression_level {
        creator.set_compression_level(level);
    }
    if let Some(ref s) = cfg.cluster_strategy {
        creator.set_cluster_strategy(parse_cluster_strategy(s)?);
    }
    if let Some(n) = cfg.cluster_size_target {
        creator.set_cluster_size_target(n);
    }
    if let Some(n) = cfg.max_in_flight_bytes {
        creator.set_max_in_flight_bytes(n);
    }
    if let Some(ref p) = cfg.main_path {
        creator.try_set_main_path(p.clone())?;
    }
    Ok(())
}

fn handle_metadata(creator: &mut Creator, rec: MetadataRec) -> Result<()> {
    let bytes: Vec<u8> = match (rec.value, rec.body_b64, rec.file) {
        (Some(s), None, None) => s.into_bytes(),
        (None, Some(b64), None) => {
            decode_body_b64(&b64).with_context(|| format!("metadata {:?}", rec.name))?
        }
        (None, None, Some(p)) => read_file_bytes(&p)?,
        (None, None, None) => bail!(
            "metadata {:?}: must provide value, body_b64, or file",
            rec.name
        ),
        _ => bail!(
            "metadata {:?}: only one of value/body_b64/file allowed",
            rec.name
        ),
    };
    match rec.mimetype {
        Some(mt) => {
            creator.try_add_metadata_with_mimetype(rec.name, mt, bytes)?;
        }
        None => {
            creator.try_add_metadata(rec.name, bytes)?;
        }
    }
    Ok(())
}

fn handle_illustration(creator: &mut Creator, rec: IllustrationRec) -> Result<()> {
    let bytes = match (rec.body_b64, rec.file) {
        (Some(b64), None) => decode_body_b64(&b64)
            .with_context(|| format!("illustration {}x{}", rec.size, rec.size))?,
        (None, Some(p)) => read_file_bytes(&p)?,
        (None, None) => bail!(
            "illustration {}x{}: must provide body_b64 or file",
            rec.size,
            rec.size
        ),
        (Some(_), Some(_)) => bail!(
            "illustration {}x{}: only one of body_b64/file allowed",
            rec.size,
            rec.size
        ),
    };
    creator.try_add_illustration(rec.size, bytes)?;
    Ok(())
}

fn handle_redirect(creator: &mut Creator, rec: RedirectRec) -> Result<()> {
    creator.try_add_redirection(rec.path, rec.title, rec.target)?;
    Ok(())
}

fn handle_item(creator: &mut Creator, rec: ItemRec, compression: Compression) -> Result<()> {
    // FRONT_ARTICLE is libzim's title-list hint, not the archive's main page.
    // Only config.main_path may select that page. zimru lists content titles
    // itself; retaining the hint in the schema keeps old manifests compatible.
    let _front_article = rec.front;

    if rec.streaming {
        if rec.content.is_some() || rec.body_b64.is_some() {
            bail!("item {:?}: streaming requires only a file body", rec.path);
        }
        let file_path = rec
            .file
            .as_ref()
            .ok_or_else(|| {
                anyhow!(
                    "item {:?}: streaming requires file (no inline content)",
                    rec.path
                )
            })?
            .clone();
        let raw = rec.compress == Some(false)
            || rec.namespace == Some(b'X')
            || matches!(compression, Compression::None);
        return stream_item_from_file(creator, &rec, &file_path, raw);
    }

    let bytes: Vec<u8> = match (&rec.content, &rec.body_b64, &rec.file) {
        (Some(s), None, None) => s.clone().into_bytes(),
        (None, Some(b64), None) => {
            decode_body_b64(b64).with_context(|| format!("item {:?}", rec.path))?
        }
        (None, None, Some(p)) => read_file_bytes(p)?,
        (None, None, None) => bail!(
            "item {:?}: must provide content, body_b64, or file",
            rec.path
        ),
        _ => bail!(
            "item {:?}: only one of content/body_b64/file allowed",
            rec.path
        ),
    };
    let mut item = match rec.namespace {
        Some(ns) => Item::in_namespace(ns, rec.path, rec.title, rec.mime, bytes),
        None => Item::new(rec.path, rec.title, rec.mime, bytes),
    };
    item.compress = rec.compress;
    creator.try_add_item(item)?;
    Ok(())
}

fn stream_item_from_file(
    creator: &mut Creator,
    rec: &ItemRec,
    file: &PathBuf,
    raw: bool,
) -> Result<()> {
    let f = File::open(file).with_context(|| format!("open {file:?}"))?;
    let metadata = f.metadata().with_context(|| format!("stat {file:?}"))?;
    let on_disk_size = metadata.len();
    let size_hint = rec.size.unwrap_or(on_disk_size);
    if let Some(declared) = rec.size {
        if declared != on_disk_size {
            bail!(
                "item {:?}: declared size {} != on-disk size {} for {:?}",
                rec.path,
                declared,
                on_disk_size,
                file
            );
        }
    }

    if !raw {
        // zimru has no public wait/drain API for its background streamed
        // zstd finalizers. At level 22, four 64 MiB streams with one worker
        // exceeded 3 GB despite a 1 MiB pipeline budget. Retain the original
        // regular pipeline for compressed bodies: one largest-body buffer,
        // with byte-budgeted encoding instead of overlapping stream encoders.
        let size = usize::try_from(size_hint).context("item exceeds address space")?;
        let mut builder = creator
            .begin_item(
                rec.path.clone(),
                rec.title.clone(),
                rec.mime.clone(),
                rec.namespace,
                Some(size),
            )
            .map_err(|e| anyhow!("begin_item({:?}): {e}", rec.path))?;
        builder.set_compress(rec.compress);
        feed_file(f, file, size_hint, |chunk| {
            builder.write_chunk(chunk);
            Ok(())
        })?;
        return builder
            .finish()
            .map_err(|e| anyhow!("finish_item({:?}): {e}", rec.path));
    }

    // Raw bodies use the actual disk-backed chunked API. ItemBuilder would
    // instead accumulate the entire file in a Vec before passing it on.
    creator
        .begin_chunked_item(
            rec.namespace,
            rec.path.clone(),
            rec.title.clone(),
            rec.mime.clone(),
            Some(size_hint),
            rec.compress,
        )
        .map_err(|e| anyhow!("begin_chunked_item({:?}): {e}", rec.path))?;
    feed_file(f, file, size_hint, |chunk| {
        creator
            .chunked_item_chunk(chunk)
            .map_err(|e| anyhow!("chunked_item_chunk({:?}): {e}", rec.path))
    })?;
    creator
        .end_chunked_item()
        .map_err(|e| anyhow!("finish_item({:?}): {e}", rec.path))?;
    Ok(())
}

fn feed_file(
    mut reader: File,
    file: &PathBuf,
    expected: u64,
    mut write: impl FnMut(&[u8]) -> Result<()>,
) -> Result<()> {
    let mut buf = vec![0u8; DEFAULT_STREAM_CHUNK];
    let mut remaining = expected;
    loop {
        let n = reader
            .read(&mut buf)
            .with_context(|| format!("read {file:?}"))?;
        if n == 0 {
            break;
        }
        if n as u64 > remaining {
            bail!("file {file:?}: body grew beyond expected {expected} bytes");
        }
        write(&buf[..n])?;
        remaining -= n as u64;
    }
    if remaining != 0 {
        bail!(
            "file {file:?}: body shrank; expected {expected} bytes, got {}",
            expected - remaining
        );
    }
    Ok(())
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    if let Some(n) = cli.threads {
        // Before Creator can initialize rayon or spawn any threads.
        std::env::set_var("RAYON_NUM_THREADS", n.to_string());
    }
    run(&cli)
}

fn run(cli: &Cli) -> Result<()> {
    let started = std::time::Instant::now();

    let mut f =
        File::open(&cli.manifest).with_context(|| format!("open manifest {:?}", cli.manifest))?;
    // Accept a zstd-compressed manifest, detected by magic rather than by file
    // extension so a plain manifest keeps working unchanged. The manifest is
    // mostly base64 of already-compressed tiles plus JSON search data;
    // measured on brazil's, zstd -3 shrinks the tile section 1.87x and the
    // search-data section (72% of the bytes) 5.22x — ~93 GB -> ~25 GB on disk.
    // Decompression runs ~1 GB/s against a packer bound at tens of MB/s by
    // zstd-22 cluster compression, so reading it costs nothing measurable.
    let mut magic = [0u8; 4];
    let got = f
        .read(&mut magic)
        .with_context(|| format!("read manifest header {:?}", cli.manifest))?;
    f.seek(SeekFrom::Start(0))
        .with_context(|| format!("rewind manifest {:?}", cli.manifest))?;
    let raw: Box<dyn Read> = if got == 4 && magic == [0x28, 0xB5, 0x2F, 0xFD] {
        Box::new(
            zstd::stream::read::Decoder::new(f)
                .with_context(|| format!("zstd decoder for {:?}", cli.manifest))?,
        )
    } else {
        Box::new(f)
    };
    // 8 MiB, not the 8 KiB default: reading the manifest is now the streaming
    // hot path, and brazil's 93 GB would otherwise cost ~12M read(2) calls.
    let reader = BufReader::with_capacity(8 << 20, raw);

    // Stream the manifest: parse one line, handle it, drop it.
    //
    // This used to collect every record into a Vec<Record> before writing a
    // single byte. Because the manifest INLINES bodies — ItemRec::content as
    // UTF-8 and ItemRec::body_b64 as base64, the latter held in its encoded
    // form until handle_item decodes it — that Vec is not metadata, it is the
    // whole payload. Peak RSS therefore tracked manifest size at ~1.02x and
    // was reached before any output: brazil's 93.1 GB manifest died at ~95 GB
    // RSS after six hours, twice, and no continent could ever be packed.
    //
    // zimru's regular pipeline budgets body allocations. Parsing incrementally
    // removes the total-manifest allocation; the largest compressed body,
    // dirents, encoder state and mapped verification pages still affect RSS.
    //
    // Safe because the manifest format guarantees `config` precedes every
    // other record (see the module docstring), and cloud/manifest_writer.py
    // emits it at __enter__ before any caller can add an item. A config
    // arriving after writing has begun is now an explicit error rather than
    // being silently applied too late to matter.
    let mut creator = Creator::new();
    creator.set_streaming_encode_threshold(STREAMING_THRESHOLD);
    let mut compression = Compression::Zstd;
    let staged = StagedOutput::new(&cli.output)?;
    let mut applied_config = false;
    let mut writing = false;
    let mut counts = (0usize, 0usize, 0usize, 0usize);
    let mut breaks = 0usize;
    #[cfg(not(feature = "cluster_break"))]
    let mut warned_no_flush = false;

    macro_rules! ensure_writing {
        () => {
            if !writing {
                creator
                    .start_writing(&staged.0)
                    .map_err(|e| anyhow!("start_writing({:?}): {e}", staged.0))?;
                writing = true;
            }
        };
    }

    for (lineno, line) in reader.lines().enumerate() {
        let line = line.with_context(|| format!("read manifest line {}", lineno + 1))?;
        let s = line.trim();
        if s.is_empty() || s.starts_with('#') {
            continue;
        }
        let rec: Record = serde_json::from_str(s).with_context(|| {
            // Truncate: a bad search-data record is megabytes on one line.
            let snip: String = s.chars().take(200).collect();
            let ell = if s.len() > snip.len() { "…" } else { "" };
            format!("parse manifest line {}: {snip}{ell}", lineno + 1)
        })?;
        match rec {
            Record::Config(cfg) => {
                if applied_config {
                    bail!("manifest contains more than one config record");
                }
                if writing {
                    bail!(
                        "manifest line {}: config record appears after content; \
                         config must precede every other record",
                        lineno + 1
                    );
                }
                apply_config(&mut creator, &cfg)?;
                if let Some(ref value) = cfg.compression {
                    compression = parse_compression(value)?;
                }
                applied_config = true;
            }
            Record::Metadata(m) => {
                ensure_writing!();
                handle_metadata(&mut creator, m)?;
                counts.0 += 1;
            }
            Record::Illustration(i) => {
                ensure_writing!();
                handle_illustration(&mut creator, i)?;
                counts.1 += 1;
            }
            Record::Item(it) => {
                ensure_writing!();
                handle_item(&mut creator, it, compression)?;
                counts.2 += 1;
            }
            Record::Redirect(r) => {
                ensure_writing!();
                handle_redirect(&mut creator, r)?;
                counts.3 += 1;
            }
            Record::ClusterBreak(b) => {
                ensure_writing!();
                #[cfg(feature = "cluster_break")]
                {
                    creator.flush_cluster().map_err(|e| {
                        anyhow!("flush_cluster at manifest line {}: {e}", lineno + 1)
                    })?;
                }
                #[cfg(not(feature = "cluster_break"))]
                {
                    if !warned_no_flush {
                        eprintln!(
                            "streetzim-pack: cluster_break records present but this binary was \
                             built without the `cluster_break` feature; clusters will NOT be \
                             split at zoom boundaries (a size target changes mid-stream only \
                             with patches/zimru-flush-cluster.patch)"
                        );
                        warned_no_flush = true;
                    }
                }
                if let Some(n) = b.cluster_size_target {
                    creator.set_cluster_size_target(n);
                }
                breaks += 1;
            }
        }
    }

    // A manifest with no content records still has to produce a valid ZIM,
    // which the old unconditional start_writing gave for free.
    if !writing {
        creator
            .start_writing(&staged.0)
            .map_err(|e| anyhow!("start_writing({:?}): {e}", staged.0))?;
    }

    creator
        .finish_writing()
        .map_err(|e| anyhow!("finish_writing({:?}): {e}", cli.output))?;
    staged.publish(&cli.output)?;

    let elapsed = started.elapsed();
    if cli.verbose {
        eprintln!(
            "streetzim-pack: wrote {:?} in {:.2}s — items={} metadata={} illustrations={} redirects={} cluster_breaks={}",
            cli.output,
            elapsed.as_secs_f64(),
            counts.2,
            counts.0,
            counts.1,
            counts.3,
            breaks
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDir(PathBuf);
    impl TestDir {
        fn new() -> Self {
            let stamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let nonce = NEXT_TEMP_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "streetzim-pack-test-{}-{stamp}-{nonce}",
                std::process::id()
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn cli(&self, manifest: &str) -> Cli {
            let path = self.0.join("manifest.jsonl");
            std::fs::write(&path, manifest).unwrap();
            Cli {
                manifest: path,
                output: self.0.join("out.zim"),
                verbose: false,
                threads: None,
            }
        }
        fn assert_no_partial(&self) {
            assert!(std::fs::read_dir(&self.0).unwrap().all(|e| !e
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".streetzim-pack-")));
        }
    }
    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    const CONFIG: &str =
        "{\"kind\":\"config\",\"compression\":\"none\",\"main_path\":\"index.html\"}\n";
    const INDEX: &str = "{\"kind\":\"item\",\"path\":\"index.html\",\"mime\":\"text/html\",\"content\":\"viewer\",\"front\":true}\n";

    #[test]
    fn front_articles_do_not_replace_the_main_page() {
        let dir = TestDir::new();
        let cli = dir.cli(&format!("{CONFIG}{INDEX}{}\n", r#"{"kind":"item","path":"search/end.html","mime":"text/html","content":"search","front":true}"#));
        run(&cli).unwrap();
        assert_eq!(
            zimru::Archive::open(&cli.output)
                .unwrap()
                .main_path()
                .unwrap(),
            "index.html"
        );
        dir.assert_no_partial();
    }

    #[test]
    fn errors_preserve_existing_output_before_and_after_writing() {
        for extra in [
            "not json",
            r#"{"kind":"item","path":"bad","mime":"text/plain","body_b64":"!!"}"#,
            r#"{"kind":"item","path":"index.html","mime":"text/html","content":"duplicate"}"#,
        ] {
            let dir = TestDir::new();
            let cli = dir.cli(&format!("{CONFIG}{INDEX}{extra}\n"));
            std::fs::write(&cli.output, b"existing archive").unwrap();
            assert!(run(&cli).is_err());
            assert_eq!(std::fs::read(&cli.output).unwrap(), b"existing archive");
            dir.assert_no_partial();
        }
        let dir = TestDir::new();
        let cli = dir.cli("");
        std::fs::write(&cli.output, b"existing archive").unwrap();
        std::fs::remove_file(&cli.manifest).unwrap();
        assert!(run(&cli).is_err());
        assert_eq!(std::fs::read(&cli.output).unwrap(), b"existing archive");
        dir.assert_no_partial();
    }

    #[test]
    fn failed_first_build_never_publishes_output() {
        let dir = TestDir::new();
        let cli = dir.cli(&format!("{CONFIG}{INDEX}not json\n"));
        assert!(run(&cli).is_err());
        assert!(!cli.output.exists());
        dir.assert_no_partial();
    }

    #[test]
    fn streaming_rejects_ambiguous_sources_and_wrong_sizes() {
        let dir = TestDir::new();
        let body = dir.0.join("body");
        std::fs::write(&body, b"abc").unwrap();
        for extra in ["\"content\":\"also inline\",\"size\":3", "\"size\":4"] {
            let item = format!(
                r#"{{"kind":"item","path":"graph","mime":"application/octet-stream","streaming":true,"file":{:?},{extra}}}"#,
                body.to_str().unwrap()
            );
            let cli = dir.cli(&format!("{CONFIG}{INDEX}{item}\n"));
            assert!(run(&cli).is_err());
            assert!(!cli.output.exists());
            dir.assert_no_partial();
        }
    }

    #[test]
    fn success_replaces_existing_output() {
        let dir = TestDir::new();
        let cli = dir.cli(&format!("{CONFIG}{INDEX}"));
        std::fs::write(&cli.output, b"old archive").unwrap();
        run(&cli).unwrap();
        assert_eq!(
            zimru::Archive::open(&cli.output)
                .unwrap()
                .main_path()
                .unwrap(),
            "index.html"
        );
        dir.assert_no_partial();
    }

    #[test]
    fn workers_must_be_positive() {
        assert_eq!(positive_threads("2"), Ok(2));
        for invalid in ["0", "-1", "x"] {
            assert!(positive_threads(invalid).is_err());
        }
    }

    #[test]
    fn libzim_index_request_is_rejected_without_replacing_output() {
        let dir = TestDir::new();
        let cli = dir.cli(r#"{"kind":"config","_indexing_requested":true}"#);
        std::fs::write(&cli.output, b"previous archive").unwrap();
        assert!(run(&cli)
            .unwrap_err()
            .to_string()
            .contains("--xapian=builder"));
        assert_eq!(std::fs::read(&cli.output).unwrap(), b"previous archive");
        dir.assert_no_partial();
    }

    #[test]
    fn reads_enforce_exact_size_on_both_body_routes() {
        let dir = TestDir::new();
        let body = dir.0.join("body");
        std::fs::write(&body, b"abc").unwrap();
        for expected in [2, 4] {
            assert!(feed_file(File::open(&body).unwrap(), &body, expected, |_| Ok(())).is_err());
        }
        let mut got = Vec::new();
        feed_file(File::open(&body).unwrap(), &body, 3, |chunk| {
            got.extend_from_slice(chunk);
            Ok(())
        })
        .unwrap();
        assert_eq!(got, b"abc");
    }
}
