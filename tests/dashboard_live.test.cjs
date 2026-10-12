const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

test('snapshot inventory detects missing servers and truncated client lists', () => {
    const { inventoryMatches } = require('../static/dashboard_live.js');
    const inventory = [[1, 7, 2], [2, 8, 0]];
    const complete = [{server_id:1,id:7,clients:[{},{}]}, {server_id:2,id:8,clients:[]}];
    assert.equal(inventoryMatches(complete, inventory), true);
    assert.equal(inventoryMatches(complete.slice(0,1), inventory), false);
    assert.equal(inventoryMatches([{server_id:1,id:7,clients:[{}]},complete[1]], inventory), false);
    assert.equal(inventoryMatches(complete, undefined), true);
});

function fixture(sseEnabled = true) {
    const timers = new Map();
    const events = {};
    let sequence = 0;
    let reconciles = 0;
    let hidden = false;
    class Source {
        static instances = [];
        constructor(url) { this.url = url; this.listeners = {}; Source.instances.push(this); }
        addEventListener(name, callback) { this.listeners[name] = callback; }
        emit(name, payload = {}) { this.listeners[name]?.({ data: JSON.stringify(payload) }); }
        close() { this.closed = true; }
    }
    const sandbox = {
        console, URL, URLSearchParams, AbortController, EventSource: Source,
        setTimeout: (callback, ms) => { const id = ++sequence; timers.set(id, { callback, ms }); return id; },
        clearTimeout: id => timers.delete(id), setInterval: () => 0,
        document: { hidden: false, documentElement: { lang: 'en' },
            addEventListener: (name, callback) => { events[name] = callback; },
            querySelector: () => null, querySelectorAll: () => [],
            getElementById: id => id === 'refresh-interval' ? { value: '10' } : null },
        addEventListener: (name, callback) => { events[name] = callback; },
        localStorage: { getItem: () => null, setItem: () => {} },
        location: { origin: 'https://eve.test' },
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(root, 'static/dashboard_live.js'), 'utf8'), sandbox);
    const live = new sandbox.DashboardLiveSync({ sseEnabled, hidden: () => hidden,
        interval: () => 10000, watched: () => [4, 5], status: () => {},
        reconcile: () => { reconciles++; } });
    const template = fs.readFileSync(path.join(root, 'templates/dashboard.html'), 'utf8');
    const scripts = [...template.matchAll(/<script nonce="\{\{ csp_nonce \}\}">([\s\S]*?)<\/script>/g)];
    const main = scripts.find(match => match[1].includes('function applySnapshotDelta'))[1];
    vm.runInContext(main.replace(/\{\{[\s\S]*?\}\}/g, 'false').replace(/\{%[\s\S]*?%\}/g, '') + `
        applyFilters = () => { globalThis.renderCount = (globalThis.renderCount || 0) + 1; };
        updateStats = () => {};
        hideLoading = () => {};
        handleRefreshError = () => {};
        globalThis.dashboard = { applySnapshotFull, applySnapshotDelta, applyClientMutation,
            refreshData, _captureServersSync, _serverSyncInfo, live: dashboardLive,
            data: () => allInboundsData, filtered: () => inboundsData,
            selection: bulkSelectedClientKeys, expanded: expandedInboundKeys,
            setSseTime: value => { serversSyncCapturedAt = value; } };
    `, sandbox);
    return { live, sandbox, dashboard: sandbox.dashboard, Source, timers, events, template,
        hidden: value => { hidden = value; }, reconciles: () => reconciles };
}
const inbound = (server_id, id, remark = 'inbound', clients = []) => ({ server_id, id, remark,
    protocol: 'vless', enable: true, clients });
const payload = (revision, inbounds, mode = 'full', removed = []) => ({ success: true,
    sync: { revision, mode, since: revision - 1 }, inbounds, removed, servers: [], stats: {} });
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

