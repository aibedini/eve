/* One cache request coordinator and bounded SSE lifecycle per dashboard tab. */
(function (root) {
    'use strict';
    class DashboardLiveSync {
        constructor(options) {
            this.options = options;
            this.lastAppliedRevision = null;
            // True while lastAppliedRevision came from the browser's boot cache: it is a
            // starting point for a delta, never proof the server is past it.
            this.provisional = false;
            this.pendingTargetRevision = 0;
            this.lastSseEventAt = null;
            this.lastDashboardRefreshAt = null;
            this.lastDeltaAppliedAt = null;
            this.lastSnapshotPublishedAt = null;
            this.sseStatus = 'disabled';
            this.pollingFallback = true;
            this.reconnectAttempts = 0;
            this.lastFetchError = null;
            this.lastEventServerIds = [];
            this.lastUpdatedInboundCount = 0;
            this.enabled = true;
            this.generation = 0;
            this.source = null;
            this.timer = null;
            this.reconnectTimer = null;
            this.active = null;
            this.queued = false;
        }
        diagnostics() {
            const keys = ['lastAppliedRevision', 'provisional', 'pendingTargetRevision', 'lastSseEventAt',
                'lastDashboardRefreshAt', 'lastDeltaAppliedAt', 'lastSnapshotPublishedAt', 'sseStatus', 'pollingFallback',
                'reconnectAttempts', 'lastFetchError', 'lastEventServerIds', 'lastUpdatedInboundCount'];
            return Object.fromEntries(keys.map(key => [key, this[key]]));
        }
        notify(event = {}) {
            this.lastSseEventAt = Date.now();
            this.pendingTargetRevision = Math.max(this.pendingTargetRevision, Number(event.revision) || 0);
            this.lastEventServerIds = event.server_ids || (event.server_id ? [event.server_id] : []);
            if (this.enabled) this.options.reconcile();
        }
        async request(run, { supersede = false, coalesce = true } = {}) {
            if (this.active && !supersede && !coalesce) {
                try { await this.active.promise; } catch (error) { /* caller owns the error */ }
                return this.request(run, { coalesce: false });
            }
            if (this.active && !supersede) {
                this.queued = true;
                return this.active.promise;
            }
            if (this.active) this.active.controller.abort();
            const request = { generation: ++this.generation, controller: new AbortController() };
            this.active = request;
            request.promise = Promise.resolve().then(() => run(request));
            try {
                return await request.promise;
            } finally {
                // An aborted request cannot clear the controller belonging to its successor.
                if (this.active === request) {
                    this.active = null;
                    if (this.queued) {
                        this.queued = false;
                        if (this.enabled) this.options.reconcile();
                    }
                }
            }
        }
        isCurrent(request, revision) {
            return this.active === request && !request.controller.signal.aborted &&
                (revision == null || this.lastAppliedRevision == null || this.provisional ||
                    revision >= this.lastAppliedRevision);
        }
        applied(sync, count) {
            if (sync && Number.isFinite(sync.revision)) this.lastAppliedRevision = sync.revision;
            this.provisional = false;
            this.lastDashboardRefreshAt = Date.now();
            this.lastFetchError = null;
            this.lastUpdatedInboundCount = count;
            if (sync && sync.published_at != null) this.lastSnapshotPublishedAt = sync.published_at;
            if (sync && sync.mode === 'delta') this.lastDeltaAppliedAt = this.lastDashboardRefreshAt;
            this.options.status();
        }
        schedulePoll() {
            clearTimeout(this.timer);
            if (!this.enabled) return;
            const hidden = this.options.hidden();
            const interval = hidden ? Math.max(45000, this.options.interval()) :
                (this.pollingFallback ? this.options.interval() : Math.max(30000, this.options.interval()));
            this.timer = setTimeout(async () => {
                try { await this.options.reconcile(); }
                finally { this.schedulePoll(); }
            }, interval);
        }
        connect() {
            if (!this.enabled || this.source || !this.options.sseEnabled || !root.EventSource) return;
            clearTimeout(this.reconnectTimer);
            const params = new URLSearchParams();
            if (this.lastAppliedRevision != null) params.set('since', this.lastAppliedRevision);
            const watched = this.options.watched();
            if (watched.length) params.set('servers', watched.join(','));
            let source;
            try { source = new root.EventSource('/api/refresh/stream?' + params); }
            catch (error) { this.disconnectAndRetry(); return; }
            this.source = source;
            this.sseStatus = 'connecting';
            const event = (message) => {
                if (this.source !== source) return;
                let payload;
                try { payload = JSON.parse(message.data || '{}'); }
                catch (error) { payload = {}; }
                this.notify(payload);
            };
            source.addEventListener('hello', message => {
                if (this.source !== source) return;
                this.sseStatus = 'connected';
                this.pollingFallback = false;
                this.reconnectAttempts = 0;
                event(message);
                this.schedulePoll();
                this.options.status();
            });
            // Servers keep the legacy name; newer deployments may use snapshot.changed.
            source.addEventListener('changed', event);
            source.addEventListener('snapshot.changed', event);
            source.addEventListener('client.changed', message => {
                if (this.source !== source) return;
                let payload;
                try { payload = JSON.parse(message.data || '{}'); } catch (error) { payload = {}; }
                // Client patches never acknowledge a whole snapshot, nor race a full response.
                this.notify(payload);
            });
            source.addEventListener('bye', () => {
                if (this.source === source) this.disconnectAndRetry();
            });
            source.onerror = () => {
                if (this.source === source) this.disconnectAndRetry();
            };
            this.options.status();
        }
        disconnectAndRetry() {
            if (this.source) this.source.close();
            this.source = null;
            this.sseStatus = 'disconnected';
            this.pollingFallback = true;
            this.schedulePoll();
            this.options.status();
            if (!this.enabled) return;
            this.options.reconcile();
            clearTimeout(this.reconnectTimer);
            const delay = Math.min(60000, 1000 * 2 ** Math.min(this.reconnectAttempts++, 6));
            this.reconnectTimer = setTimeout(() => this.connect(), delay * (0.75 + Math.random() * 0.5));
        }
        start() {
            this.enabled = true;
            this.connect();
            this.schedulePoll();
            this.options.status();
        }
        pause() {
            this.enabled = false;
            clearTimeout(this.timer);
            clearTimeout(this.reconnectTimer);
            if (this.source) this.source.close();
            this.source = null;
            this.sseStatus = 'paused';
            this.pollingFallback = false;
            this.options.status();
        }
        resumeVisible() {
            if (!this.enabled) return;
            this.options.reconcile();
            this.connect();
            this.schedulePoll();
        }
    }
    root.DashboardLiveSync = DashboardLiveSync;
    function nodeKey(node) {
        if (node.nodeType !== 1) return null;
        if (node.dataset.dashboardKey) return 'card:' + node.dataset.dashboardKey;
        if (node.tagName === 'TR') {
            const checkbox = node.querySelector('.client-bulk-checkbox');
            if (checkbox) return 'client:' + checkbox.dataset.serverId + ':' + checkbox.dataset.email;
        }
        return node.id || null;
    }
    function morphNode(current, next, keepRows = false) {
        if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) {
            current.replaceWith(next);
            return next;
        }
        if (current.nodeType !== 1) {
            if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
            return current;
        }
        if (keepRows && current.tagName === 'TBODY') return current;
        for (const attr of Array.from(current.attributes)) {
            if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
        }
        for (const attr of Array.from(next.attributes)) {
            if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
        }
        morphChildren(current, next, keepRows);
        return current;
    }
    function morphChildren(current, next, keepRows = false) {
        const old = Array.from(current.childNodes);
        const keyed = new Map(old.map(node => [nodeKey(node), node]).filter(([key]) => key));
        const retained = new Set();
        let cursor = current.firstChild;
        for (const node of Array.from(next.childNodes)) {
            const key = nodeKey(node);
            let match = key ? keyed.get(key) : cursor;
            if (match && (retained.has(match) || (!key && nodeKey(match)))) match = null;
            const applied = match ? morphNode(match, node, keepRows) : node;
            if (applied !== cursor && !(match && match === cursor)) current.insertBefore(applied, cursor);
            retained.add(applied);
            cursor = applied.nextSibling;
        }
        for (const node of old) if (!retained.has(node) && node.parentNode === current) node.remove();
    }
    function inventoryMatches(inbounds, inventory) {
        if (!Array.isArray(inventory)) return true;
        if (!Array.isArray(inbounds) || inbounds.length !== inventory.length) return false;
        const actual = new Map(inbounds.map(row => [String(row.server_id) + '|' + String(row.id), (row.clients || []).length]));
        return inventory.every(row => actual.get(String(row[0]) + '|' + String(row[1])) === row[2]);
    }
    root.dashboardInventoryMatches = inventoryMatches;
    root.morphDashboardChildren = morphChildren;
    root.morphDashboardNode = morphNode;
    if (typeof module !== 'undefined') module.exports = { DashboardLiveSync, morphChildren, morphNode, inventoryMatches };
})(typeof window !== 'undefined' ? window : globalThis);
