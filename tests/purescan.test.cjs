// Run from the repository root with: node --test tests/purescan.test.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname,
  '../inventree_smart_parts/static/inventree_smart_parts/ui/purescan.js'), 'utf8');

const LABELS = [
  { label: 'STOCK_1A2B', quantity: '120' },
  { label: 'STOCK_3C4D', quantity: '40' },
  { label: 'STOCK_5E6F', quantity: '20' },
];

// Stock summary as served by /api/purescan/stock-summary/
function summary({ last = 9, locationStock = '180', labels = LABELS, more = true } = {}) {
  const local = last ? locationStock : null;
  const shown = local && local !== '0' ? labels : [];
  return {
    total_stock: '420',
    location_stock: local,
    stock_labels: shown.map(l => l.label),
    stock_label_items: shown,
    more_labels: shown.length ? more : false,
    mpn: 'RES-10K',
    part_name: 'RES-10K',
    description: 'RES 10K OHM 1% 1/10W 0603',
  };
}

function terminal({
  last = 9, found = true, locationStock = '180', labels, more, deferred,
  lookup = {}, createpart, stockSummary, summaryStatus = 200,
} = {}) {
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
    window, URLSearchParams, console: { ...console, warn() {} },
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
        found, part_id: found ? 4 : null, mpn: 'RES-10K', part_name: 'RES-10K',
        enrich_needed: false, missing_suppliers: [],
        barcode_data: { mpn: 'RES-10K', quantity: 20 }, ...lookup,
      });
      // `createpart` may be a response body or a promise of one (deferred)
      if (endpoint.endsWith('/createpart/') && createpart) return response(await createpart);
      if (endpoint.endsWith('/stock-summary/')) {
        if (deferred) await deferred;
        if (summaryStatus !== 200) return response({ error: 'Boom' }, summaryStatus);
        return response(stockSummary || summary({ last, locationStock, labels, more }));
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
  const t = {
    scan, calls, current: () => current,
    called: suffix => calls.filter(c => c.target.pathname.endsWith(suffix)),
    advance(ms) {
      now += ms;
      for (const [id, timer] of timers) {
        if (timer.when <= now) { timers.delete(id); timer.fn(); }
      }
    },
  };
  return t;
}

async function until(predicate, rounds = 200) {
  for (let i = 0; i < rounds && !predicate(); i++) await Promise.resolve();
  assert.ok(predicate(), 'condition never became true');
}

const REEL = '[)>\x1e06\x1d1PRES-10K\x1dQ20\x1e\x04';
const MOUSER_REEL = '[)>\x1e06\x1dK606-1\x1d14K002\x1d1PRES-10K\x1dQ20\x1d11K0870\x1d4LJP\x1d1VYageo\x1e\x04';

// ── FASTADD burst ───────────────────────────────────────────────────────────

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
  // After the burst a reel scanned in IDLE is an INFO lookup
  await t.scan(REEL);
  assert.match(t.current().detail, /^RES-10K\n/);
  assert.equal(t.current().action, 'INFO');
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

// ── #2 SYS:INFO on a DataMatrix ─────────────────────────────────────────────

test('INFO shows MPN, description, total and default-location stock, label details', async () => {
  const t = terminal({
    lookup: { barcode_data: {
      mpn: 'RES-10K', quantity: 20, distributor: 'mouser', manufacturer: 'Yageo',
      supplier_sku: '603-RC0603FR-0710KL', batch: '2241',
    } },
  });
  await t.scan('SYS:INFO');
  await t.scan(REEL);
  assert.equal(t.current().detail, [
    'RES-10K',
    'RES 10K OHM 1% 1/10W 0603',
    'Total stock: 420 (180 in default location: [STOCK_1A2B, STOCK_3C4D, STOCK_5E6F, ...])',
    '',
    'Label: Mouser · Mfr: Yageo',
    'SKU: 603-RC0603FR-0710KL · Qty: 20 · Batch: 2241',
  ].join('\n'));
  const card = t.current().stockCard;
  assert.equal(card.mode, 'info');
  assert.equal(card.localStock, '180');
  assert.equal(card.labels.map(l => l.label).join(), 'STOCK_1A2B,STOCK_3C4D,STOCK_5E6F');
  assert.equal(card.moreLabels, true);
  const [request] = t.called('/stock-summary/');
  assert.equal(request.target.searchParams.get('part_id'), '4');
  assert.equal(request.target.searchParams.get('location_id'), '9');
});