test('dashboard defaults to live sync and starts cache-only synchronization', () => {
    const f = fixture();
    assert.match(f.template, /id="auto-refresh-toggle" checked/);
    assert.match(f.template, /refreshData\(true, \{ mode: 'cache', poll: false \}\)\.finally/);
    f.live.start();
    assert.equal(f.Source.instances.length, 1);
    assert.equal(f.timers.get(f.live.timer).ms, 10000);
});
test('SSE hello and snapshot events reconcile without acknowledging their revision', () => {
    const f = fixture(); f.live.lastAppliedRevision = 10; f.live.start();
    const source = f.Source.instances[0];
    source.emit('hello', { revision: 11 });
    source.emit('snapshot.changed', { revision: 12, server_ids: [4] });
    assert.equal(f.live.lastAppliedRevision, 10);
    assert.equal(f.live.pendingTargetRevision, 12);
    assert.equal(f.reconciles(), 2);
    assert.equal(f.live.sseStatus, 'connected');
});
test('disabled SSE and unsupported EventSource use lightweight polling', () => {
    const f = fixture(false); f.live.start();
    assert.equal(f.Source.instances.length, 0);
    assert.equal(f.live.pollingFallback, true);
    const g = fixture(); g.sandbox.EventSource = undefined; g.live.start();
    assert.equal(g.live.pollingFallback, true);
});
test('SSE error/capacity rejection closes source and polls with jittered bounded reconnect', () => {
    const f = fixture(); f.live.start(); f.Source.instances[0].onerror();
    assert.equal(f.Source.instances[0].closed, true);
    assert.equal(f.live.pollingFallback, true);
    assert.equal(f.reconciles(), 1);
    assert.ok(f.timers.get(f.live.reconnectTimer).ms >= 750);
    assert.ok(f.timers.get(f.live.reconnectTimer).ms <= 1250);
});
test('SSE reconnect resumes from applied revision and never duplicates connections', () => {
    const f = fixture(); f.live.lastAppliedRevision = 9; f.live.start(); f.live.connect();
    assert.equal(f.Source.instances.length, 1);
    f.Source.instances[0].onerror(); f.timers.get(f.live.reconnectTimer).callback();
    assert.match(f.Source.instances[1].url, /since=9/);
    f.Source.instances[1].emit('hello', { revision: 11 });
    assert.equal(f.live.pollingFallback, false);
});
test('stream bye falls back immediately and explicit pause stops both paths', () => {
    const f = fixture(); f.live.start(); f.Source.instances[0].emit('bye');
    assert.equal(f.live.pollingFallback, true);
    f.live.pause(); const calls = f.reconciles(); f.live.resumeVisible();
    assert.equal(f.reconciles(), calls);
    assert.equal(f.timers.has(f.live.timer), false);
    assert.equal(f.timers.has(f.live.reconnectTimer), false);
});
test('hidden tabs poll less often and visible/network recovery immediately reconcile', () => {
    const f = fixture(false); f.hidden(true); f.live.start();
    assert.equal(f.timers.get(f.live.timer).ms, 45000);
    f.hidden(false); f.live.resumeVisible();
    assert.equal(f.reconciles(), 1); assert.equal(f.timers.get(f.live.timer).ms, 10000);
    assert.ok(f.events.visibilitychange); assert.ok(f.events.online);
});
test('overlapping SSE/poll signals coalesce into one active request and one trailing read', async () => {
    const f = fixture(); let resolve; let runs = 0;
    const first = f.live.request(() => { runs++; return new Promise(r => { resolve = r; }); });
    await flush(); f.live.request(() => { runs++; }); f.live.request(() => { runs++; });
    assert.equal(runs, 1); resolve(); await first;
    assert.equal(f.reconciles(), 1);
});
test('aborted request cannot advance cursor or clear its successor', async () => {
    const f = fixture(); let finish; let firstRequest; let successor;
    const first = f.live.request(request => { firstRequest = request; return new Promise(r => { finish = r; }); });
    await flush();
    const second = f.live.request(request => { successor = request; return new Promise(() => {}); }, { supersede: true });
    await flush(); finish(); await first;
    assert.equal(f.live.isCurrent(firstRequest, 20), false);
    assert.equal(f.live.active, successor); assert.equal(f.live.lastAppliedRevision, null);
    void second;
});
test('older response cannot overwrite a newer applied snapshot', async () => {
    const f = fixture(); f.dashboard.live.lastAppliedRevision = 12;
    f.sandbox.fetch = async () => ({ json: async () => payload(11, [inbound(4, 1, 'old')]) });
    await f.dashboard.refreshData(true, { mode: 'cache', poll: false });
    assert.equal(f.dashboard.data().length, 0); assert.equal(f.dashboard.live.lastAppliedRevision, 12);
});
test('partial client event and mutation never acknowledge unrelated full revision', () => {
    const f = fixture(); f.live.lastAppliedRevision = 5; f.live.start();
    f.Source.instances[0].emit('client.changed', { server_id: 4, revision: 10, client_state: {} });
    assert.equal(f.live.lastAppliedRevision, 5);
    f.dashboard.live.lastAppliedRevision = 5;
    f.dashboard.applyClientMutation({ mutation: { snapshot_revision: 10 } });
    assert.equal(f.dashboard.live.lastAppliedRevision, 5);
    assert.equal(f.dashboard.live.pendingTargetRevision, 10);
});
test('missing delta base recovers with a full cache response without panel enqueue', async () => {
    const f = fixture(); const urls = []; f.dashboard.live.lastAppliedRevision = 5;
    f.sandbox.fetch = async url => { urls.push(url); return { json: async () => urls.length === 1 ?
        { ...payload(7, [], 'delta'), sync: { revision: 7, mode: 'delta', since: 6 } } : payload(7, [inbound(4, 1)]) }; };
    await f.dashboard.refreshData(true, { mode: 'cache', poll: false });
    assert.equal(urls.length, 2); assert.match(urls[0], /since=5/);
    assert.doesNotMatch(urls[1], /since=|enqueue=|force=/); assert.match(urls[1], /poll=false/);
    assert.equal(f.dashboard.live.lastAppliedRevision, 7);
});
test('unchanged poll updates freshness without rendering cards', async () => {
    const f = fixture(); f.dashboard.live.lastAppliedRevision = 7;
    f.sandbox.fetch = async () => ({ json: async () => payload(7, [], 'unchanged') });
    await f.dashboard.refreshData(true, { mode: 'cache', poll: false });
    assert.equal(f.sandbox.renderCount || 0, 0); assert.ok(f.dashboard.live.lastDashboardRefreshAt);
});
test('failed and aborted HTTP requests retain applied cursor', async () => {
    const f = fixture(); f.dashboard.live.lastAppliedRevision = 7;
    f.sandbox.fetch = async () => { throw Object.assign(new Error(), { name: 'AbortError' }); };
    await f.dashboard.refreshData(true, { mode: 'cache', poll: false });
    assert.equal(f.dashboard.live.lastAppliedRevision, 7);
    f.sandbox.fetch = async () => ({ json: async () => ({ success: false }) });
    await f.dashboard.refreshData(true, { mode: 'cache', poll: false });
    assert.equal(f.dashboard.live.lastAppliedRevision, 7); assert.ok(f.dashboard.live.lastFetchError);
});
test('rename, addition, removal and renewal merge without duplicating inbounds', () => {
    const f = fixture(); const eco5 = inbound(5, 1, 'ECO5');
    f.dashboard.applySnapshotFull(payload(1, [inbound(4, 1, 'old'), eco5]));
    f.dashboard.applySnapshotDelta(payload(2, [inbound(4, 1, 'renamed', [{ email: 'a', expiryTimestamp: 999, totalGB: 2048 }]), inbound(4, 2, 'new')], 'delta'));
    assert.equal(f.dashboard.data()[0].remark, 'renamed'); assert.equal(f.dashboard.data()[1], eco5);
    assert.equal(f.dashboard.data().length, 3);
    assert.equal(f.dashboard.data()[0].clients[0].expiryTimestamp, 999);
    assert.equal(f.dashboard.data()[0].clients[0].totalGB, 2048);
    f.dashboard.applySnapshotDelta(payload(3, [inbound(4, 2, 'new')], 'delta', [[4, 1]]));
    assert.equal(f.dashboard.data().length, 2); assert.equal(f.dashboard.filtered(), f.dashboard.data());
});
test('client moved to another inbound removes its old assignment', () => {
    const f = fixture(); f.dashboard.applySnapshotFull(payload(1, [inbound(4, 1, '', [{ email: 'a' }]), inbound(4, 2)]));
    f.dashboard.applySnapshotDelta(payload(2, [inbound(4, 1), inbound(4, 2, '', [{ email: 'a' }])], 'delta'));
    assert.equal(f.dashboard.data()[0].clients.length, 0); assert.equal(f.dashboard.data()[1].clients.length, 1);
});
test('selection survives when valid, removed clients are pruned, expansion keys survive', () => {
    const f = fixture(); f.dashboard.applySnapshotFull(payload(1, [inbound(4, 1, '', [{ email: 'a' }, { email: 'b' }])]));
    f.dashboard.selection.set('4:1:a', {}); f.dashboard.selection.set('4:1:b', {}); f.dashboard.expanded.add('v3s:4');
    f.dashboard.applySnapshotDelta(payload(2, [inbound(4, 1, '', [{ email: 'a' }])], 'delta'));
    assert.equal(f.dashboard.selection.has('4:1:a'), true); assert.equal(f.dashboard.selection.has('4:1:b'), false);
    assert.equal(f.dashboard.expanded.has('v3s:4'), true);
});
test('freshness classification ages without a new response', () => {
    const f = fixture(); f.dashboard._captureServersSync({ servers_sync: { 4: {
        sync_health: 'live', last_success_age_seconds: 3, live_max_age_seconds: 6, fresh_max_age_seconds: 60 } } });
    f.dashboard.setSseTime(Date.now() - 65000);
    assert.match(f.dashboard._serverSyncInfo(4).cls, /field-note-warn/);
});

