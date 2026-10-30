/*
 * Sub Manager -> Page Template: pick the subscription page template and drag its
 * sections into order. Pointer events drive the drag so mouse, pen and touch behave
 * the same; the handle also accepts ArrowUp/ArrowDown for keyboard users.
 */
(function () {
    'use strict';

    var dataEl = document.getElementById('sub-layout-data');
    if (!dataEl) return;
    var data = JSON.parse(dataEl.textContent);
    var _t = window._t || function (text) { return text; };

    var BLOCKS = {
        status: { name: _t('Status & notices'), desc: _t('Account name, service state and announcements') },
        renew: { name: _t('Renewal packages'), desc: _t('Packages the user can request a renewal with') },
        backup: { name: _t('Backup configs'), desc: _t('Shown only when a backup config exists') },
        usage: { name: _t('Data usage'), desc: _t('Used and remaining data, expiry and support links') },
        link: { name: _t('Link & app guide'), desc: _t('Subscription link, QR and per-device app tutorials') },
        history: { name: _t('Usage history'), desc: _t('Daily and monthly usage table') },
        faq: { name: _t('FAQ'), desc: _t('Frequently asked questions per platform') },
        config: { name: _t('Manual configuration'), desc: _t('Copyable configs for manual import') }
    };
    var DEFAULT_ORDER = Object.keys(BLOCKS);
    var REQUIRED = {};
    data.required.forEach(function (id) { REQUIRED[id] = true; });
    var DEVICES = {
        mobile: { width: 390, height: 780 },
        tablet: { width: 820, height: 1040 },
        desktop: { width: 1366, height: 860 }
    };

    var state = {
        template: data.layout.template,
        layouts: JSON.parse(JSON.stringify(data.layout.layouts)),
        device: 'mobile',
        lang: data.lang === 'en' ? 'en' : 'fa'
    };
    var savedSnapshot = snapshot();
    var shown = false;
    var previewTimer = null;

    var list = document.getElementById('sub-block-list');
    var live = document.getElementById('sub-layout-live');
    var saveBtn = document.getElementById('sub-layout-save');
    var resetBtn = document.getElementById('sub-layout-reset');
    var stage = document.getElementById('sub-preview-stage');
    var frame = document.getElementById('sub-preview-frame');
    var iframe = document.getElementById('sub-preview-iframe');
    var cards = Array.prototype.slice.call(document.querySelectorAll('.sub-tpl-card'));

    function snapshot() {
        return JSON.stringify({ template: state.template, layouts: state.layouts });
    }

    function blocks() {
        return state.layouts[state.template];
    }

    function isDirty() {
        return snapshot() !== savedSnapshot;
    }

    function changed() {
        saveBtn.disabled = !isDirty();
        schedulePreview();
    }

    function announce(text) {
        live.textContent = '';
        window.setTimeout(function () { live.textContent = text; }, 30);
    }

    /* ---------- templates ---------- */

    function renderTemplates() {
        cards.forEach(function (card) {
            var on = card.getAttribute('data-template') === state.template;
            card.classList.toggle('selected', on);
            card.setAttribute('aria-checked', on ? 'true' : 'false');
            card.tabIndex = on ? 0 : -1;
        });
    }

    function selectTemplate(name, focus) {
        if (!state.layouts[name] || name === state.template) return;
        state.template = name;
        renderTemplates();
        renderBlocks();
        if (focus) cards.filter(function (c) { return c.getAttribute('data-template') === name; })[0].focus();
        changed();
    }

    cards.forEach(function (card, index) {
        card.addEventListener('click', function () { selectTemplate(card.getAttribute('data-template')); });
        card.addEventListener('keydown', function (event) {
            var step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
            if (!step) return;
            event.preventDefault();
            if (document.dir === 'rtl' && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) step = -step;
            var next = cards[(index + step + cards.length) % cards.length];
            selectTemplate(next.getAttribute('data-template'), true);
        });
    });

    /* ---------- section list ---------- */

    function gripIcon() {
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', '18');
        svg.setAttribute('height', '18');
        svg.setAttribute('aria-hidden', 'true');
        [[9, 6], [15, 6], [9, 12], [15, 12], [9, 18], [15, 18]].forEach(function (p) {
            var c = document.createElementNS(ns, 'circle');
            c.setAttribute('cx', p[0]);
            c.setAttribute('cy', p[1]);
            c.setAttribute('r', '1.6');
            svg.appendChild(c);
        });
        return svg;
    }

    function itemFor(block, index, total) {
        var meta = BLOCKS[block.id];
        var li = document.createElement('li');
        li.className = 'sub-block-item' + (block.visible ? '' : ' is-hidden');
        li.setAttribute('data-block', block.id);

        var handle = document.createElement('button');
        handle.type = 'button';
        handle.className = 'sub-block-handle';
        handle.setAttribute('aria-label', _t('Move {name}, position {pos} of {total}', {
            name: meta.name, pos: index + 1, total: total
        }));
        handle.appendChild(gripIcon());

        var pos = document.createElement('span');
        pos.className = 'sub-block-pos';
        pos.textContent = String(index + 1);

        var text = document.createElement('span');
        text.className = 'sub-block-text';
        var name = document.createElement('span');
        name.className = 'sub-block-name';
        name.textContent = meta.name;
        var desc = document.createElement('span');
        desc.className = 'sub-block-desc';
        desc.textContent = meta.desc;
        text.appendChild(name);
        text.appendChild(desc);

        li.appendChild(handle);
        li.appendChild(pos);
        li.appendChild(text);

        if (REQUIRED[block.id]) {
            var badge = document.createElement('span');
            badge.className = 'badge sub-block-lock';
            badge.textContent = _t('Always shown');
            li.appendChild(badge);
        } else {
            var label = document.createElement('label');
            label.className = 'sub-block-toggle';
            label.title = _t('Show on the page');
            var sw = document.createElement('div');
            sw.className = 'toggle-switch';
            var input = document.createElement('input');
            input.type = 'checkbox';
            input.checked = block.visible;
            input.setAttribute('aria-label', _t('Show {name}', { name: meta.name }));
            input.addEventListener('change', function () {
                block.visible = input.checked;
                li.classList.toggle('is-hidden', !input.checked);
                announce(input.checked ? _t('{name} is shown', { name: meta.name }) : _t('{name} is hidden', { name: meta.name }));
                changed();
            });
            var slider = document.createElement('span');
            slider.className = 'slider';
            sw.appendChild(input);
            sw.appendChild(slider);
            label.appendChild(sw);
            li.appendChild(label);
        }
        return li;
    }

    function renderBlocks(focusId) {
        var items = blocks();
        list.textContent = '';
        items.forEach(function (block, index) { list.appendChild(itemFor(block, index, items.length)); });
        if (focusId) {
            var handle = list.querySelector('[data-block="' + focusId + '"] .sub-block-handle');
            if (handle) handle.focus();
        }
    }

    function orderFromDom() {
        var byId = {};
        blocks().forEach(function (block) { byId[block.id] = block; });
        state.layouts[state.template] = Array.prototype.map.call(
            list.querySelectorAll('.sub-block-item'),
            function (li) { return byId[li.getAttribute('data-block')]; });
    }

    function announcePosition(id) {
        var items = blocks();
        var index = items.findIndex(function (b) { return b.id === id; });
        announce(_t('{name} moved to position {pos} of {total}', {
            name: BLOCKS[id].name, pos: index + 1, total: items.length
        }));
    }

    function moveBy(id, step) {
        var items = blocks();
        var from = items.findIndex(function (b) { return b.id === id; });
        var to = from + step;
        if (from < 0 || to < 0 || to >= items.length) return;
        items.splice(to, 0, items.splice(from, 1)[0]);
        renderBlocks(id);
        announcePosition(id);
        changed();
    }

    list.addEventListener('keydown', function (event) {
        var handle = event.target.closest('.sub-block-handle');
        if (!handle) return;
        var id = handle.parentNode.getAttribute('data-block');
        var items = blocks();
        if (event.key === 'ArrowUp') moveBy(id, -1);
        else if (event.key === 'ArrowDown') moveBy(id, 1);
        else if (event.key === 'Home') moveBy(id, -items.findIndex(function (b) { return b.id === id; }));
        else if (event.key === 'End') moveBy(id, items.length - 1 - items.findIndex(function (b) { return b.id === id; }));
        else return;
        event.preventDefault();
    });

    /* ---------- pointer drag ---------- */

    var drag = null;

    function itemTops() {
        var tops = new Map();
        Array.prototype.forEach.call(list.children, function (li) {
            tops.set(li, li.getBoundingClientRect().top);
        });
        return tops;
    }

    function animateSiblings(before) {
        Array.prototype.forEach.call(list.children, function (li) {
            if (drag && li === drag.item) return;
            var delta = before.get(li) - li.getBoundingClientRect().top;
            if (!delta) return;
            li.style.transition = 'none';
            li.style.transform = 'translateY(' + delta + 'px)';
            requestAnimationFrame(function () {
                li.style.transition = '';
                li.style.transform = '';
            });
        });
    }

    function followPointer(clientY) {
        var item = drag.item;
        item.style.transform = '';
        var restTop = item.getBoundingClientRect().top;
        item.style.transform = 'translateY(' + (clientY - drag.grabOffset - restTop) + 'px)';
    }

    function reorderAt(clientY) {
        var others = Array.prototype.filter.call(list.children, function (li) { return li !== drag.item; });
        var target = null;
        for (var i = 0; i < others.length; i += 1) {
            var rect = others[i].getBoundingClientRect();
            if (clientY < rect.top + rect.height / 2) { target = others[i]; break; }
        }
        if (target === drag.item.nextSibling || (!target && drag.item === list.lastElementChild)) return;
        var before = itemTops();
        list.insertBefore(drag.item, target);
        animateSiblings(before);
    }

    function autoScroll(clientY) {
        var edge = 48;
        if (clientY < edge) window.scrollBy(0, -12);
        else if (clientY > window.innerHeight - edge) window.scrollBy(0, 12);
    }

    list.addEventListener('pointerdown', function (event) {
        if (event.button !== 0 || drag) return;
        var item = event.target.closest('.sub-block-item');
        if (!item) return;
        var onHandle = !!event.target.closest('.sub-block-handle');
        // Touch drags start from the handle only, so the list still scrolls with a finger.
        if (!onHandle && (event.pointerType !== 'mouse' || event.target.closest('label, input, button'))) return;
        drag = {
            item: item,
            id: item.getAttribute('data-block'),
            pointerId: event.pointerId,
            startY: event.clientY,
            grabOffset: event.clientY - item.getBoundingClientRect().top,
            startIndex: Array.prototype.indexOf.call(list.children, item),
            active: false
        };
        item.setPointerCapture(event.pointerId);
    });

    list.addEventListener('pointermove', function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        if (!drag.active) {
            if (Math.abs(event.clientY - drag.startY) < 4) return;
            drag.active = true;
            drag.item.classList.add('is-dragging');
            list.classList.add('is-sorting');
        }
        event.preventDefault();
        reorderAt(event.clientY);
        followPointer(event.clientY);
        autoScroll(event.clientY);
    });

    function endDrag(event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var current = drag;
        drag = null;
        current.item.classList.remove('is-dragging');
        current.item.style.transform = '';
        list.classList.remove('is-sorting');
        if (!current.active) return;
        if (Array.prototype.indexOf.call(list.children, current.item) === current.startIndex) return;
        orderFromDom();
        renderBlocks();
        announcePosition(current.id);
        changed();
    }

    list.addEventListener('pointerup', endDrag);
    list.addEventListener('pointercancel', endDrag);

    /* ---------- preview ---------- */

    function previewUrl() {
        var items = blocks();
        var params = new URLSearchParams({
            template: state.template,
            lang: state.lang,
            order: items.map(function (b) { return b.id; }).join(','),
            hidden: items.filter(function (b) { return !b.visible; }).map(function (b) { return b.id; }).join(',')
        });
        return data.preview_url + '?' + params.toString();
    }

    function schedulePreview() {
        if (!shown) return;
        window.clearTimeout(previewTimer);
        previewTimer = window.setTimeout(function () {
            var keep = null;
            try { keep = iframe.contentWindow && iframe.contentWindow.scrollY; } catch (e) { keep = null; }
            iframe.onload = function () {
                if (keep) {
                    try { iframe.contentWindow.scrollTo(0, keep); } catch (e) { /* cross-document */ }
                }
            };
            iframe.src = previewUrl();
        }, 250);
    }

    function fitPreview() {
        var device = DEVICES[state.device];
        var available = stage.clientWidth;
        var scale = Math.min(1, available / device.width);
        frame.style.width = device.width + 'px';
        frame.style.height = device.height + 'px';
        frame.style.transform = 'scale(' + scale + ')';
        frame.style.left = Math.max(0, Math.round((available - device.width * scale) / 2)) + 'px';
        stage.style.height = Math.round(device.height * scale) + 'px';
    }

    function bindSegment(selector, attr, apply) {
        var buttons = Array.prototype.slice.call(document.querySelectorAll(selector));
        buttons.forEach(function (btn) {
            btn.addEventListener('click', function () {
                buttons.forEach(function (b) {
                    var on = b === btn;
                    b.classList.toggle('active', on);
                    b.setAttribute('aria-pressed', on ? 'true' : 'false');
                });
                apply(btn.getAttribute(attr));
            });
        });
    }

    bindSegment('.sub-preview-device', 'data-device', function (device) {
        state.device = device;
        frame.setAttribute('data-device', device);
        fitPreview();
    });
    bindSegment('.sub-preview-lang', 'data-lang', function (lang) {
        state.lang = lang;
        schedulePreview();
    });
    document.querySelectorAll('.sub-preview-lang').forEach(function (btn) {
        var on = btn.getAttribute('data-lang') === state.lang;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });

    if (window.ResizeObserver) new ResizeObserver(fitPreview).observe(stage);
    else window.addEventListener('resize', fitPreview);

    /* ---------- save / reset ---------- */

    resetBtn.addEventListener('click', function () {
        state.layouts[state.template] = DEFAULT_ORDER.map(function (id) { return { id: id, visible: true }; });
        renderBlocks();
        announce(_t('Section order reset'));
        changed();
    });

    saveBtn.addEventListener('click', async function () {
        saveBtn.disabled = true;
        try {
            var response = await fetch('/api/subscription-page-layout', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ template: state.template, layouts: state.layouts })
            });
            var result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || ('HTTP ' + response.status));
            state.template = result.layout.template;
            state.layouts = result.layout.layouts;
            savedSnapshot = snapshot();
            renderTemplates();
            renderBlocks();
            window.showToast && window.showToast(_t('Page template saved'));
        } catch (error) {
            saveBtn.disabled = false;
            window.showToast && window.showToast(error.message || _t('Could not save the page template'), 'error');
        }
    });

    window.addEventListener('beforeunload', function (event) {
        if (!isDirty()) return;
        event.preventDefault();
        event.returnValue = '';
    });

    renderTemplates();
    renderBlocks();

    window.EveSubLayout = {
        show: function () {
            fitPreview();
            if (shown) return;
            shown = true;
            schedulePreview();
        }
    };
}());
