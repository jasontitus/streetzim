import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import fs from 'node:fs';
import { test } from 'node:test';

const read = (file) => fs.readFileSync(new URL('../' + file, import.meta.url), 'utf8');
function slice(src, from, to) {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  assert.ok(a >= 0 && b > a, from);
  return src.slice(a, b);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((r, j) => { resolve = r; reject = j; });
  return { promise, resolve, reject };
}
function element() {
  return {
    listeners: {}, value: '', style: { display: 'none' },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    blur() { this.listeners.blur?.(); },
    setAttribute() {},
    appendChild() {},
    querySelectorAll() { return []; },
  };
}
function search(routing) {
  const input = element(), resultsEl = element(), clearBtn = element();
  const reads = [], timers = new Map();
  let tick = 0;
  const setTimeout = (fn) => { timers.set(++tick, fn); return tick; };
  const clearTimeout = (id) => timers.delete(id);
  const fetchRows = () => { const read = deferred(); reads.push(read); return read.promise; };
  const map = { _queryPlaces: fetchRows, on() {} };
  const source = routing
    ? slice(read('resources/viewer/src/index/550-routing-pickers.js'), '  function attachTypeahead(', '  var dismissOriginSuggestions')
    : (() => {
      const s = read('resources/viewer/src/index/300-search.js');
      return slice(s, '  var searchSeq =', '  function escapeHtml(') +
        slice(s, '  // Input handler with debounce', '  // Expose map globally');
    })();
  new Function('input', 'resultsEl', 'clearBtn', 'map', 'document', 'setTimeout', 'clearTimeout', 'streamFilterChunks', `
    var activeIdx = -1, searchMarker = null, manifest = {}, chunkCache = {};
    function getPrefixes() { return ['prefix']; }
    function _szCornerQuery() { return null; }
    function szT(key, fallback) { return fallback; }
    const SZ_LANG2 = 'en';
    function _szShownNames(item) { return [item.n]; }
    function szFirstName(name) { return name; }
    function scoreResults(matches) { return matches; }
    function renderResults() { resultsEl.style.display = 'block'; }
    ${source}
    ${routing ? "attachTypeahead(input, resultsEl, 'origin');" : ''}
  `)(input, resultsEl, clearBtn, map, { createElement: element }, setTimeout, clearTimeout, fetchRows);
  return {
    input, resultsEl, reads, clearBtn,
    type(value) { input.value = value; input.listeners.input(); },
    runTimers() { for (const [id, run] of timers) { timers.delete(id); run(); } },
  };
}

for (const routing of [false, true]) {
  const label = routing ? 'route picker' : 'map search';
  const rows = [{ item: { n: 'Result', a: 1, o: 2 } }];
  test(label + ' discards a result during the next input debounce', async () => {
    const box = search(routing);
    box.type('old');
    box.runTimers();
    assert.equal(box.reads.length, 1);
    box.type('new');
    box.reads[0].resolve(rows);
    await box.reads[0].promise;
    assert.equal(box.resultsEl.style.display, 'none');
    box.runTimers();
    assert.equal(box.reads.length, 2);
    assert.equal(box.resultsEl.style.display, 'block');
    box.reads[1].resolve(rows);
    await box.reads[1].promise;
  });

  test(label + ' cannot reopen after Escape during a request', async () => {
    const box = search(routing);
    box.type('query');
    box.runTimers();
    box.input.listeners.keydown({ key: 'Escape' });
    box.reads[0].resolve(rows);
    await box.reads[0].promise;
    assert.equal(box.resultsEl.style.display, 'none');
  });
}

test('clear cancels a queued map search before it makes any requests', () => {
  const box = search(false);
  box.type('query');
  box.clearBtn.listeners.click();
  box.runTimers();
  assert.equal(box.reads.length, 0);
});

test('Find ignores a near-place reply after the field is shortened or dismissed', async () => {
  const src = read('resources/viewer/places.html');
  for (const dismiss of ['shorten', 'Escape']) {
    const near = {
      value: 'Paris', hasAttribute() { return false; }, removeAttribute() {},
    };
    const nearResults = { hidden: true, replaceChildren() {} };
    const reply = deferred();
    const { onNearInput, onNearKeydown } = new Function('els', 'streamLeaves', `
      ${slice(src, 'function hideNearResults()', 'function renderNearResults(')}
      ${slice(src, 'function addOnce(', 'function pickNearResult(')}
      function searchLeavesFor() { return ['prefix']; }
      function foldText(s) { return s.toLowerCase(); }
      function renderNearResults() { els.nearResults.hidden = false; }
      return { onNearInput, onNearKeydown };
    `)({ near, nearResults }, () => reply.promise);
    const old = onNearInput();
    if (dismiss === 'shorten') {
      near.value = 'P';
      await onNearInput();
    } else onNearKeydown({ key: 'Escape' });
    reply.resolve([{ n: 'Paris', a: 48.86, o: 2.35 }]);
    await old;
    assert.equal(nearResults.hidden, true, dismiss);
  }
});

