// Parse glyph range files (fonts/<stack>/<start>-<end>.pbf) with the glyph
// parser of the vendored MapLibre GL JS itself, not a re-implementation:
// the bundle's "shared" module is evaluated on its own (it needs no DOM)
// and the export that reads a glyph PBF is picked by its shape,
// `function(t){return new Pbf(t).readFields(readFontstacks,[])}`, so a
// MapLibre upgrade that renames the minified symbols still finds it. The
// parser builds each glyph's AlphaImage, which throws on a bitmap whose
// length is not (width+6)*(height+6).
//
//   node tests/lib/maplibre_glyph_parse.mjs a.pbf b.pbf ...
//
// Prints one JSON line per file: {file, ids, n, error}. Exit status 1 if
// any file failed to parse. Used by tests/test_glyph_fallback_corners.py.
import fs from 'node:fs';

const REPO = new URL('../..', import.meta.url).pathname.replace(/\/$/, '');
const js = fs.readFileSync(`${REPO}/resources/vendor/maplibre-gl/maplibre-gl.js`, 'utf8');
const line = js.split('\n').find((l) => l.startsWith('define("shared"'));
if (!line) throw new Error('maplibre-gl.js: no define("shared", ...) chunk');
const factory = (0, eval)(line.slice(line.indexOf('(function'), line.lastIndexOf(')')));
const shared = {};
factory(shared);
const SHAPE = /^function\(\w+\)\{return new \w+\(\w+\)\.readFields\(\w+,\[\]\)\}$/;
const parsers = Object.values(shared).filter((f) => typeof f === 'function' && SHAPE.test(String(f)));
if (parsers.length !== 1) throw new Error(`expected one glyph parser export, found ${parsers.length}`);
const parse = parsers[0];

let failed = 0;
for (const file of process.argv.slice(2)) {
  try {
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
    console.log(JSON.stringify({ file, n: glyphs.length, ids: glyphs.map((g) => g.id) }));
  } catch (e) {
    failed++;
    console.log(JSON.stringify({ file, error: String(e && e.message || e) }));
  }
}
process.exitCode = failed ? 1 : 0;