test('INFO label list: no "..." up to three labels, none when stock is unlabelled', async () => {
  for (const [options, expected] of [
    [{ more: false }, 'Total stock: 420 (180 in default location: [STOCK_1A2B, STOCK_3C4D, STOCK_5E6F])'],
    [{ labels: LABELS.slice(0, 1), more: false }, 'Total stock: 420 (180 in default location: [STOCK_1A2B])'],
    [{ labels: [] }, 'Total stock: 420 (180 in default location)'],
  ]) {
    const t = terminal(options);
    await t.scan('SYS:INFO');
    await t.scan(REEL);
    assert.equal(t.current().detail.split('\n')[2], expected);
  }
});

test('INFO omits the parenthesis without default location or local stock', async () => {
  for (const options of [{ last: null }, { locationStock: '0' }]) {
    const t = terminal(options);
    await t.scan('SYS:INFO');
    await t.scan(REEL);
    assert.equal(t.current().detail,
      'RES-10K\nRES 10K OHM 1% 1/10W 0603\nTotal stock: 420\n\nQty: 20');
    if ('last' in options) {
      assert.equal(t.called('/stock-summary/')[0].target.searchParams.get('location_id'), null);
    }
  }
});

test('INFO on an unknown part shows the label and never creates it', async () => {
  const t = terminal({ found: false });
  await t.scan('SYS:INFO');
  await t.scan(REEL);
  assert.match(t.current().detail, /Part in DB: NO/);
  assert.equal(t.called('/createpart/').length, 0);
  assert.equal(t.called('/stock-summary/').length, 0);
});

test('INFO reports a failed stock lookup as an error, not as zero', async () => {
  const t = terminal({ summaryStatus: 500 });
  await t.scan('SYS:INFO');
  await t.scan(REEL);
  assert.equal(t.current().state, 'ERROR');
  assert.match(t.current().detail, /Stock lookup failed: Boom/);
});

test('late INFO response cannot replace a new command', async () => {
  let finish;
  const t = terminal({ deferred: new Promise(resolve => { finish = resolve; }) });
  await t.scan('SYS:INFO');
  const info = t.scan(REEL);
  await until(() => t.called('/stock-summary/').length > 0);
  await t.scan('SYS:FASTADD');
  finish();
  await info;
  assert.equal(t.current().state, 'AWAITING_ITEM');
  assert.equal(t.current().action, 'FASTADD');
});

// ── #4 FASTADD / CREATEPART: stock already in the default location ─────────

test('FASTADD on an existing part shows the stock already in the default location', async () => {
  const t = terminal();
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  assert.equal(t.current().state, 'AWAITING_LOCATION');
  assert.equal(t.current().detail, [
    'Create stock for "RES-10K" (reel qty 20)',
    '📦 Already in default location: 180 [STOCK_1A2B, STOCK_3C4D, STOCK_5E6F, ...]',
    '→ Scan one of those labels to MERGE, or a FREE STOCK_ label for a new instance',
    '📍 Location: Bin (SYS:LAST) — scan a Location label to override',
  ].join('\n'));
  const card = t.current().stockCard;
  assert.equal(card.mode, 'receive');
  assert.equal(card.title, 'Receive a reel of 20');
  assert.match(card.hint, /merge the reel/);
  // Merging into one of the listed labels still works
  await t.scan('STOCK_USED');
  assert.match(t.current().detail, /Merged into existing instance/);
});

test('FASTADD without stock in the default location asks for a free label', async () => {
  const t = terminal({ locationStock: '0' });
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  assert.equal(t.current().detail, [
    'Create stock for "RES-10K" (reel qty 20)',
    '→ Scan a FREE STOCK_ label to receive here',
    '📍 Location: Bin (SYS:LAST) — scan a Location label to override',
  ].join('\n'));
  await t.scan('STOCK_9Z9Z');
  assert.equal(t.called('/createstock/')[0].body.location_id, 9);
});

test('FASTADD without SYS:LAST keeps the location prompt and skips the stock lookup', async () => {
  const t = terminal({ last: null });
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  assert.match(t.current().detail, /No default location — scan a Location label/);
  assert.equal(t.called('/stock-summary/').length, 0);
});

