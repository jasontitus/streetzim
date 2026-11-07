import assert from 'node:assert/strict';
import fs from 'node:fs';
import { test } from 'node:test';

const read = (path) => fs.readFileSync(new URL('../' + path, import.meta.url), 'utf8');
function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

function drive() {
  const events = {};
  let revision = 0;
  const state = { record: null, revision: null };
  const self = {
    addEventListener(type, handler) { events[type] = handler; },
    clients: { async matchAll() { return []; } },
    StreetZimReader: class {
      constructor(source) { this.file = source; this.info = { name: this.file.name }; }
      async open() { await this.file.opening?.promise; if (this.file.error) throw this.file.error; }
      static normalizePath(path) { return path; }
      async findEntry() { return { mime: 'text/plain' }; }
      async rawBlobSpan() { return null; }
      async readEntry() {
        this.file.readStarted?.resolve();
        await this.file.reading?.promise;
        return { mime: 'text/plain', data: new TextEncoder().encode(this.file.name) };
      }
    },
  };
  new Function('self', 'state', 'importScripts', 'crypto', read('web/drive/sw.js') + `
    inStore = async (mode, action) => {
      if (mode === 'readwrite' && state.writeError) throw state.writeError;
      const store = {
        get(id) {
          const request = { result: id === 'current' ? state.record : state.revision };
          queueMicrotask(() => request.onsuccess?.());
          return request;
        },
        put(rec) { if (rec.id === 'current') state.record = rec; else state.revision = rec; },
        delete() { state.record = null; }
      };
      const result = action(store);
      await Promise.resolve();
      return result();
    };
    state.entry = () => serveFromZim('data.json', new Request('https://example.test/data.json'));
  `)(self, state, () => {}, { getRandomValues: (buf) => { buf[0] = ++revision; return buf; } });
  state.ask = (data) => new Promise((resolve, reject) => {
    events.message({ data, waitUntil(p) { p.catch(reject); }, ports: [{ postMessage: resolve }] });
  });
  state.set = (file, session = false) => state.ask({ type: 'set-zim', blob: file, name: file.name, session });
  return state;
}

test('a new map never joins an entry read still in flight from the previous map', async () => {
  const sw = drive();
  const a = { name: 'A', readStarted: deferred(), reading: deferred() };
  await sw.set(a);
  const oldRead = sw.entry();
  await a.readStarted.promise;
  await sw.set({ name: 'B' });
  const newRead = sw.entry();
  // Let B get through findEntry and the raw-span probe before releasing A.
  await new Promise(setImmediate);
  a.reading.resolve();
  assert.equal(await (await oldRead).text(), 'A');
  assert.equal(await (await newRead).text(), 'B');
});

test('clear received during a replacement is the last selection mutation', async () => {
  const sw = drive();
  const opening = deferred();
  const first = sw.set({ name: 'A', opening });
  const clear = sw.ask({ type: 'clear-zim' });
  opening.resolve();
  assert.match((await first).error, /superseded/);
  assert.equal((await clear).ok, true);
  assert.equal((await sw.ask({ type: 'status' })).present, false);
});

test('status uses one selection across a page-side IndexedDB commit and reload', async () => {
  const sw = drive();
  await sw.set({ name: 'A' });
  sw.record = { blob: { name: 'B' }, name: 'B' };
  sw.revision = { value: 'page-pick' };
  const before = await sw.ask({ type: 'status' });
  assert.equal(before.name, before.info.name);
  await sw.ask({ type: 'reload-zim' });
  assert.equal((await sw.ask({ type: 'status' })).name, 'B');
});

test('a late file reload cannot discard a newer session-only map', async () => {
  const sw = drive();
  sw.record = { blob: { name: 'file' }, name: 'file', pickId: 'file-pick' };
  sw.revision = { value: 'file-pick' };
  assert.ok((await sw.set({ name: 'session' }, true)).ok);
  assert.match((await sw.ask({ type: 'reload-zim', pickId: 'file-pick' })).error, /superseded/);
  const status = await sw.ask({ type: 'status' });
  assert.deepEqual([status.loaded, status.name, status.session], [true, 'session', true]);
});

