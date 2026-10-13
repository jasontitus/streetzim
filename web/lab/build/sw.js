// Service worker of the language builder (/lab/build/ only): a streamed
// download. The page registers {id, filename, size} with a MessagePort and
// opens /lab/build/__dl/<id>; this answers with a ReadableStream fed from the
// port (one 'ack' per chunk it has room for), Content-Disposition:
// attachment and the known Content-Length, so the browser's own download
// manager saves the file as it is assembled. Everything else goes to the
// network untouched.
const streams = new Map();
const HWM = 64 * 1048576;            // bytes held here before it stops acking

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('message', (e) => {
  const d = e.data || {};
  if (d.type !== 'register') return;
  const port = e.ports[0];
  let ctrl, waiting = 0;
  const stream = new ReadableStream({
    start(c) { ctrl = c; },
    pull() { if (waiting) { waiting--; port.postMessage({ type: 'ack' }); } },
    cancel(reason) { port.postMessage({ type: 'cancelled', reason: String(reason) }); streams.delete(d.id); },
  }, new ByteLengthQueuingStrategy({ highWaterMark: HWM }));
  port.onmessage = (m) => {
    const x = m.data;
    if (x.type === 'chunk') {
      ctrl.enqueue(x.chunk);
      if (ctrl.desiredSize > 0) port.postMessage({ type: 'ack' }); else waiting++;
    } else if (x.type === 'end') { ctrl.close(); streams.delete(d.id); }
    else if (x.type === 'error') { ctrl.error(new Error(x.reason)); streams.delete(d.id); }
  };
  streams.set(d.id, { stream, filename: d.filename, size: d.size });
  port.postMessage({ type: 'registered' });
});

self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  const at = u.pathname.indexOf('/__dl/');
  if (at < 0) return;
  const s = streams.get(decodeURIComponent(u.pathname.slice(at + 6)));
  if (!s) { e.respondWith(new Response('This download is no longer running.', { status: 404 })); return; }
  e.respondWith(new Response(s.stream, { headers: {
    'Content-Type': 'application/octet-stream',
    'Content-Disposition': `attachment; filename="${s.filename.replace(/["\\]/g, '')}"`,
    'Content-Length': String(s.size),
    'Cache-Control': 'no-store',
  } }));
});
