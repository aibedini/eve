/* System network rates; one small cache read per visible page every five seconds. */
(function () {
    'use strict';
    const text = key => typeof _t === 'function' ? _t(key) : key;
    const samples = new Map();
    let payload = null;
    let receivedAt = 0;
    let timer;
    let controller;
    let stopped = false;
    let suspended = false;

    function formatRate(bytes) {
        if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '—';
        const mbps = bytes * 8 / 1000000;
        return mbps >= 1000 ? (mbps / 1000).toFixed(2) + ' Gbps' : mbps.toFixed(2) + ' Mbps';
    }

    function createMeter() {
        const box = document.createElement('div');
        box.className = 'server-network';
        box.title = text('System network traffic, including non-VPN traffic');
        for (const [key, label, arrow] of [['upload_bps', 'Upload', '↑'], ['download_bps', 'Download', '↓']]) {
            const metric = document.createElement('div');
            metric.className = 'server-network-metric';
            const name = document.createElement('span');
            name.className = 'server-network-label';
            name.textContent = arrow + ' ' + text(label);
            const value = document.createElement('strong');
            value.dataset.networkRate = key;
            value.dir = 'ltr';
            value.textContent = '—';
            metric.append(name, value);
            box.append(metric);
        }
        const state = document.createElement('span');
        state.className = 'server-network-state';
        state.dataset.networkState = '';
        state.textContent = text('Waiting for network data');
        box.append(state);
        return box;
    }

    function paintMeter(box, row, fresh, state) {
        box.dataset.networkFresh = String(fresh);
        box.querySelectorAll('[data-network-rate]').forEach(node => {
            node.textContent = fresh ? formatRate(row?.[node.dataset.networkRate]) : '—';
        });
        const status = box.querySelector('[data-network-state]');
        status.textContent = state;
        status.title = row?.sampled_at ? new Date(row.sampled_at * 1000).toLocaleString() : '';
    }

    function render() {
        const now = Date.now() / 1000;
        const recent = receivedAt && now - receivedAt <= 15;
        document.querySelectorAll('[data-server-network-id]').forEach(box => {
            if (!box.firstChild) box.append(createMeter());
            const row = samples.get(String(box.dataset.serverNetworkId));
            const age = row ? row.age_seconds + now - receivedAt : null;
            const fresh = Boolean(box.dataset.networkDisabled !== 'true' && recent && row?.fresh && age <= 15);
            let state = text('Waiting for network data');
            if (box.dataset.networkDisabled === 'true') state = text('Network monitoring paused');
            else if (fresh) state = text('Live') + ' · ' + Math.max(0, Math.floor(age)) + 's';
            else if (row?.error === 'unsupported') state = text('Network rate unavailable in panel API');
            else if (row?.sampled_at) state = text('Network data is stale');
            else if (row?.error === 'connection' || !recent && receivedAt) state = text('Network connection unavailable');
            paintMeter(box.firstChild, row, fresh, state);
        });
        document.querySelectorAll('[data-server-network-total]').forEach(box => {
            if (!box.firstChild) box.append(createMeter());
            const valid = (payload?.servers || []).filter(row => recent && row.fresh && row.age_seconds + now - receivedAt <= 15);
            const total = valid.reduce((acc, row) => ({upload_bps: acc.upload_bps + row.upload_bps,
                download_bps: acc.download_bps + row.download_bps}), {upload_bps: 0, download_bps: 0});
            const expected = payload?.totals?.expected ?? 0;
            const state = receivedAt ? `${text('Live servers')}: ${valid.length}/${expected}` : text('Waiting for network data');
            paintMeter(box.firstChild, total, valid.length > 0, state);
        });
    }

    async function poll() {
        if (document.hidden || stopped || suspended || controller) return;
        clearTimeout(timer);
        let delay = 5000;
        controller = new AbortController();
        const timeout = setTimeout(() => controller?.abort(), 10000);
        try {
            const response = await fetch('/api/servers/network', {cache: 'no-store', signal: controller.signal});
            if (response.status === 401 || response.status === 403 || response.redirected) {
                stopped = true;
                throw new Error('session');
            }
            if (response.status === 429) delay = 60000;
            if (!response.ok) throw new Error('network');
            payload = await response.json();
            samples.clear();
            (payload.servers || []).forEach(row => samples.set(String(row.server_id), row));
            receivedAt = Date.now() / 1000;
        } catch (_) {
            delay = Math.max(delay, 10000);
        } finally {
            clearTimeout(timeout);
            controller = null;
            render();
            if (!stopped && !suspended && !document.hidden) timer = setTimeout(poll, delay);
        }
    }

    document.addEventListener('visibilitychange', () => {
        clearTimeout(timer);
        if (document.hidden) controller?.abort();
        else { render(); poll(); }
    });
    window.addEventListener('pagehide', () => { suspended = true; clearTimeout(timer); controller?.abort(); });
    window.addEventListener('pageshow', () => { suspended = false; render(); poll(); });
    window.EveServerNetwork = {render, formatRate};
    document.addEventListener('DOMContentLoaded', () => { render(); poll(); });
    setInterval(() => { if (!document.hidden) render(); }, 1000);
}());
