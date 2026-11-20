// Run with: node --test tests/dashboard-receipt.test.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ui = path.join(__dirname, '../inventree_smart_parts/static/inventree_smart_parts/ui');
const load = (name, context) => vm.runInNewContext(fs.readFileSync(path.join(ui, name), 'utf8'), context);

test('focused barcode field keeps scanner input out of the MPN search path', () => {
  let onKey;
  const input = { value: 'STOCK_AB12', hasAttribute: name => name === 'data-sp-direct-scan' };
  const context = {
    window: { location: { pathname: '/plugin/smartparts/' } },
    document: { activeElement: input, addEventListener(type, listener) { onKey = listener; } },
  };
  load('scanner.js', context);
  context.window.SmartPartsScanner.init(() => assert.fail('A label field must not trigger MPN lookup'));
  for (const key of [...input.value, 'Enter']) onKey({ key });
  assert.equal(input.value, 'STOCK_AB12');
});

test('a late token conflict retries stock against the part saved by the first request', async () => {
  const calls = [];
  const nodes = new Map();
  const editor = { isConnected: true, before(error) { nodes.set(error.id, error); } };
  const saveButton = { innerHTML: 'Create new part' };
  const results = {};
  nodes.set('searchResults', results);
  nodes.set('receiveStockSection', editor);
  nodes.set('enableReceive', { checked: true });
  nodes.set('stockQty', { value: '2.5' });
  nodes.set('stockLocation', { value: '8' });
  nodes.set('stockToken', { value: 'STOCK_AB12' });
  const document = {
    addEventListener() {},
    getElementById: id => nodes.get(id),
    querySelectorAll: selector => selector === '.sp-btn-success' ? [saveButton] : [],
    createElement() { return { style: {}, setAttribute() {}, remove() { nodes.delete(this.id); } }; },
  };
  const context = {
    window: {}, document, getCookie: () => 'csrf', escHtml: value => String(value ?? ''),
    fetch: async (url, options) => {
      calls.push({ url, data: JSON.parse(options.body) });
      const data = url === 'create/'
        ? { success: true, action: 'created', part_id: 24, stock: { success: false, message: 'Already linked' } }
        : { success: true, stock_id: 12, message: 'Linked STOCK_AB13' };
      return { ok: true, json: async () => data };
    },
  };
  load('editor_main.js', context);
  context.saveFromEditor(false, null);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get('spSaveError').textContent, /Part saved.*Already linked/);
  assert.equal(saveButton.innerHTML, 'Retry stock receipt');
  nodes.get('stockToken').value = 'STOCK_AB13';
  context.saveFromEditor(false, null);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls.map(call => [call.url, call.data.stock_token]),
    [['create/', 'STOCK_AB12'], ['api/stock/create/', 'STOCK_AB13']]);
  assert.equal(calls[1].data.part_id, 24);
  assert.equal(calls[1].data.quantity, '2.5');
  assert.match(results.innerHTML, /Linked STOCK_AB13/);
});