test('FASTADD of a newly created part makes no stock lookup', async () => {
  const t = terminal({
    found: false,
    createpart: { success: true, part_id: 7, part_name: 'RES-10K', category_path: 'Resistors' },
  });
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  assert.equal(t.called('/stock-summary/').length, 0);
  assert.equal(t.current().state, 'AWAITING_LOCATION');
  assert.match(t.current().detail, /RES-10K" — Resistors \(reel qty 20\)/);
  assert.match(t.current().detail, /Scan a FREE STOCK_ label to receive here/);
});

test('FASTADD still prompts when the stock lookup fails', async () => {
  const t = terminal({ summaryStatus: 500 });
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  assert.equal(t.current().state, 'AWAITING_LOCATION');
  assert.doesNotMatch(t.current().detail, /Already in default location/);
});

test('burst scan of the next reel shows its local stock too', async () => {
  const t = terminal();
  await t.scan('SYS:FASTADD');
  await t.scan(REEL);
  await t.scan('STOCK_9Z9Z');
  await t.scan(REEL);
  assert.equal(t.current().state, 'AWAITING_LOCATION');
  assert.match(t.current().detail, /📦 Already in default location: 180/);
  assert.equal(t.called('/stock-summary/').length, 2);
});

test('CREATEPART on an existing part lists its stock in the default location', async () => {
  const t = terminal({
    createpart: { success: false, already_exists: true, part_id: 4, part_name: 'RES-10K', enriched: [] },
  });
  await t.scan('SYS:CREATEPART');
  await t.scan(REEL);
  assert.equal(t.current().detail, [
    '⚠ Part already exists: RES-10K',
    'Nothing to fill',
    '📦 Already in default location: 180 [STOCK_1A2B, STOCK_3C4D, STOCK_5E6F, ...]',
  ].join('\n'));
  assert.equal(t.current().stockCard.mode, 'exists');
});

test('a cancel during the CREATEPART search is not overwritten by its late response', async () => {
  let finish;
  const t = terminal({ createpart: new Promise(resolve => { finish = resolve; }) });
  await t.scan('SYS:CREATEPART');
  const pending = t.scan(REEL);
  await until(() => t.called('/createpart/').length > 0);
  await t.scan('SYS:CANCEL');
  finish({ success: true, part_id: 4, part_name: 'RES-10K' });
  await pending;
  assert.equal(t.current().state, 'IDLE');
});

// ── #3 FASTADD gap-fill of missing DigiKey / Mouser supplier parts ──────────

const MOUSER_GAP = {
  distributor: 'mouser', distributor_supplier_part: false,
  enrich_needed: true, missing_suppliers: ['mouser', 'digikey'],
};

test('FASTADD gap-fills the missing supplier parts and shows warnings', async () => {
  const t = terminal({
    lookup: MOUSER_GAP,
    createpart: {
      success: false, already_exists: true, part_id: 4, part_name: 'RES-10K',
      enriched: ['Mouser supplier part 81-RES10K', 'DigiKey supplier part 311-10K-ND'],
      enrich_errors: ['Supplier pricing skipped for Mouser 81-RES10K: Missing rate'],
    },
  });
  await t.scan('SYS:FASTADD');
  await t.scan(MOUSER_REEL);
  const fills = t.called('/createpart/');
  assert.equal(fills.length, 1);
  assert.equal(fills[0].body.enrich_scope, 'label');
  assert.equal(t.current().state, 'AWAITING_LOCATION');
  assert.match(t.current().detail,
    /✓ Filled: Mouser supplier part 81-RES10K, DigiKey supplier part 311-10K-ND/);
  assert.match(t.current().detail, /⚠ Supplier pricing skipped for Mouser 81-RES10K/);
  assert.equal(t.current().stockCard.notes.at(-1),
    '⚠ Supplier pricing skipped for Mouser 81-RES10K: Missing rate');
});

test('FASTADD makes no gap-fill call when nothing is missing', async () => {
  const t = terminal({ lookup: { distributor: 'mouser', distributor_supplier_part: true } });
  await t.scan('SYS:FASTADD');
  await t.scan(MOUSER_REEL);
  assert.equal(t.called('/createpart/').length, 0);
});

test('FASTADD sets a missing default supplier without searching distributors', async () => {
  const t = terminal({
    lookup: { distributor: 'mouser', distributor_supplier_part: true, enrich_needed: true },
    createpart: { success: false, already_exists: true, part_id: 4, enriched: ['Default supplier: Mouser'] },
  });
  await t.scan('SYS:FASTADD');
  await t.scan(MOUSER_REEL);
  assert.equal(t.called('/createpart/').length, 1);
  assert.match(t.current().detail, /✓ Filled: Default supplier: Mouser/);
});

test('a cancel during the FASTADD gap-fill is not overwritten by its late response', async () => {
  let finish;
  const t = terminal({ lookup: MOUSER_GAP, createpart: new Promise(resolve => { finish = resolve; }) });
  await t.scan('SYS:FASTADD');
  const pending = t.scan(REEL);
  await until(() => t.called('/createpart/').length > 0);
  await t.scan('SYS:CANCEL');
  finish({ success: false, already_exists: true, part_id: 4, enriched: ['Mouser supplier part X'] });
  await pending;
  assert.equal(t.current().state, 'IDLE');
});
