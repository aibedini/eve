(function () {
    'use strict';

    function start() {
        const section = document.getElementById('sub-inbound-status');
        if (!section) return;
        const grid = document.getElementById('sub-inbound-grid');
        const checked = document.getElementById('sub-inbound-checked');
    const counts = document.getElementById('sub-inbound-counts');
    const toggle = document.getElementById('sub-inbound-toggle');
    const body = document.getElementById('sub-inbound-body');
    const arrow = document.getElementById('sub-inbound-arrow');
        const fa = section.dataset.lang === 'fa';
        const labels = fa
      ? { active: 'متصل', disabled: 'عدم اتصال', unknown: 'نامشخص', empty: 'اینباندی برای این اشتراک پیدا نشد.', checked: 'آخرین بررسی: ', activeCount: 'متصل', disabledCount: 'عدم اتصال', ping: 'پینگ' }
      : { active: 'Connected', disabled: 'Disconnected', unknown: 'Unknown', empty: 'No inbounds found for this subscription.', checked: 'Last checked: ', activeCount: 'connected', disabledCount: 'disconnected', ping: 'Ping' };
        let lastItems = [];
        let lastCheckedAt = null;

        function render(items, checkedAt) {
            grid.replaceChildren();
      counts.textContent = `${items.filter((item) => item.state === 'active').length} ${labels.activeCount} · ${items.filter((item) => item.state === 'disabled').length} ${labels.disabledCount}`;
            const date = checkedAt && new Date(checkedAt);
            const dateText = date && !Number.isNaN(date.getTime())
                ? new Intl.DateTimeFormat(fa ? 'fa-IR' : 'en-US', {
                    dateStyle: 'short', timeStyle: 'short'
                }).format(date)
                : '';
            if (!items.length) {
                const empty = document.createElement('p');
                empty.className = 'field-note';
                empty.textContent = labels.empty;
                grid.append(empty);
            }
            for (const item of items) {
                const card = document.createElement('div');
                card.className = 'sub-inbound-card sub-inbound-card--' + item.state;
                const flag = document.createElement('span');
                flag.className = 'sub-inbound-flag';
                const emoji = String(item.name || '').match(/[\u{1F1E6}-\u{1F1FF}]{2}/u);
                const code = emoji && window.EveFlags && window.EveFlags.codeFromEmoji(emoji[0]);
                if (code && window.EveFlags.flagUrl) {
                    const image = document.createElement('img');
                    image.src = window.EveFlags.flagUrl(code);
                    image.alt = code.toUpperCase();
                    image.loading = 'lazy';
                    flag.append(image);
                } else {
                    flag.textContent = '◎';
                    flag.setAttribute('aria-hidden', 'true');
                }
                const name = document.createElement('strong');
                name.className = 'sub-inbound-name';
                name.textContent = String(item.name || 'Inbound').replace(/[\u{1F1E6}-\u{1F1FF}]{2}/gu, '').trim() || 'Inbound';
                const state = document.createElement('span');
                state.className = 'sub-inbound-state';
                state.textContent = labels[item.state] || labels.unknown;
                card.append(flag, name, state);
        const ping = Number(item.ping_ms);
        if (item.ping_ms != null && Number.isFinite(ping) && ping >= 0) {
          const pingLabel = document.createElement('span');
          pingLabel.className = 'sub-inbound-time';
          pingLabel.textContent = `${labels.ping}: ${Math.round(ping)} ms`;
          card.append(pingLabel);
                }
                grid.append(card);
            }
            checked.textContent = dateText ? labels.checked + dateText : '';
      if (body.dataset.open === 'true') body.style.maxHeight = `${body.scrollHeight}px`;
        }

        async function refresh() {
            try {
                const response = await fetch(section.dataset.statusUrl, { cache: 'no-store', credentials: 'same-origin' });
                if (!response.ok) throw new Error('status unavailable');
                const payload = await response.json();
                lastItems = Array.isArray(payload.inbounds) ? payload.inbounds : [];
                lastCheckedAt = payload.checked_at;
                render(lastItems, lastCheckedAt);
            } catch (_) {
                const unknown = lastItems.length
                    ? lastItems.map((item) => ({ ...item, state: 'unknown' }))
                    : [{ name: fa ? 'وضعیت سرورها' : 'Server status', state: 'unknown' }];
                render(unknown, lastCheckedAt);
            }
        }

        try {
            lastItems = JSON.parse(section.dataset.inbounds || '[]');
            lastCheckedAt = section.dataset.checkedAt || null;
            render(lastItems, lastCheckedAt);
        } catch (_) {
            lastItems = [];
        }
        const initialAge = lastCheckedAt ? Date.now() - new Date(lastCheckedAt).getTime() : Infinity;
        if (!Number.isFinite(initialAge) || initialAge >= 300000) refresh();
        window.setInterval(() => {
            if (document.visibilityState === 'visible') refresh();
        }, 300000);
    }

    toggle.addEventListener('click', () => {
      const open = body.dataset.open !== 'true';
      body.dataset.open = String(open);
      toggle.setAttribute('aria-expanded', String(open));
      body.style.maxHeight = open ? `${body.scrollHeight}px` : '0px';
      arrow.classList.toggle('sub-inbound-arrow-open', open);
    });

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
}());
