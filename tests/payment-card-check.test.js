const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node() {
  const classes = new Set(['hidden']);
  return {
    value: '', children: [], textContent: '',
    classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name) },
    addEventListener(name, callback) { this[name] = callback; },
    replaceChildren() { this.children = []; this.textContent = ''; },
    append(...children) { this.children.push(...children); }
  };
}

async function main() {
  const elements = new Map([
    ['renew-sender-card', node()], ['renew-dest-card', node()],
    ['renew-card-check-icon', node()], ['renew-card-check-results', node()]
  ]);
  const document = {
    readyState: 'complete', documentElement: { lang: 'fa' },
    getElementById: (id) => elements.get(id), createElement: node
  };
  let timer;
  let matches = [];
  const fetch = async () => ({ ok: true, json: async () => ({ matches }) });
  vm.runInNewContext(fs.readFileSync('static/payment-card-check.js', 'utf8'), {
    document, window: { EveDate: { timezone: 'Asia/Tehran' } }, fetch,
    setTimeout: (callback) => { timer = callback; }, clearTimeout: () => { timer = null; },
    AbortController, URLSearchParams, Intl, Date, Number, Error
  });
  const sender = elements.get('renew-sender-card');
  const destination = elements.get('renew-dest-card');
  const icon = elements.get('renew-card-check-icon');
  const results = elements.get('renew-card-check-results');
  sender.value = '3796';
  sender.input();
  assert.equal(timer, null, 'destination card is required');
  destination.value = '3';
  destination.change();
  await timer();
  assert.equal(icon.classList.contains('hidden'), false, 'no match shows a green check');

  matches = [{ user: 'client@example.com', created_at: '2026-11-11T12:00:00+00:00', amount: 120000 }];
  sender.input();
  await timer();
  assert.equal(icon.classList.contains('hidden'), true);
  assert.equal(results.classList.contains('hidden'), false);
  assert.equal(results.children[1].children[0].textContent.includes('client@example.com'), true);
  assert.equal(results.children[1].children[0].textContent.includes('تومان'), true);
}

main().catch((error) => { console.error(error.message, error.stack.split('\n')[1]); process.exitCode = 1; });
