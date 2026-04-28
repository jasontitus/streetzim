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
//!                JSON, JS, CSS, SVG, plain text.
//! - `body_b64` — inline base64-encoded bytes. The default for
//!                everything binary that fits in memory (tiles, PNGs,
//!                small PBFs). 33 % file-size inflation buys us:
//!                no per-item `open()` syscalls (a 320 s win at
//!                Japan-scale on APFS), no temp-file staging on the
//!                Python side, and one big sequential read on the
//!                Rust side instead of millions of random opens.
//! - `file`     — path on disk. Reserved for `streaming: true` items
//!                (multi-GB routing chunks where zimru reads a chunk
//!                at a time instead of loading whole-file). Also a
//!                back-compat path for legacy manifests still using
//!                the old per-body staged-files layout.
//!
//! Notes:
//! - `streaming: true` routes through zimru's chunked-streaming path
//!   (memory peak = chunk size, not file size). Use it for >64 MiB
//!   files; bodies that big should not be base64-inlined.
//! - `compress` is a per-item override: `false` forces the cluster
//!   uncompressed even when `config.compression` is zstd/xz; omitting
//!   it (or `true`) honours the Creator default. zimru groups items by
//!   effective compression so a single ZIM can mix compressed and raw
//!   clusters (use case: streetzim's >500 MB routing chunks that bust
//!   PWA fzstd's per-cluster cap).

use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;

use anyhow::{anyhow, bail, Context, Result};
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use clap::Parser;
use serde::Deserialize;
use zimru::writer::{ClusterStrategy, Creator, Item};
use zimru::Compression;

const DEFAULT_STREAM_CHUNK: usize = 4 * 1024 * 1024; // 4 MiB

#[derive(Parser, Debug)]
#[command(version, about = "Pack a streetzim manifest into a ZIM file via zimru.")]
struct Cli {
    /// Path to the JSONL manifest produced by streetzim's Python pipeline.
    manifest: PathBuf,
    /// Path of the ZIM file to write.
    output: PathBuf,
    /// Print stats to stderr at finalize time.
    #[arg(long)]
    verbose: bool,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Record {
    Config(ConfigRec),
    Metadata(MetadataRec),
    Illustration(IllustrationRec),
    Item(ItemRec),
    Redirect(RedirectRec),
}

#[derive(Debug, Deserialize, Default)]
struct ConfigRec {
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
        creator.set_main_path(p.clone());
    }
    Ok(())
}

fn handle_metadata(creator: &mut Creator, rec: MetadataRec) -> Result<()> {
    let bytes: Vec<u8> = match (rec.value, rec.body_b64, rec.file) {
        (Some(s), None, None) => s.into_bytes(),
        (None, Some(b64), None) => decode_body_b64(&b64)
            .with_context(|| format!("metadata {:?}", rec.name))?,
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
            creator.add_metadata_with_mimetype(rec.name, mt, bytes);
        }
        None => {
            creator.add_metadata(rec.name, bytes);
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
            rec.size, rec.size
        ),
        (Some(_), Some(_)) => bail!(
            "illustration {}x{}: only one of body_b64/file allowed",
            rec.size, rec.size
        ),
    };
    creator.add_illustration(rec.size, bytes);
    Ok(())
}

fn handle_redirect(creator: &mut Creator, rec: RedirectRec) -> Result<()> {
    creator.add_redirection(rec.path, rec.title, rec.target);
    Ok(())
}

fn handle_item(creator: &mut Creator, rec: ItemRec) -> Result<()> {
    if rec.front {
        creator.set_main_path(rec.path.clone());
    }

    if rec.streaming {
        let file_path = rec
            .file
            .as_ref()
            .ok_or_else(|| anyhow!("item {:?}: streaming requires file (no inline content)", rec.path))?
            .clone();
        return stream_item_from_file(creator, &rec, &file_path);
    }

    let bytes: Vec<u8> = match (&rec.content, &rec.body_b64, &rec.file) {
        (Some(s), None, None) => s.clone().into_bytes(),
        (None, Some(b64), None) => decode_body_b64(b64)
            .with_context(|| format!("item {:?}", rec.path))?,
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
    creator.add_item(item);
    Ok(())
}

fn stream_item_from_file(creator: &mut Creator, rec: &ItemRec, file: &PathBuf) -> Result<()> {
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

    let mut reader = BufReader::with_capacity(DEFAULT_STREAM_CHUNK, f);
    let mut builder = creator
        .begin_item(
            rec.path.clone(),
            rec.title.clone(),
            rec.mime.clone(),
            rec.namespace,
            Some(size_hint as usize),
        )
        .map_err(|e| anyhow!("begin_item({:?}): {e}", rec.path))?;
    builder.set_compress(rec.compress);
    let mut buf = vec![0u8; DEFAULT_STREAM_CHUNK];
    loop {
        let n = reader.read(&mut buf).with_context(|| format!("read {file:?}"))?;
        if n == 0 {
            break;
        }
        builder.write_chunk(&buf[..n]);
    }
    builder
        .finish()
        .map_err(|e| anyhow!("finish_item({:?}): {e}", rec.path))?;
    Ok(())
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let started = std::time::Instant::now();

    let f = File::open(&cli.manifest)
        .with_context(|| format!("open manifest {:?}", cli.manifest))?;
    let reader = BufReader::new(f);

    let mut records: Vec<Record> = Vec::new();
    for (lineno, line) in reader.lines().enumerate() {
        let line = line.with_context(|| format!("read manifest line {}", lineno + 1))?;
        let s = line.trim();
        if s.is_empty() || s.starts_with('#') {
            continue;
        }
        let rec: Record = serde_json::from_str(s)
            .with_context(|| format!("parse manifest line {}: {s}", lineno + 1))?;
        records.push(rec);
    }

    let mut creator = Creator::new();

    let mut applied_config = false;
    for rec in &records {
        if let Record::Config(cfg) = rec {
            if applied_config {
                bail!("manifest contains more than one config record");
            }
            apply_config(&mut creator, cfg)?;
            applied_config = true;
        }
    }

    creator
        .start_writing(&cli.output)
        .map_err(|e| anyhow!("start_writing({:?}): {e}", cli.output))?;

    let mut counts = (0usize, 0usize, 0usize, 0usize);
    for rec in records {
        match rec {
            Record::Config(_) => {}
            Record::Metadata(m) => {
                handle_metadata(&mut creator, m)?;
                counts.0 += 1;
            }
            Record::Illustration(i) => {
                handle_illustration(&mut creator, i)?;
                counts.1 += 1;
            }
            Record::Item(it) => {
                handle_item(&mut creator, it)?;
                counts.2 += 1;
            }
            Record::Redirect(r) => {
                handle_redirect(&mut creator, r)?;
                counts.3 += 1;
            }
        }
    }

    creator
        .finish_writing()
        .map_err(|e| anyhow!("finish_writing({:?}): {e}", cli.output))?;

    let elapsed = started.elapsed();
    if cli.verbose {
        eprintln!(
            "streetzim-pack: wrote {:?} in {:.2}s — items={} metadata={} illustrations={} redirects={}",
            cli.output,
            elapsed.as_secs_f64(),
            counts.2,
            counts.0,
            counts.1,
            counts.3
        );
    }
    Ok(())
}
