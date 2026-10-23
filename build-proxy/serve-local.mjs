// Run the build worker on Node for local tests (127.0.0.1 only):
//   SITE_ORIGIN=http://127.0.0.1:8000 UPSTREAM=https://archive.org/download/ \
//     node build-proxy/serve-local.mjs [port]                   (default 8767)
// SZ_STATS=1 prints one JSON line per /merged/ request (parts, subrequests,
// bytes, milliseconds, Node CPU microseconds); SZ_LOG=1 one line per request
// of any kind ({"log": method, path, status, at}), preflights included.
import http from 'node:http';
import { Readable } from 'node:stream';
import { createMergedHandler } from './merged-webseed.js';
import { handleRequest } from '../preview-proxy/ia-range-proxy.js';

const port = Number(process.argv[2] || process.env.PORT || 8767);
const wantStats = process.env.SZ_STATS === '1';
const wantLog = process.env.SZ_LOG === '1';
const merged = createMergedHandler({
  site: process.env.SITE_ORIGIN, upstream: process.env.UPSTREAM,
  stats: wantStats ? (ev) => process.stdout.write(JSON.stringify(ev) + '\n') : null,
});

const server = http.createServer(async (req, res) => {
  const headers = new Headers();
  for (const name of ['range', 'if-range', 'origin']) {
    const v = req.headers[name];
    if (typeof v === 'string') headers.set(name, v);
  }
  const request = new Request(`http://127.0.0.1:${port}${req.url}`, { method: req.method, headers });
  const cpu0 = process.cpuUsage();
  let response;
  try {
    response = req.url.startsWith('/merged/') ? await merged(request, null) : await handleRequest(request);
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'text/plain' });
    res.end('worker error: ' + (err && err.stack || err) + '\n');
    return;
  }
  const out = {};
  response.headers.forEach((value, name) => { out[name] = value; });
  res.writeHead(response.status, out);
  if (wantLog) process.stdout.write(JSON.stringify({ log: req.method, path: req.url.split('?')[0], status: response.status, at: Date.now() }) + '\n');
  if (!response.body || req.method === 'HEAD') { res.end(); return; }
  const s = Readable.fromWeb(response.body);
  s.on('error', () => res.destroy());
  s.pipe(res);
  if (wantStats) res.on('close', () => {
    const c = process.cpuUsage(cpu0);
    process.stdout.write(JSON.stringify({ url: req.url, range: req.headers.range || '', cpu_us: c.user + c.system }) + '\n');
  });
});

server.listen(port, '127.0.0.1', () => {
  console.log(`streetzim build worker on http://127.0.0.1:${port} (site ${process.env.SITE_ORIGIN || '-'}, upstream ${process.env.UPSTREAM || 'archive.org'})`);
});
