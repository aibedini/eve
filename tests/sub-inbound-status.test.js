const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
function element(id) {
  const node = {
    id,
    dataset: {},
    style: {},
    children: [],
    attributes: {},
    classList: { toggle() {} },
    append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this[name] = callback; },
    scrollHeight: 200
  };
  elements.set(id, node);
  return node;
}

const section = element('sub-inbound-status');
section.dataset.lang = 'fa';
section.dataset.inbounds = JSON.stringify([
  { name: '🇩🇪 Germany', state: 'active' },
  { name: '🇫🇷 France', state: 'disabled' }
]);
section.dataset.checkedAt = new Date().toISOString();
for (const id of ['sub-inbound-grid', 'sub-inbound-checked', 'sub-inbound-active-count',
  'sub-inbound-disabled-count', 'sub-inbound-toggle', 'sub-inbound-body', 'sub-inbound-arrow']) element(id);
elements.get('sub-inbound-body').dataset.open = 'false';

const document = {
  readyState: 'complete',
  getElementById: (id) => elements.get(id),
  createElement: () => ({ append() {}, setAttribute() {} })
};
const window = { EveFlags: null, setInterval() {} };
vm.runInNewContext(fs.readFileSync('static/sub-inbound-status.js', 'utf8'),
  { document, window, Intl, Date, Number, String, Array, fetch() { throw Error('unexpected fetch'); } });

assert.equal(elements.get('sub-inbound-active-count').textContent, 1);
assert.equal(elements.get('sub-inbound-disabled-count').textContent, 1);
const toggle = elements.get('sub-inbound-toggle');
assert.equal(typeof toggle.click, 'function');
toggle.click();
assert.equal(toggle.attributes['aria-expanded'], 'true');
assert.equal(elements.get('sub-inbound-body').style.maxHeight, '200px');
toggle.click();
assert.equal(toggle.attributes['aria-expanded'], 'false');
assert.equal(elements.get('sub-inbound-body').style.maxHeight, '0px');