test('OPFS cleanup skips live attempts and retry owns a different file', async () => {
  const source = read('web/build/builder.js');
  const values = new Map(), held = new Set(), files = new Set(), offered = [];
  const dir = {
    async *entries() { for (const file of files) yield [file, { kind: 'file' }]; },
    async removeEntry(name) { files.delete(name); },
  };
  const navigator = {
    locks: {
      async request(name, options, callback) {
        if (typeof options === 'function') callback = options;
        if (held.has(name)) return callback(null);
        held.add(name);
        try { return await callback({ name }); } finally { held.delete(name); }
      },
    },
    storage: {
      async getDirectory() { return { async getDirectoryHandle() { return dir; } }; },
    },
  };
  const localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, val) => values.set(key, val),
    removeItem: (key) => values.delete(key),
  };
  const b = new Function('navigator', 'localStorage', 'crypto', 'offered', `
    let running = null;
    const hex = (buf) => Buffer.from(buf).toString('hex');
    async function opfsRoomError() { return null; }
    ${slice(source, '  async function opfsSink(', '  // Where to write.')}
    ${slice(source, '  const OPFS_DIR =', '  async function offerSave(')}
    async function offerSave(storageName, name) { offered.push([storageName, name]); }
    ${slice(source, '  async function tidyBrowserStorage()', '  // ------------------------------------------------------------ build')}
    return { opfsSink, opfsState, tidyBrowserStorage };
  `)(navigator, localStorage, webcrypto, offered);
  const a = await b.opfsSink('map.zim', 42);
  const retry = await b.opfsSink('map.zim', 42);
  assert.notEqual(a.storageName, retry.storageName);
  files.add(a.storageName);
  files.add(retry.storageName);
  await b.tidyBrowserStorage(); // Between close and publication, the page still owns both.
  assert.deepEqual([...files], [a.storageName, retry.storageName]);
  await retry.cleanup(false);
  b.opfsState(a.storageName, { name: 'map.zim', state: 'ready' });
  await a.cleanup(true);
  await b.tidyBrowserStorage();
  assert.deepEqual([...files], [a.storageName]);
  assert.deepEqual(offered, [[a.storageName, 'map.zim']]);

  const abandoned = await b.opfsSink('other.zim', 42);
  files.add(abandoned.storageName);
  held.clear(); // The owning tab exited before publishing the result.
  await b.tidyBrowserStorage();
  assert.deepEqual([...files], [a.storageName]);
});

test('a stalled OPFS writer fails instead of spinning on zero-byte writes', async () => {
  const src = read('web/build/assembler-worker.js');
  const Sink = new Function(slice(src, 'class OpfsSink', '// ---------------------------------------------------------------- sources') +
    '\nreturn OpfsSink;')();
  const sink = new Sink('test');
  sink.pos = 0;
  let writes = 0;
  sink.h = { write() { return writes++ === 0 ? 0 : 1; } };
  await assert.rejects(sink.write(Uint8Array.of(1)), /stopped accepting/);
  assert.equal(writes, 1);
});

test('the builder releases a pending wake lock after the job ends', async () => {
  const src = read('web/build/builder.js');
  const pending = deferred();
  let requested = 0, released = 0;
  const navigator = { wakeLock: { request() { requested++; return pending.promise; } } };
  const b = new Function('navigator', 'document', `
    let running = {}, packAbort = null;
    ${slice(src, '  let wakeLock =', "  document.addEventListener('visibilitychange', () => { if (busy()")}
    return { holdScreen, finish() { running = null; releaseScreen(); } };
  `)(navigator, { visibilityState: 'visible' });
  const acquired = b.holdScreen();
  b.holdScreen();
  b.finish();
  pending.resolve({ async release() { released++; } });
  await acquired;
  assert.deepEqual([requested, released], [1, 1]);
});

test('the browser build owns its job slot until sink cleanup finishes', async () => {
  const source = read('web/build/builder.js');
  const entered = deferred(), release = deferred();
  const api = new Function('entered', 'release', `
    let running = null, hiddenMs = 0, hiddenSince = null;
    const job = { on: false }, ended = [], nodes = new Map();
    const entry = { lang: 'en', download_bytes: 0 }, region = { id: 'region' };
    const packSeeds = new Map(), baseFile = null, MEM = {}, document = {};
    function $(id) {
      if (!nodes.has(id)) nodes.set(id, { classList: { remove() {} }, textContent: '' });
      return nodes.get(id);
    }
    function showWay() {}
    function setStatus(text) { $('status').textContent = text; }
    function announce() {}
    function jobStart() { job.on = true; }
    function jobEnd(ok) { job.on = false; ended.push(ok); }
    function jobShow() {}
    function holdScreen() {}
    function releaseScreen() {}
    function checkReady() {}
    function track() {}
    function netParams() { return {}; }
    function fmtMB() { return '18 B'; }
    const T = { copying() {} };
    async function recipeFor() { return { name: 'map', size: 18, packs: {} }; }
    async function prepareSink() {
      return { sink: 'sw', transfer: [], cleanup: async () => { entered.resolve(); await release.promise; } };
    }
    class Worker {
      postMessage() { this.onmessage({ data: { type: 'done', result: { ok: true, size: 18, ms: 100 } } }); }
      terminate() {}
    }
    ${slice(source, '  async function build(mode)', "  window.addEventListener('pagehide'")}
    return { build, ended, isRunning: () => !!running };
  `)(entered, release);
  const first = api.build('file');
  await entered.promise;
  assert.equal(api.isRunning(), true);
  assert.deepEqual(api.ended, []);
  await api.build('file');  // An overlapping entry point cannot claim the slot.
  release.resolve();
  await first;
  assert.equal(api.isRunning(), false);
  assert.deepEqual(api.ended, [true]);
});