test('an older validating worker pick cannot overwrite a newer page commit', async () => {
  const sw = drive();
  const opening = deferred();
  const first = sw.set({ name: 'old', opening });
  await new Promise(setImmediate);
  sw.record = { blob: { name: 'file' }, name: 'file', pickId: 'file-pick' };
  sw.revision = { value: 'file-pick' };
  assert.ok((await sw.ask({ type: 'reload-zim', pickId: 'file-pick' })).ok);
  opening.resolve();
  assert.match((await first).error, /superseded/);
  assert.equal((await sw.ask({ type: 'status' })).name, 'file');
});

test('a failed replacement retains the session and reload retires its metadata', async () => {
  const sw = drive();
  await sw.set({ name: 'A' }, true);
  sw.writeError = new Error('quota');
  assert.equal((await sw.set({ name: 'B' })).ok, false);
  const status = await sw.ask({ type: 'status' });
  assert.deepEqual([status.loaded, status.present, status.session, status.name], [true, true, true, 'A']);
  sw.record = { blob: { name: 'C' }, name: 'C' };
  await sw.ask({ type: 'reload-zim' });
  sw.record = null;
  await sw.ask({ type: 'reload-zim' });
  assert.equal((await sw.ask({ type: 'status' })).present, false);
});

test('a failed durable delete cannot publish a contradictory session selection', async () => {
  const sw = drive();
  await sw.set({ name: 'A' });
  sw.writeError = new Error('delete failed');
  assert.equal((await sw.set({ name: 'B' }, true)).ok, false);
  const status = await sw.ask({ type: 'status' });
  assert.deepEqual([status.name, status.info.name, status.session], ['A', 'A', false]);
});

test('an unreadable saved map remains removable and can reopen on retry', async () => {
  const sw = drive();
  const file = { name: 'A', error: new Error('temporary') };
  sw.record = { blob: file, name: file.name };
  const bad = await sw.ask({ type: 'status' });
  assert.deepEqual([bad.present, bad.loaded, bad.openError], [true, false, 'temporary']);
  delete file.error;
  assert.equal((await sw.ask({ type: 'status' })).loaded, true);
});

test('a retired source stays removable and cannot serve a cached entry', async () => {
  const sw = drive();
  const file = { name: 'A' };
  assert.ok((await sw.set(file)).ok);
  assert.equal((await sw.entry()).status, 200);
  file.sourceChanged = Object.assign(new Error('remote ZIM changed'), { status: 409 });
  const status = await sw.ask({ type: 'status' });
  assert.deepEqual([status.present, status.loaded, status.openError], [true, false, 'remote ZIM changed']);
  const response = await sw.entry();
  assert.equal(response.status, 502);
  assert.equal(response.headers.get('X-Streetzim-Upstream'), '409');
});

function builder() {
  const events = {};
  const self = {
    addEventListener(type, handler) { events[type] = handler; },
    location: { origin: 'https://example.test' }, registration: { scope: 'https://example.test/build/' },
  };
  new Function('self', 'setInterval', 'clearInterval', read('web/build/sw.js'))(self, () => 1, () => {});
  return {
    register(size) {
      const messages = [];
      const port = { postMessage(message) { messages.push(message); } };
      events.message({ data: { type: 'register', id: 'id', filename: 'map.zim', size }, ports: [port] });
      return { messages, send(data) { port.onmessage({ data }); } };
    },
    start() {
      let response;
      events.fetch({ request: new Request('https://example.test/build/__dl/id'), respondWith(r) { response = r; } });
      return response;
    },
  };
}

test('a cancelled download ignores a chunk already queued on its message port', async () => {
  const sw = builder();
  const stream = sw.register(2);
  await sw.start().body.cancel();
  stream.send({ type: 'chunk', chunk: Uint8Array.of(1) });
  stream.send({ type: 'end' });
  assert.deepEqual(stream.messages.map((m) => m.type).filter((t) => t !== 'pulled'), ['registered', 'started', 'cancelled']);
  assert.equal(sw.start().status, 404);
});

test('a short stream fails instead of reporting drained', async () => {
  const sw = builder();
  const stream = sw.register(2);
  const response = sw.start();
  stream.send({ type: 'chunk', chunk: Uint8Array.of(1) });
  stream.send({ type: 'end' });
  await assert.rejects(response.arrayBuffer(), /length/);
  assert.ok(stream.messages.some((m) => m.type === 'cancelled'));
  assert.ok(!stream.messages.some((m) => m.type === 'drained'));
});

