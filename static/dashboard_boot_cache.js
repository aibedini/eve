/* Compact snapshot wire format and the per-browser boot cache of the dashboard.
 *
 * The dashboard paints the last snapshot this browser saw before the network answers
 * (stale-while-revalidate), then asks /api/refresh only for what changed since that
 * revision. The cache is scoped to one admin, role and app version, never claims to be
 * live (the freshness rows are not stored), and is wiped on the login page.
 */
(function (root) {
    'use strict';
    const DB_NAME = 'eve-dashboard';
    const STORE = 'snapshots';
    const RECORD_KEY = 'dashboard';
    const SCHEMA = 1;
    const MAX_AGE_MS = 7 * 24 * 3600 * 1000;

    function isRow(value) {
        return value !== null && typeof value === 'object' && !Array.isArray(value);
    }

    // Wire v2 -> v1 rows, in place. Mirrors snapshot_model.expand_wire_inbounds: a
    // shallow copy per membership, stamped with the inbound id when the inbound asks.
    function expand(payload) {
        if (!payload || !Array.isArray(payload.client_table) || !Array.isArray(payload.inbounds)) return payload;
        const table = payload.client_table;
        for (const inbound of payload.inbounds) {
            if (!inbound || !Array.isArray(inbound.client_refs)) continue;
            const stamp = inbound.client_stamp === true;
            const refs = inbound.client_refs;
            const rows = new Array(refs.length);
            for (let i = 0; i < refs.length; i++) {
                const value = table[refs[i]];
                if (!isRow(value)) { rows[i] = value; continue; }
                const row = Object.assign({}, value);
                if (stamp) row.inbound_id = inbound.id;
                rows[i] = row;
            }
            inbound.clients = rows;
            delete inbound.client_refs;
            delete inbound.client_stamp;
        }
        delete payload.client_table;
        delete payload.wire;
        return payload;
    }

    function sameValue(a, b) {
        if (a === b) return true;
        if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') return false;
        return JSON.stringify(a) === JSON.stringify(b);
    }

    function sameRow(a, b, skip) {
        const keys = Object.keys(a);
        let count = 0;
        for (const key of keys) {
            if (key === skip) continue;
            count++;
            if (!Object.prototype.hasOwnProperty.call(b, key) || !sameValue(a[key], b[key])) return false;
        }
        let other = 0;
        for (const key of Object.keys(b)) if (key !== skip) other++;
        return count === other;
    }

    // v1 rows -> wire v2, for storage. Rows are deduplicated by value (the expanded
    // copies no longer share identity), so expand(compact(x)) reproduces x exactly.
    function compact(inbounds) {
        const table = [];
        const byIdentity = new Map();
        const result = [];
        for (const source of inbounds || []) {
            if (!isRow(source)) continue;
            const clients = Array.isArray(source.clients) ? source.clients : [];
            const stamp = clients.length > 0 && clients.every(row => isRow(row) &&
                Object.prototype.hasOwnProperty.call(row, 'inbound_id') && row.inbound_id === source.id);
            const view = {};
            for (const key of Object.keys(source)) if (key !== 'clients') view[key] = source[key];
            const refs = new Array(clients.length);
            for (let i = 0; i < clients.length; i++) {
                const row = clients[i];
                if (!isRow(row)) { refs[i] = table.push(row) - 1; continue; }
                const entity = stamp ? Object.assign({}, row) : row;
                if (stamp) delete entity.inbound_id;
                const identity = String(source.server_id) + '|' + String(entity.email || entity.id || '');
                const candidates = byIdentity.get(identity) || [];
                let slot = candidates.find(index => sameRow(table[index], entity));
                if (slot === undefined) {
                    slot = table.push(entity) - 1;
                    candidates.push(slot);
                    byIdentity.set(identity, candidates);
                }
                refs[i] = slot;
            }
            view.client_refs = refs;
            if (stamp) view.client_stamp = true;
            result.push(view);
        }
        return { inbounds: result, client_table: table };
    }

    function openDb() {
        return new Promise((resolve, reject) => {
            if (!root.indexedDB) { reject(new Error('IndexedDB unavailable')); return; }
            const request = root.indexedDB.open(DB_NAME, 1);
            request.onupgradeneeded = () => {
                if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE);
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
            request.onblocked = () => reject(new Error('IndexedDB blocked'));
        });
    }

    function transact(mode, run) {
        return openDb().then(db => new Promise((resolve, reject) => {
            const tx = db.transaction(STORE, mode);
            const result = run(tx.objectStore(STORE));
            tx.oncomplete = () => { db.close(); resolve(result && 'result' in result ? result.result : undefined); };
            tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); };
        }));
    }

    // Resolves to the stored record for this scope, or null. Never rejects: a missing,
    // foreign, old or unreadable cache is just a cold start.
    function load(scope, { now = Date.now(), timeoutMs = 400 } = {}) {
        if (!scope) return Promise.resolve(null);
        const read = transact('readonly', store => store.get(RECORD_KEY)).then(record => {
            if (!record || record.schema !== SCHEMA || record.scope !== scope) return null;
            if (!Number.isFinite(record.revision) || !(now - record.saved_at < MAX_AGE_MS)) return null;
            return record;
        }).catch(() => null);
        const timeout = new Promise(resolve => setTimeout(() => resolve(null), timeoutMs));
        return Promise.race([read, timeout]);
    }

    function save(scope, snapshot, { now = Date.now() } = {}) {
        if (!scope) return Promise.resolve(false);
        const packed = compact(snapshot.inbounds);
        const record = {
            schema: SCHEMA, scope, saved_at: now, revision: snapshot.revision,
            inbounds: packed.inbounds, client_table: packed.client_table,
            servers: snapshot.servers || [], server_statuses: snapshot.serverStatuses || [],
            stats: snapshot.stats || {},
        };
        return transact('readwrite', store => store.put(record, RECORD_KEY)).then(() => true, () => false);
    }

    function clear() {
        return new Promise(resolve => {
            try {
                const request = root.indexedDB && root.indexedDB.deleteDatabase(DB_NAME);
                if (!request) { resolve(false); return; }
                request.onsuccess = () => resolve(true);
                request.onerror = request.onblocked = () => resolve(false);
            } catch (error) { resolve(false); }
        });
    }

    // Debounced, idle-time persistence of the newest applied revision.
    function createSaver(scope, read, { delayMs = 1500 } = {}) {
        let timer = null;
        let savedRevision = null;
        const idle = callback => (root.requestIdleCallback
            ? root.requestIdleCallback(callback, { timeout: 5000 }) : setTimeout(callback, 0));
        return {
            markSaved(revision) { savedRevision = revision; },
            schedule() {
                if (!scope) return;
                clearTimeout(timer);
                timer = setTimeout(() => idle(() => {
                    const snapshot = read();
                    if (!snapshot || !Number.isFinite(snapshot.revision) || snapshot.revision === savedRevision) return;
                    savedRevision = snapshot.revision;
                    save(scope, snapshot).then(ok => { if (!ok) savedRevision = null; });
                }), delayMs);
            },
        };
    }

    const api = { expand, compact, load, save, clear, createSaver, DB_NAME };
    root.EveDashboardCache = api;
    if (typeof module !== 'undefined') module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
