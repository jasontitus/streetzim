// Bridges Node's (req, res) to the Fetch-API handler — shared by
// serve-local.mjs and the optional Firebase Functions entry point.
import { Readable } from 'node:stream';
import { handleRequest } from './ia-range-proxy.js';

export async function nodeAdapter(req, res, baseUrl) {
  const headers = new Headers();
  for (const name of ['range', 'if-range', 'origin']) {
    const v = req.headers[name];
    if (typeof v === 'string') headers.set(name, v);
  }
  const request = new Request(baseUrl + req.url, { method: req.method, headers });
  let response;
  try {
    response = await handleRequest(request);
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'text/plain' });
    res.end('proxy error: ' + (err && err.stack || err) + '\n');
    return;
  }
  const out = {};
  response.headers.forEach((value, name) => { out[name] = value; });
  res.writeHead(response.status, out);
  if (!response.body || req.method === 'HEAD') {
    res.end();
    return;
  }
  Readable.fromWeb(response.body).pipe(res);
}