test('a complete stream reports drained once even when end is replayed', async () => {
  const sw = builder();
  const stream = sw.register(2);
  const response = sw.start();
  stream.send({ type: 'chunk', chunk: Uint8Array.of(1, 2) });
  stream.send({ type: 'end' });
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), Uint8Array.of(1, 2));
  stream.send({ type: 'end' });
  assert.equal(stream.messages.filter((m) => m.type === 'drained').length, 1);
});

test('assembly rejects inconsistent plans before opening a sink and still accepts an exact plan', async () => {
  const { createHash, webcrypto } = await import('node:crypto');
  const { performance } = await import('node:perf_hooks');
  const base = Uint8Array.of(1, 2);
  const sha = createHash('sha256').update(base).digest('hex');
  const md5 = createHash('md5').update(base).digest('hex');
  const messages = [];
  let opened = false;
  const worker = new Function('importScripts', 'SZFetch', 'hashwasm', 'crypto', 'performance', 'postMessage',
    'let onmessage;\n' + read('web/build/assembler-worker.js') + '\nreturn onmessage;')(
    () => {}, { newNet: () => ({}), pct: () => null }, {
      async createMD5() {
        let bytes = Buffer.alloc(0);
        return {
          init() {},
          save() { return bytes; },
          load(state) { bytes = state; },
          update(data) { bytes = Buffer.concat([bytes, data]); },
          digest(format) {
            const result = createHash('md5').update(bytes).digest();
            return format === 'binary' ? result : result.toString(format);
          },
        };
      },
    }, webcrypto, performance, (msg) => messages.push(msg));
  const request = {
    cmd: 'run', proto: 7, sink: 'fsa', baseFile: new Blob([base]),
    handle: {
      async createWritable() {
        opened = true;
        return { async write() {}, async close() {}, async abort() {} };
      },
    },
    recipe: {
      format: 'streetzim-recipe/2', size: 18, md5,
      base: { size: 2, chunk: 2, sha256: [sha] }, packs: {},
      segments: [{ src: 'base', off: 0, len: 2 }, { src: 'md5' }],
    },
  };
  for (const [recipe, error] of [
    [{ ...request.recipe, size: 19 }, /size/],
    [{ ...request.recipe, segments: [{ src: 'base', off: 1, len: 2 }, { src: 'md5' }] }, /outside/],
    [{ ...request.recipe, base: { size: 2, chunk: Infinity, sha256: [] } }, /invalid source or chunk size/],
  ]) {
    await worker({ data: { ...request, recipe } });
    const { result } = messages.pop();
    assert.equal(result.ok, false);
    assert.match(result.error, error);
    assert.equal(opened, false);
  }
  await worker({ data: request });
  const { result } = messages.pop();
  assert.equal(result.ok, true);
  assert.equal(result.written, 18);
  assert.equal(opened, true);
});

test('a streaming preview rejects ranges from a changed remote ZIM until reopened', async () => {
  const self = {};
  let etag = '"version-a"';
  let total = 8;
  const fetchRange = async (_url, { headers }) => {
    const [start, end] = headers.Range.slice(6).split('-').map(Number);
    return new Response(new Uint8Array(end - start + 1), { status: 206, headers: {
      'Content-Range': `bytes ${start}-${end}/${total}`, ETag: etag,
    } });
  };
  new Function('self', 'fetch', read('web/drive/zim-reader.js'))(self, fetchRange);
  for (const change of [() => { etag = '"version-b"'; }, () => { total = 12; }]) {
    etag = '"version-a"'; total = 8;
    const source = new self.StreetZimHttpSource('https://example.test/map.zim', { blockSize: 4 });
    await source.open();
    change();
    await assert.rejects(source.openRange(4, 7), (e) => e.status === 409 && /changed/.test(e.message));
    await assert.rejects(source.slice(0, 2).arrayBuffer(), /changed/);
    const reopened = new self.StreetZimHttpSource('https://example.test/map.zim', { blockSize: 4 });
    await reopened.open();
    assert.equal((await reopened.slice(0, 2).arrayBuffer()).byteLength, 2);
  }
});
