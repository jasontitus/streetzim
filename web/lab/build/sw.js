// Service worker of the language builder (/lab/build/ only): a streamed
// download. The page registers {id, filename, size} with a MessagePort and
// opens <scope>__dl/<id> in a hidden iframe; this answers with a
// ReadableStream fed from the port (one 'ack' per chunk it has room for),
// Content-Disposition: attachment and the known Content-Length, so the
// browser's own download manager saves the file as it is assembled.
// Everything else goes to the network untouched.
//
// Port protocol (assembler-worker.js is the other end):
//   in:  chunk {chunk} | end | error {reason} | ping
//   out: started | ack | pulled {bytes} | drained | cancelled {reason} | pong
// 'started' means the browser has asked for the download (the page waits
// for it, and offers a "Start download" button if it does not come).
// 'pulled' counts the bytes the browser has read from the stream; 'drained'
// means it has read all of them. A download that does not start within
// START_TIMEOUT_MS (the iframe was blocked or never loaded) is cancelled; one
// whose writer goes silent for SILENT_MS (it pings every 5 s) is failed, so
// the browser does not keep a half-finished download open forever.
const VERSION = 4;
const streams = new Map();            // id -> {stream, filename, size, started}
const HWM = 64 * 1048576;             // bytes held here before it stops acking (the page may ask
                                      // for less: {hwm} at registration, 16 MiB on a small device)
const START_TIMEOUT_MS = 20000;
const SILENT_MS = 30000;

// An update does not take over by itself: the page asks for it
// ({type:'skipWaiting'}) only when the running version has no download in
// progress ({type:'status'} -> busy), so a new version never cuts off a stream.
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('message', (e) => {
  const d = e.data || {};
  if (d.type === 'keepalive') return;                 // the event itself keeps this worker running
  if (d.type === 'status') { if (e.ports[0]) e.ports[0].postMessage({ type: 'status', v: VERSION, busy: streams.size }); return; }
  if (d.type === 'skipWaiting') { self.skipWaiting(); return; }
  if (d.type !== 'register' || !e.ports[0]) return;
  const port = e.ports[0];
  const hwm = Math.min(HWM, Math.max(1048576, +d.hwm || HWM));
  let ctrl, waiting = 0, enqueued = 0, ended = false, lastPulled = -1, startTimer = null, heard = Date.now();
  const queued = () => hwm - ctrl.desiredSize;
  const forget = () => { streams.delete(d.id); clearTimeout(startTimer); clearInterval(silence); };
  const silence = setInterval(() => {
    if (ended || Date.now() - heard < SILENT_MS) return;
    forget();
    try { ctrl.error(new Error('the page stopped writing')); } catch (err) { /* closed */ }
  }, 5000);
  const report = () => {
    const pulled = enqueued - queued();
    if (pulled !== lastPulled) { lastPulled = pulled; port.postMessage({ type: 'pulled', bytes: pulled }); }
    if (ended && queued() === 0) { ctrl.close(); forget(); port.postMessage({ type: 'drained' }); }
  };
  const stream = new ReadableStream({
    start(c) { ctrl = c; },
    pull() {
      if (waiting) { waiting--; port.postMessage({ type: 'ack' }); }
      report();
    },
    cancel(reason) { forget(); port.postMessage({ type: 'cancelled', reason: String(reason || 'cancelled') }); },
  }, new ByteLengthQueuingStrategy({ highWaterMark: hwm }));
  port.onmessage = (m) => {
    const x = m.data || {};
    heard = Date.now();
    if (x.type === 'chunk') {
      ctrl.enqueue(x.chunk); enqueued += x.chunk.byteLength;
      if (ctrl.desiredSize > 0) port.postMessage({ type: 'ack' }); else waiting++;
    } else if (x.type === 'end') { ended = true; report(); }
    else if (x.type === 'error') { try { ctrl.error(new Error(x.reason)); } catch (err) { /* closed */ } forget(); }
    else if (x.type === 'ping') port.postMessage({ type: 'pong' });
  };
  const s = { stream, filename: d.filename, size: d.size, started: false, port };
  startTimer = setTimeout(() => {
    if (s.started) return;
    forget();
    try { ctrl.error(new Error('not started')); } catch (err) { /* closed */ }
    port.postMessage({ type: 'cancelled', reason: 'the download did not start' });
  }, START_TIMEOUT_MS);
  streams.set(d.id, s);
  port.postMessage({ type: 'registered', v: VERSION });
});

self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (u.origin !== self.location.origin) return;
  const prefix = new URL(self.registration.scope).pathname + '__dl/';
  if (!u.pathname.startsWith(prefix)) return;
  const s = streams.get(decodeURIComponent(u.pathname.slice(prefix.length)));
  if (!s || s.started) { e.respondWith(new Response('This download is no longer running.', { status: 404 })); return; }
  s.started = true;
  s.port.postMessage({ type: 'started' });
  e.respondWith(new Response(s.stream, { headers: {
    'Content-Type': 'application/octet-stream',
    'Content-Disposition': `attachment; filename="${s.filename.replace(/["\\\r\n]/g, '')}"`,
    'Content-Length': String(s.size),
    'Cache-Control': 'no-store',
  } }));
});
