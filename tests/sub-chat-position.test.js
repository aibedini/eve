const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const properties = {};
const widget = {
  isConnected: true,
  closest() { return null; },
  getBoundingClientRect() { return { left: 20, top: 760, bottom: 816, width: 56, height: 56 }; },
  style: {
    getPropertyValue(name) { return properties[name]?.value || ''; },
    getPropertyPriority(name) { return properties[name]?.priority || ''; },
    setProperty(name, value, priority) { properties[name] = { value, priority }; }
  }
};
const nav = { getBoundingClientRect() { return { top: 745, bottom: 835 }; } };
const document = {
  readyState: 'complete',
  querySelector() { return nav; },
  body: { querySelectorAll() { return [widget]; } }
};
const window = {
  innerHeight: 835,
  matchMedia() { return { matches: true }; },
  getComputedStyle() { return { position: 'fixed' }; },
  addEventListener() {}
};
class MutationObserver { observe() {} }
vm.runInNewContext(fs.readFileSync('static/sub-chat-position.js', 'utf8'),
  { document, window, MutationObserver, requestAnimationFrame: (callback) => callback(), Math, Object });
assert.equal(properties.bottom.value, '102px');
assert.equal(properties.left.value, '12px');
assert.equal(properties.right.value, 'auto');
