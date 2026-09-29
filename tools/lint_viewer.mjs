#!/usr/bin/env node
// Lint the viewer JavaScript with ESLint (config: eslint.config.mjs).
//
//   npm run lint:viewer          (CI runs this in the `checks` job)
//
// - resources/viewer/routing-worker.js is linted as a file.
// - index.html is joined from resources/viewer/src/index/* (as
//   tools/build_viewer.py does) and places.html is read as is; each page's
//   inline <script> blocks are linted together as one program (classic
//   scripts share the page's global scope), with everything outside them
//   blanked to spaces so line and column numbers are unchanged. Findings in
//   index.html are reported against the part file they come from.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { ESLint } from "eslint";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const VIEWER = join(ROOT, "resources", "viewer");
const PARTS = join(VIEWER, "src", "index");

// Keep the inline, JavaScript <script> blocks; blank everything else.
function scriptsOnly(html) {
  const out = html.replace(/[^\n]/g, " ").split("");
  const re = /<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi;
  for (let m; (m = re.exec(html)); ) {
    const attrs = m[1] || "";
    if (/\bsrc\s*=/i.test(attrs)) continue;
    const type = /\btype\s*=\s*["']?([^"'\s>]+)/i.exec(attrs);
    if (type && !/^(text\/javascript|application\/javascript)$/i.test(type[1])) continue;
    const start = m.index + m[0].indexOf(">") + 1;
    for (let i = 0; i < m[2].length; i++) out[start + i] = html[start + i];
  }
  return out.join("");
}

// index.html line -> [part file, line in part]
function partLocator(parts) {
  const starts = [];
  let line = 1;
  for (const p of parts) {
    starts.push([line, p.name]);
    line += (p.text.match(/\n/g) || []).length;
  }
  return (l) => {
    let hit = starts[0];
    for (const s of starts) if (s[0] <= l) hit = s;
    return [relative(ROOT, join(PARTS, hit[1])), l - hit[0] + 1];
  };
}

const parts = readdirSync(PARTS)
  .filter((n) => /^\d{3}-[\w-]+\.(html|js)$/.test(n))
  .sort()
  .map((name) => ({ name, text: readFileSync(join(PARTS, name), "utf8") }));

const eslint = new ESLint({ cwd: ROOT });
const jobs = [
  {
    text: scriptsOnly(parts.map((p) => p.text).join("")),
    filePath: join(VIEWER, "index.html.js"),
    locate: partLocator(parts),
  },
  {
    text: scriptsOnly(readFileSync(join(VIEWER, "places.html"), "utf8")),
    filePath: join(VIEWER, "places.html.js"),
    locate: (l) => ["resources/viewer/places.html", l],
  },
  {
    text: readFileSync(join(VIEWER, "routing-worker.js"), "utf8"),
    filePath: join(VIEWER, "routing-worker.js"),
    locate: (l) => ["resources/viewer/routing-worker.js", l],
  },
];

let problems = 0;
for (const job of jobs) {
  // A page whose <script> blocks were not found would pass vacuously.
  if (job.text.replace(/\s/g, "").length < 1000) {
    console.log(`${relative(ROOT, job.filePath)}: no inline JavaScript found to lint`);
    process.exit(1);
  }
  const [result] = await eslint.lintText(job.text, { filePath: job.filePath });
  for (const m of result.messages) {
    problems++;
    const [file, line] = job.locate(m.line || 1);
    const sev = m.severity === 2 ? "error" : "warning";
    console.log(`${file}:${line}:${m.column || 1}: ${sev}: ${m.message} (${m.ruleId || "parse"})`);
  }
}
if (problems) {
  console.log(`\n${problems} problem(s)`);
  process.exit(1);
}
console.log(`ok: ${jobs.length} viewer scripts pass ESLint (index.html from ${parts.length} parts)`);
