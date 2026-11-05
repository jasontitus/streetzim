// Parse glyph range files (fonts/<stack>/<start>-<end>.pbf) with the glyph
// parsers of the vendored MapLibre GL JS builds themselves, not a
// re-implementation: maps carry MapLibre 6 (maplibre-gl.js) and MapLibre 5
// (maplibre-gl-legacy.js, for engines 6 cannot run in), and both must read
// every range. The parser is the function shaped
// `function(t){return new Pbf(t).readFields(readFontstacks,[])}`, found by
// that shape so an upgrade that renames the minified symbols still finds
// it: in MapLibre 5 among the exports of the bundle's "shared" module
// (evaluated on its own; it needs no DOM), in MapLibre 6 inside the one
// classic script tools/pin_viewer_assets.py makes, which is evaluated with
// the function handed out. Each parser builds each glyph's AlphaImage,
// which throws on a bitmap whose length is not (width+6)*(height+6).
//
//   node tests/lib/maplibre_glyph_parse.mjs a.pbf b.pbf ...
//
// Prints one JSON line per file: {file, ids, n, error}. Exit status 1 if
// any file failed to parse, or the two builds read it differently. Used by
// tests/test_glyph_fallback_corners.py.
import fs from 'node:fs';

const REPO = new URL('../..', import.meta.url).pathname.replace(/\/$/, '');
const SHAPE = /^function\s*[\w$]*\([\w$]+\)\{return new [\w$]+\([\w$]+\)\.readFields\([\w$]+,\[\]\)\}$/;

function parser5(js) {
  const line = js.split('\n').find((l) => l.startsWith('define("shared"'));
  if (!line) throw new Error('maplibre-gl-legacy.js: no define("shared", ...) chunk');
  const factory = (0, eval)(line.slice(line.indexOf('(function'), line.lastIndexOf(')')));
  const shared = {};
  factory(shared);
  const found = Object.values(shared).filter((f) => typeof f === 'function' && SHAPE.test(String(f)));
  if (found.length !== 1) throw new Error(`MapLibre 5: expected one glyph parser export, found ${found.length}`);
  return found[0];
}

function parser6(js) {
  const named = [...js.matchAll(/function ([\w$]+)\(([\w$]+)\)\{return new [\w$]+\(\2\)\.readFields\([\w$]+,\[\]\)\}/g)];
  if (named.length !== 1) throw new Error(`MapLibre 6: expected one glyph parser, found ${named.length}`);
  const marker = '\nvar __maplibreApi = {';
  if (js.split(marker).length !== 2) throw new Error('maplibre-gl.js: not the converted MapLibre 6 script');
  (0, eval)(js.replace(marker, `\nglobalThis.__szGlyphParse = ${named[0][1]};${marker}`));
  const parse = globalThis.__szGlyphParse;
  if (typeof parse !== 'function' || !SHAPE.test(String(parse))) throw new Error('MapLibre 6: glyph parser not reached');
  return parse;
}

const vendor = `${REPO}/resources/vendor`;
const parsers = [
  ['MapLibre 6', parser6(fs.readFileSync(`${vendor}/maplibre-gl/maplibre-gl.js`, 'utf8'))],
  ['MapLibre 5', parser5(fs.readFileSync(`${vendor}/maplibre-gl-legacy/maplibre-gl-legacy.js`, 'utf8'))],
];

function read(parse, file) {
  const glyphs = parse(new Uint8Array(fs.readFileSync(file)));
  for (const g of glyphs) {
    const m = g.metrics;
    if (g.bitmap.width !== m.width + 6 || g.bitmap.height !== m.height + 6) {
      throw new Error(`glyph ${g.id}: bitmap ${g.bitmap.width}x${g.bitmap.height} for ${m.width}x${m.height}`);
    }
    for (const k of ['width', 'height', 'left', 'top', 'advance']) {
      if (!Number.isInteger(m[k])) throw new Error(`glyph ${g.id}: ${k} is ${m[k]}`);
    }
  }
  return glyphs.map((g) => g.id);
}

let failed = 0;
for (const file of process.argv.slice(2)) {
  try {
    const seen = parsers.map(([name, parse]) => {
      try { return read(parse, file); } catch (e) { throw new Error(`${name}: ${e && e.message || e}`); }
    });
    if (JSON.stringify(seen[0]) !== JSON.stringify(seen[1])) {
      throw new Error(`MapLibre 6 reads ${seen[0].length} glyphs, MapLibre 5 ${seen[1].length}`);
    }
    console.log(JSON.stringify({ file, n: seen[0].length, ids: seen[0] }));
  } catch (e) {
    failed++;
    console.log(JSON.stringify({ file, error: String(e && e.message || e) }));
  }
}
process.exitCode = failed ? 1 : 0;
