// Differential comparison: run each route twice (default optimized
// path + ?route=full forced single-pass admissible A*) and print
// distances side-by-side so we can see where greedy/two-pass costs
// us accuracy vs how much it saves on time + memory.
//
// Usage:
//   ZIM_URL=http://localhost:8765/osm-japan-chips-v2.zim ROUTES=japan \
//     node cloud/route_compare.mjs
//
// Output: one row per route with default-distance, full-distance,
// and the delta as a percentage. Wall + peak heap reported per mode.

import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const harness = join(here, 'route_browser_test.mjs');

function run(env) {
  return new Promise((resolve, reject) => {
    const proc = spawn('node', [harness], { env: { ...process.env, ...env }, stdio: 'pipe' });
    let stdout = '', stderr = '';
    proc.stdout.on('data', d => { stdout += d; process.stderr.write(d); });
    proc.stderr.on('data', d => { stderr += d; process.stderr.write(d); });
    proc.on('close', code => resolve({ code, stdout, stderr }));
    proc.on('error', reject);
  });
}

function parseSummary(stdout) {
  // Grab summary lines. Format from harness:
  //   "  [PASS]  Tokyo → Oita (~830 km)  → 1174.3 km  (41.8s, peak 583MB / 4 cells)"
  // We split on the LAST "  → " before the result so labels with
  // their own "→" in the name (Tokyo → Oita) don't trip the parser.
  const out = [];
  for (const line of stdout.split('\n')) {
    const tag = (line.match(/^\s*\[(PASS|FAIL)\]/) || [])[1];
    if (!tag) continue;
    const stat = line.match(/\(([\d.]+s),\s+peak\s+(\d+)MB\s+\/\s+(\d+)\s+cells\)\s*$/);
    if (!stat) continue;
    const [, wall, heap, cells] = stat;
    // Drop the leading "  [PASS]  " and the trailing "(stats)" —
    // anchor the trailing parens to the last set only so route labels
    // with their own (~30 km) hint don't get clobbered.
    const inner = line.replace(/^\s*\[\w+\]\s+/, '').replace(/\s+\([^)]*\)\s*$/, '');
    const lastArrow = inner.lastIndexOf('→');
    const label = inner.slice(0, lastArrow).trim();
    const distText = inner.slice(lastArrow + 1).trim();
    const km = parseFloat((distText.match(/^([\d.]+)\s*km/) || [, '0'])[1]);
    const mtrs = parseFloat((distText.match(/^([\d.]+)\s*m\b/) || [, '0'])[1]);
    out.push({
      tag, label, dist: km > 0 ? km : (mtrs > 0 ? mtrs / 1000 : 0),
      distText, wall, heap: +heap, cells: +cells,
    });
  }
  return out;
}

async function main() {
  console.log('=== mode: default (two-pass + greedy) ===');
  const a = parseSummary((await run({ MODE: 'default' })).stdout);
  console.log('\n\n=== mode: full (single-pass admissible) ===');
  const b = parseSummary((await run({ MODE: 'full' })).stdout);

  console.log('\n\n=== comparison ===');
  console.log('label                                          default       full        Δ%   default-wall full-wall');
  for (let i = 0; i < a.length; i++) {
    const d = a[i], f = b[i];
    if (!d || !f) {
      console.log('  (missing pair at row ' + i + ')');
      continue;
    }
    const delta = f.dist > 0 ? ((d.dist - f.dist) / f.dist) * 100 : 0;
    console.log(
      ' ' + (d.label || '').slice(0, 45).padEnd(46) +
      d.distText.padStart(10) + '  ' +
      f.distText.padStart(10) + '   ' +
      (delta >= 0 ? '+' : '') + delta.toFixed(1).padStart(5) + '%   ' +
      d.wall.padStart(8) + '   ' + f.wall.padStart(8)
    );
  }
}

main().catch(err => { console.error(err); process.exit(2); });
