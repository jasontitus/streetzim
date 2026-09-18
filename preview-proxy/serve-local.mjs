// Run the range proxy on Node for local development and the smoke test:
//   node preview-proxy/serve-local.mjs [port]      (default 8766)
// Then point web/drive/preview-config.js (or a test copy of it) at
// http://127.0.0.1:<port>.
//
// With H2_CERT and H2_KEY set (PEM files) it serves HTTPS with HTTP/2
// instead, which is what a deployed worker speaks: browsers multiplex
// HTTP/2 but open at most 6 HTTP/1.1 connections per host, and the
// viewer's parallel lookups are shaped by that difference.
import fs from 'node:fs';
import http from 'node:http';
import http2 from 'node:http2';
import { nodeAdapter } from './node-adapter.mjs';

const port = Number(process.argv[2] || process.env.PORT || 8766);
const cert = process.env.H2_CERT, key = process.env.H2_KEY;

const server = (cert && key)
  ? http2.createSecureServer(
      { cert: fs.readFileSync(cert), key: fs.readFileSync(key), allowHTTP1: true },
      (req, res) => nodeAdapter(req, res, 'https://127.0.0.1:' + port))
  : http.createServer((req, res) => nodeAdapter(req, res, 'http://127.0.0.1:' + port));

server.listen(port, '127.0.0.1', () => {
  console.log('streetzim preview proxy on ' + (cert ? 'https' : 'http') + '://127.0.0.1:' +
              port + '/<item>/<file>.zim  \u2192  https://archive.org/download/' +
              (cert ? '  (HTTP/2)' : ''));
});
