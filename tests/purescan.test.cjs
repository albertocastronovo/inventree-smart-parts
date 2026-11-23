// Run from the repository root with: node --test tests/purescan.test.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname,
  '../inventree_smart_parts/static/inventree_smart_parts/ui/purescan.js'), 'utf8');

function terminal({ last = 9, found = true, locationStock = '180', deferred, lookup = {}, createpart } = {}) {
  let now = 0;
  let timerId = 0;
  let current;
  const timers = new Map();
  const calls = [];
  const local = new Map();
  if (last) local.set('purescan_last_location', JSON.stringify({ id: last, name: 'Bin' }));
  const response = (data, status = 200) => ({
    status, ok: status < 400, json: async () => data,
  });
  const window = { localStorage: { getItem: key => local.get(key), setItem: (k, v) => local.set(k, v) } };
  const context = {
    window, URLSearchParams, console,
    document: { addEventListener() {}, removeEventListener() {}, getElementById: () => ({ value: 'csrf' }) },
    Date: class extends Date { static now() { return now; } },
    setTimeout(fn, ms) { timers.set(++timerId, { fn, when: now + ms }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      const target = new URL(url, 'https://inventory.test');
      const body = options?.body ? JSON.parse(options.body) : null;
      calls.push({ target, body });
      const endpoint = target.pathname;
      if (endpoint.endsWith('/resolve/')) return response({}, 400);
      if (endpoint.endsWith('/lookup/')) return response({
        found, part_id: found ? 4 : null, mpn: 'RES-10K', part_name: 'Resistor',
        barcode_data: { mpn: 'RES-10K', quantity: 20 }, ...lookup,
      });
      // `createpart` may be a response body or a promise of one (deferred)
      if (endpoint.endsWith('/createpart/') && createpart) return response(await createpart);
      if (endpoint.endsWith('/stock-summary/')) {
        if (deferred) await deferred;
        return response({ total_stock: '420', location_stock: last ? locationStock : null,
          stock_labels: last && locationStock !== '0' ? ['STOCK_1A2B', 'STOCK_3C4D', 'STOCK_5E6F'] : [],
          more_labels: locationStock !== '0' });
      }
      if (endpoint.endsWith('/token/')) return response(body.token === 'STOCK_USED'
        ? { type: 'stockitem', id: 12, part_id: 4, location_id: 9 }
        : { type: 'free' });
      if (endpoint.endsWith('/createstock/') || endpoint === '/api/stock/add/') {
        return response({ success: true });
      }
      assert.fail(`Unexpected request: ${url}`);
    },
  };
  vm.runInNewContext(script, context);
  const scan = window.PureScan.handleScan;
  window.PureScan.init(state => { current = state; });
  return {
    scan, calls, current: () => current,
    advance(ms) {
      now += ms;
      for (const [id, timer] of timers) {
        if (timer.when <= now) { timers.delete(id); timer.fn(); }
      }
    },
  };
}

const REEL = '[)>\x1e06\x1d1PRES-10K\x1dQ20\x1e\x04';

test('FASTADD continues at 29 seconds; a merge restarts the window', async () => {
  const t = terminal();
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  await t.scan('STOCK_1A2B');
  assert.match(t.current().detail, /within 30s/);
  t.advance(29000);
  await t.scan(REEL);
  await t.scan('STOCK_USED');
  assert.match(t.current().detail, /Merged into existing instance/);
  assert.ok(t.calls.some(c => c.target.pathname === '/api/stock/add/' && c.body.items[0].quantity === 20));
  t.advance(29000);
  assert.equal(t.current().state, 'SUCCESS');
  t.advance(1001);
  assert.equal(t.current().state, 'IDLE');
  await t.scan(REEL);
  assert.match(t.current().detail, /Distributor label/);
});

test('SYS:LAST disarms an active receive burst without a stale timer', async () => {
  const t = terminal();
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  await t.scan('STOCK_1A2B');
  await t.scan('SYS:LAST');
  t.advance(31000);
  assert.equal(t.current().state, 'AWAITING_LOCATION');
  assert.equal(t.current().action, 'LAST');
});

test('explicit INFO shows exact location count and truncated label list', async () => {
  const t = terminal();
  await t.scan('SYS:INFO');
  await t.scan(REEL);
  assert.equal(t.current().detail, 'Total stock: 420\nIn location: 180 [STOCK_1A2B, STOCK_3C4D, STOCK_5E6F, ...]');
  const summary = t.calls.find(c => c.target.pathname.endsWith('/stock-summary/'));
  assert.equal(summary.target.searchParams.get('part_id'), '4');
  assert.equal(summary.target.searchParams.get('location_id'), '9');
});

test('INFO handles no saved location, zero local stock, and an unknown part', async () => {
  for (const [options, expected] of [
    [{ last: null }, /^Total stock: 420$/],
    [{ locationStock: '0' }, /^Total stock: 420\nIn location: 0$/],
    [{ found: false }, /Part in DB: NO/],
  ]) {
    const t = terminal(options);
    await t.scan('SYS:INFO');
    await t.scan(REEL);
    assert.match(t.current().detail, expected);
    assert.ok(!t.calls.some(c => c.target.pathname.endsWith('/createpart/')));
  }
});

test('late INFO response cannot replace a new command', async () => {
  let finish;
  const t = terminal({ deferred: new Promise(resolve => { finish = resolve; }) });
  await t.scan('SYS:INFO');
  const info = t.scan(REEL);
  for (let i = 0; i < 20 && !t.calls.some(c => c.target.pathname.endsWith('/stock-summary/')); i++) {
    await Promise.resolve();
  }
  assert.ok(t.calls.some(c => c.target.pathname.endsWith('/stock-summary/')));
  await t.scan('SYS:FASTADD');
  finish();
  await info;
  assert.equal(t.current().state, 'AWAITING_ITEM');
  assert.equal(t.current().action, 'FASTADD');
});

const MOUSER_GAP = { distributor: 'mouser', distributor_supplier_part: false };

test('FASTADD gap-fills a known part from its Mouser label and shows warnings', async () => {
  const t = terminal({
    lookup: MOUSER_GAP,
    createpart: {
      success: false, already_exists: true, part_id: 4, part_name: 'Resistor',
      enriched: ['Mouser supplier part 81-RES10K'],
      enrich_errors: ['Supplier pricing skipped for Mouser 81-RES10K: Missing rate'],
    },
  });
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  const fills = t.calls.filter(c => c.target.pathname.endsWith('/createpart/'));
  assert.equal(fills.length, 1);
  assert.equal(fills[0].body.enrich_scope, 'label');
  assert.equal(t.current().state, 'AWAITING_LOCATION');
  assert.match(t.current().detail, /✓ Filled: Mouser supplier part 81-RES10K/);
  assert.match(t.current().detail, /⚠ Supplier pricing skipped for Mouser 81-RES10K/);
});

test('a cancel during the FASTADD gap-fill is not overwritten by its late response', async () => {
  let finish;
  const t = terminal({ lookup: MOUSER_GAP, createpart: new Promise(resolve => { finish = resolve; }) });
  await t.scan('SYS:FASTADD');
  const pending = t.scan(REEL);
  for (let i = 0; i < 100 && !t.calls.some(c => c.target.pathname.endsWith('/createpart/')); i++) {
    await Promise.resolve();
  }
  assert.ok(t.calls.some(c => c.target.pathname.endsWith('/createpart/')));
  await t.scan('SYS:CANCEL');
  finish({ success: false, already_exists: true, part_id: 4, enriched: ['Mouser supplier part X'] });
  await pending;
  assert.equal(t.current().state, 'IDLE');
});