// Minimal DOM nodes exercise the production reconciler without browser/network dependencies.
class Element {
    constructor(tag, key = '', text = '') {
        this.nodeType = 1; this.nodeName = this.tagName = tag; this.dataset = { dashboardKey: key };
        this.id = ''; this.childNodes = []; this.attrs = new Map(); this.text = text;
        this.checked = false; this.scrollTop = 0;
    }
    get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
    get firstChild() { return this.childNodes[0] || null; }
    get nextSibling() { if (!this.parentNode) return null; const a = this.parentNode.childNodes; return a[a.indexOf(this) + 1] || null; }
    hasAttribute(name) { return this.attrs.has(name); }
    getAttribute(name) { return this.attrs.get(name) ?? null; }
    setAttribute(name, value) { this.attrs.set(name, value); }
    removeAttribute(name) { this.attrs.delete(name); }
    querySelector() { return null; }
    append(node) { this.insertBefore(node, null); return this; }
    insertBefore(node, before) {
        node.remove(); const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
        assert.ok(index >= 0); this.childNodes.splice(index, 0, node); node.parentNode = this;
    }
    remove() { if (this.parentNode) { const a = this.parentNode.childNodes; a.splice(a.indexOf(this), 1); this.parentNode = null; } }
    replaceWith(node) { const parent = this.parentNode; parent.insertBefore(node, this); this.remove(); }
}
class Text {
    constructor(value) { this.nodeType = 3; this.nodeName = '#text'; this.nodeValue = value; }
    get nextSibling() { return Element.prototype.__lookupGetter__('nextSibling').call(this); }
    remove() { Element.prototype.remove.call(this); }
    replaceWith(node) { Element.prototype.replaceWith.call(this, node); }
}
test('visible chip rename/add/delete preserves other server node, focus and scroll', () => {
    const f = fixture(); const parent = new Element('DIV');
    const eco4 = new Element('DIV', 'v3s:4').append(new Text('old'));
    const eco5 = new Element('DIV', 'v3s:5').append(new Text('ECO5')); eco5.scrollTop = 81;
    parent.append(eco4).append(eco5);
    const next = new Element('DIV').append(new Element('DIV', 'v3s:4').append(new Text('renamed')))
        .append(new Element('DIV', 'v3s:5').append(new Text('ECO5'))).append(new Element('DIV', '4:2'));
    f.sandbox.morphDashboardChildren(parent, next);
    assert.equal(parent.childNodes[0], eco4); assert.equal(eco4.firstChild.nodeValue, 'renamed');
    assert.equal(parent.childNodes[1], eco5); assert.equal(eco5.scrollTop, 81);
    const removed = new Element('DIV').append(new Element('DIV', 'v3s:5').append(new Text('ECO5')));
    f.sandbox.morphDashboardChildren(parent, removed);
    assert.equal(parent.childNodes.length, 1); assert.equal(parent.firstChild, eco5);
});
test('row reconciliation keeps selected control and collapsed/expanded attributes', () => {
    const f = fixture(); const current = new Element('DIV'); current.setAttribute('class', 'clients-list hidden');
    const input = new Element('INPUT'); input.checked = true; current.append(input);
    const next = new Element('DIV'); next.setAttribute('class', 'clients-list hidden'); next.append(new Element('INPUT'));
    f.sandbox.morphDashboardNode(current, next);
    assert.equal(current.firstChild, input); assert.equal(input.checked, true);
    assert.equal(current.getAttribute('class'), 'clients-list hidden');
    next.setAttribute('class', 'clients-list'); f.sandbox.morphDashboardNode(current, next);
    assert.equal(current.getAttribute('class'), 'clients-list');
});
test('conditional element replacement keeps the insertion cursor valid', () => {
    const f = fixture(); const current = new Element('DIV').append(new Element('SPAN'));
    const next = new Element('DIV').append(new Element('BUTTON')).append(new Element('SPAN'));
    f.sandbox.morphDashboardChildren(current, next);
    assert.deepEqual(current.childNodes.map(node => node.tagName), ['BUTTON', 'SPAN']);
});
