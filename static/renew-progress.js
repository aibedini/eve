/* Renewal progress comes from the durable ledger; polling never repeats a renewal. */
(function () {
    'use strict';
    const root = document.getElementById('renew-operation-modal');
    if (!root) return;
    const fa = root.dataset.renewLang === 'fa';
    const labels = {
        renew: ['Renew client', 'تمدید حساب'], history: ['Recent operations', 'عملیات اخیر'],
        notification: ['Client notification', 'اطلاع‌رسانی به مشتری'], details: ['Technical details', 'جزئیات فنی'],
        accountInfo: ['Account information', 'اطلاعات حساب'], viewHistory: ['View in recent operations', 'پیگیری در عملیات اخیر'],
        recheck: ['Re-check status', 'بررسی مجدد وضعیت'], retrySms: ['Retry SMS', 'تلاش مجدد ارسال پیامک'],
        repair: ['Retry activation', 'تلاش مجدد فعال‌سازی'],
        background: ['Continue in background', 'ادامه در پس‌زمینه'], done: ['Done', 'تمام'],
        copy: ['Copy info', 'کپی اطلاعات'], send: ['Send to client', 'ارسال به مشتری'],
        backgroundNote: ['You can close this window. Follow progress in recent operations.', 'می‌توانید این پنجره را ببندید؛ ادامهٔ کار در عملیات اخیر قابل پیگیری است.'],
        historyNote: ['History remains available after closing a dialog or refreshing the page.', 'پس از بستن پنجره یا بارگذاری دوبارهٔ صفحه، سابقهٔ عملیات در دسترس است.'],
        historyLimit: ['Latest 50 renewals · Your operations', '۵۰ تمدید اخیر · عملیات شما'],
        inProgress: ['In progress', 'در حال انجام'], attention: ['Needs attention', 'نیاز به بررسی'], all: ['All', 'همه'],
        search: ['Search account or operation', 'جست‌وجوی حساب یا عملیات'], refresh: ['Refresh history', 'به‌روزرسانی سابقه'],
        saved: ['Request saved', 'درخواست ثبت شد'], updating: ['Applying renewal', 'در حال اعمال تمدید'],
        recordRequest: ['Record request', 'ثبت درخواست'], apply: ['Update server', 'اعمال تغییرات در پنل'],
        acceptedUpdate: ['Server update accepted', 'درخواست تغییرات در پنل پذیرفته شد'],
        accountVerified: ['Account verified', 'حساب تأیید شد'],
        applied: ['Renewal applied', 'تمدید اعمال شد'], verify: ['Verify account', 'تأیید وضعیت حساب'],
        submitting: ['Submitting renewal', 'در حال ثبت درخواست تمدید'],
        submittingBody: ['Waiting for EVE to record the request. Updates appear here automatically.', 'در انتظار ثبت درخواست؛ وضعیت واقعی به‌صورت خودکار اینجا نمایش داده می‌شود.'],
        updatingBody: ['The request is saved. Waiting for the server update.', 'درخواست ثبت شده است؛ منتظر پاسخ پنل برای اعمال تمدید هستیم.'],
        verifying: ['Confirming renewal', 'در حال تأیید تمدید'],
        verifyingBody: ['The panel accepted the update. Confirming expiry, traffic and activation.', 'پنل درخواست تغییرات را پذیرفته است؛ انقضا، حجم و فعال‌بودن حساب بررسی می‌شود.'],
        confirmed: ['Renewal confirmed', 'تمدید تأیید شد'],
        confirmedBody: ['The renewed account is verified and active.', 'تمدید بررسی شده و حساب فعال است.'],
        pending: ['Activation is still synchronizing', 'فعال‌سازی هنوز در حال همگام‌سازی است'],
        pendingBody: ['The renewal is applied. Do not renew again; activation continues in the background.', 'تمدید اعمال شده است. دوباره تمدید نکنید؛ فعال‌سازی در پس‌زمینه ادامه دارد.'],
        unknown: ['Renewal not confirmed yet', 'نتیجهٔ تمدید هنوز مشخص نیست'],
        unknownBody: ['Re-check the existing request before attempting another renewal.', 'پیش از تمدید دوباره، همین درخواست را بررسی مجدد کنید.'],
        partial: ['Renewal needs reconciliation', 'تمدید نیاز به تطبیق دارد'],
        partialBody: ['Only part of the change is confirmed. Do not renew again; check the details.', 'فقط بخشی از تغییرات تأیید شده است. دوباره تمدید نکنید؛ جزئیات را بررسی کنید.'],
        failed: ['Renewal was not applied', 'تمدید اعمال نشد'],
        failedBody: ['Review the error and re-check before submitting a new renewal.', 'خطا را بررسی کنید و پیش از ثبت تمدید جدید، وضعیت را دوباره بخوانید.'],
        auth: ['Panel access needs attention', 'دسترسی به پنل نیاز به بررسی دارد'],
        authBody: ['Check panel credentials, then re-check this operation.', 'اطلاعات دسترسی پنل را بررسی و سپس همین عملیات را دوباره بررسی کنید.'],
        delayed: ['Waiting for server confirmation', 'در انتظار تأیید سرور'],
        delayedBody: ['This is taking longer. The outcome is not confirmed; re-check without renewing again.', 'این مرحله طولانی شده است. نتیجه هنوز تأیید نشده؛ بدون تمدید دوباره، بررسی مجدد کنید.'],
        waiting: ['Waiting for verification', 'در انتظار تأیید حساب'],
        waitingNote: ['SMS waits until activation is confirmed.', 'پیامک پس از تأیید فعال‌سازی ارسال می‌شود.'],
        sending: ['Sending SMS', 'در حال ارسال پیامک'],
        sendingNote: ['The notification runs separately from renewal.', 'اطلاع‌رسانی مستقل از تمدید ادامه دارد.'],
        accepted: ['Gateway accepted', 'پذیرفته‌شده توسط درگاه'],
        acceptedNote: ['Accepted by the SMS gateway; delivery to the phone is not yet confirmed.', 'درگاه پیامک را پذیرفته؛ تحویل به گوشی هنوز تأیید نشده است.'],
        delivered: ['Delivered', 'تحویل داده شد'], deliveredNote: ['Delivery was confirmed by the carrier.', 'تحویل توسط اپراتور تأیید شده است.'],
        smsFailed: ['SMS needs attention', 'پیامک نیاز به بررسی دارد'],
        smsFailedNote: ['The notification failed. The renewal result is unchanged.', 'اطلاع‌رسانی ناموفق بوده است؛ نتیجهٔ تمدید تغییری نکرده است.'],
        skipped: ['SMS not sent', 'پیامک ارسال نشد'],
        skippedNote: ['Sending was skipped by notification settings or recipient checks. See SMS center.', 'ارسال به‌دلیل تنظیمات اطلاع‌رسانی یا بررسی گیرنده انجام نشده است؛ مرکز پیامک را بررسی کنید.'],
        smsUnknown: ['SMS status unavailable', 'وضعیت پیامک در دسترس نیست'],
        smsUnknownNote: ['This older operation has no linked delivery record. Check SMS center.', 'برای این عملیات قدیمی، سابقهٔ ارسال متصل موجود نیست؛ مرکز پیامک را بررسی کنید.'],
        network: ['Connection interrupted. The outcome is unknown; re-check this request.', 'ارتباط قطع شده است؛ نتیجه نامشخص است. همین درخواست را دوباره بررسی کنید.'],
        checking: ['Checking…', 'در حال بررسی…'], loading: ['Loading…', 'در حال دریافت…'],
        empty: ['No matching renewals in the latest 50 operations.', 'در ۵۰ عملیات اخیر، تمدیدی مطابق این فیلتر یافت نشد.'],
        unavailable: ['History could not be refreshed. Try again.', 'سابقه به‌روز نشد؛ دوباره تلاش کنید.'],
        notFound: ['This operation is not available yet or you no longer have access. No new renewal was submitted.', 'این عملیات هنوز در دسترس نیست یا دسترسی شما تغییر کرده است. تمدید جدیدی ثبت نشد.'],
        retryBlocked: ['SMS retry is unavailable. Refresh to check its latest status.', 'تلاش مجدد پیامک ممکن نیست؛ آخرین وضعیت را به‌روز کنید.'],
        copied: ['Account information copied', 'اطلاعات حساب کپی شد'],
    };
    const t = key => (labels[key] || labels.unknown)[fa ? 1 : 0];
    const reasons = {
        GLOBAL_DISABLED:['The global account is disabled.', 'حساب در سطح کلی غیرفعال است.'],
        MEMBERSHIP_DISABLED:['An inbound membership is disabled.', 'حساب در یکی از ورودی‌ها غیرفعال است.'],
        MEMBERSHIP_MISSING:['An inbound membership is missing.', 'عضویت حساب در یکی از ورودی‌ها پیدا نشد.'],
        MEMBERSHIP_UNKNOWN:['Inbound membership could not be verified.', 'عضویت حساب در ورودی‌ها قابل تأیید نبود.'],
        TRAFFIC_DISABLED:['Traffic access is disabled.', 'دسترسی ترافیک غیرفعال است.'],
        EXPIRY_MISMATCH:['The expiry does not match the requested value.', 'انقضا با مقدار درخواستی مطابقت ندارد.'],
        QUOTA_MISMATCH:['The traffic limit does not match the requested value.', 'سقف حجم با مقدار درخواستی مطابقت ندارد.'],
        TRAFFIC_DEPLETED:['The account is still depleted.', 'حجم یا زمان حساب هنوز تمام شده است.'],
        TRAFFIC_RESET_FAILED:['Traffic reset was not confirmed.', 'بازنشانی ترافیک تأیید نشد.'],
        NODE_SYNC_PENDING:['The panel node is still synchronizing.', 'نود پنل هنوز در حال همگام‌سازی است.'],
        PANEL_READ_FAILED:['The panel could not be read.', 'خواندن وضعیت پنل ممکن نشد.'],
        AUTH_FAILED:['The panel rejected API access.', 'پنل دسترسی به API را نپذیرفت.'],
        MUTATION_REJECTED:['The panel rejected the update.', 'پنل تغییرات را نپذیرفت.'],
        MUTATION_OUTCOME_UNKNOWN:['The server update outcome is unknown.', 'نتیجهٔ اعمال تغییرات در سرور مشخص نیست.'],
    };
    document.querySelectorAll('[data-renew-label]').forEach(el => { el.textContent = t(el.dataset.renewLabel); });
    const byId = id => document.getElementById(id);
    const operations = new Map();
    const busy = new Set();
    const submitting = new Set();
    const inerted = new Map();
    let selected = null, filter = 'all', timer = null, polling = false, opener = null;
    let permitted = true;
    const activity = byId('renew-activity-modal');
    const visible = el => !el.classList.contains('hidden');
    const text = (id, value) => { byId(id).textContent = value == null ? '' : String(value); };
    const toggle = (id, show) => byId(id).classList.toggle('hidden', !show);

    function view(operation, now) {
        const state = operation.final_state;
        let key = 'unknown', tone = 'warning', phase = 2;
        if (operation.confirmed) { key = 'confirmed'; tone = 'success'; phase = 3; }
        else if (state === 'CONFIG_APPLIED_ACTIVATION_PENDING') key = 'pending';
        else if (state === 'PARTIALLY_APPLIED') { key = 'partial'; phase = 1; }
        else if (state === 'AUTH_DEGRADED') { key = 'auth'; phase = 1; }
        else if (state === 'NOT_APPLIED' || operation.state === 'failed') { key = 'failed'; tone = 'danger'; phase = 1; }
        else if (state === 'UNKNOWN' || operation.state === 'needs_reconciliation') { key = 'unknown'; phase = 1; }
        else if (operation.state === 'submitting') { key = 'submitting'; tone = 'primary'; phase = 0; }
        else if (['reserved', 'prepared'].includes(operation.state)) { key = 'updating'; tone = 'primary'; phase = 1; }
        else if (operation.state === 'panel_applied') { key = 'verifying'; tone = 'primary'; }
        const updated = Date.parse(operation.updated_at || operation.created_at || '');
        if (tone === 'primary' && Number.isFinite(updated) && now - updated > 15000) {
            key = 'delayed'; tone = 'warning';
        }
        const detail = (operation.reason_codes || []).filter(code => reasons[code]).map(code => reasons[code][fa ? 1 : 0]).join(' ');
        return { key, tone, phase, title: t(key), body: t(key === 'updating' ? 'updatingBody' : key + 'Body') + (detail ? ' ' + detail : '') };
    }

    function mark(el, tone, loading) {
        el.className = 'renew-progress-mark is-' + tone + (loading ? ' is-loading' : '');
        el.replaceChildren();
        if (loading) return;
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2'); svg.setAttribute('aria-hidden', 'true');
        const path = document.createElementNS(svg.namespaceURI, 'path');
        path.setAttribute('d', tone === 'success' ? 'M5 12l4 4L19 6' : tone === 'danger' ? 'M6 6l12 12M6 18L18 6' : 'M12 5v9m0 3v2');
        svg.appendChild(path); el.appendChild(svg);
    }

    function render() {
        const operation = operations.get(selected);
        if (!operation) return;
        const vm = view(operation, Date.now());
        text('renew-operation-account', operation.email);
        const summary = [];
        if (operation.days != null) summary.push(operation.days + (fa ? ' روز' : ' days'));
        if (operation.volume_gb != null) summary.push(operation.volume_gb + ' GB');
        if (operation.amount != null) summary.push(Number(operation.amount).toLocaleString(fa ? 'fa-IR' : 'en-US') + ' T');
        text('renew-operation-summary', summary.join(' · '));
        // Avoid repeatedly announcing the same title on routine ledger polls.
        if (byId('renew-operation-title').textContent !== vm.title) {
            text('renew-operation-title', vm.title);
        }
        if (byId('renew-operation-description').textContent !== vm.body) text('renew-operation-description', vm.body);
        mark(byId('renew-operation-mark'), vm.tone, vm.tone === 'primary');
        const steps = byId('renew-operation-steps');
        steps.replaceChildren();
        ['saved', 'applied', 'verify'].forEach((key, index) => {
            const item = document.createElement('li');
            const complete = index < vm.phase;
            item.className = 'renew-progress-step' + (complete ? ' is-complete' : '');
            if (!complete && index === vm.phase) item.setAttribute('aria-current', 'step');
            const icon = document.createElement('span');
            icon.setAttribute('aria-hidden', 'true');
            mark(icon, complete ? 'success' : index === vm.phase ? vm.tone : 'muted', !complete && index === vm.phase && vm.tone === 'primary');
            const label = document.createElement('span');
            const stepKey = index === 0 ? (complete ? 'saved' : 'recordRequest')
                : index === 1 ? (complete ? (operation.config_applied || operation.confirmed
                    || operation.final_state === 'CONFIG_APPLIED_ACTIVATION_PENDING' ? 'applied' : 'acceptedUpdate') : 'apply')
                    : complete ? 'accountVerified' : 'verify';
            label.textContent = t(stepKey);
            item.append(icon, label); steps.appendChild(item);
        });
        const sms = operation.sms || { status: 'waiting' };
        const smsKey = sms.status === 'failed' ? 'smsFailed' : sms.status === 'unknown' ? 'smsUnknown' : sms.status;
        const status = byId('renew-operation-sms');
        if (status.textContent !== t(smsKey)) status.textContent = t(smsKey);
        status.className = 'badge renew-notification-' + sms.status;
        text('renew-operation-sms-description', t(smsKey + 'Note'));
        toggle('renew-operation-retry-sms', Boolean(sms.retryable));
        byId('renew-operation-retry-sms').disabled = busy.has(selected);
        const canCopy = operation.confirmed && Boolean(operation.copy_text);
        toggle('renew-operation-copy-details', canCopy);
        toggle('renew-operation-copy-button', canCopy);
        toggle('renew-operation-send', canCopy && typeof window.sendRenewToUser === 'function');
        text('renew-operation-copy', canCopy ? operation.copy_text : '');
        text('renew-operation-done', t(operation.confirmed || operation.state === 'failed' ? 'done' : 'background'));
        text('renew-operation-background-note', t(operation.confirmed || operation.state === 'failed' ? 'historyNote' : 'backgroundNote'));
        byId('renew-operation-recheck').disabled = busy.has(selected);
        toggle('renew-operation-repair', Boolean(operation.repairable));
        byId('renew-operation-repair').disabled = busy.has(selected);
        text('renew-operation-recheck', t(busy.has(selected) ? 'checking' : 'recheck'));
        text('renew-operation-diagnostic', JSON.stringify({ operation_id: operation.operation_id,
            state: operation.state, final_state: operation.final_state, updated_at: operation.updated_at,
            trace_id: operation.trace_id, timing: operation.timing, error: operation.error }, null, 2));
        toggle('renew-operation-network', Boolean(operation.networkError));
        text('renew-operation-network', operation.networkError || '');
    }

    function renderHistory() {
        const search = byId('renew-activity-search').value.trim().toLowerCase();
        const list = byId('renew-activity-list');
        const rows = [...operations.values()].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
            .filter(op => (filter === 'all' || (filter === 'attention' ? op.attention : op.pending))
                && (!search || (op.email + ' ' + op.operation_id).toLowerCase().includes(search)));
        // Leave focused rows intact between polls, including keyboard navigation.
        const signature = JSON.stringify(rows.map(op => [op.operation_id, op.state, op.final_state, op.sms, op.updated_at]));
        if (list.dataset.signature !== signature) {
            const focusedId = document.activeElement?.dataset.renewOperation;
            list.dataset.signature = signature; list.replaceChildren();
            rows.forEach(op => {
                const button = document.createElement('button'); button.type = 'button';
                button.className = 'renew-activity-row'; button.dataset.renewOperation = op.operation_id;
                const icon = document.createElement('span'); icon.setAttribute('aria-hidden', 'true');
                const vm = view(op, Date.now()); mark(icon, vm.tone, vm.tone === 'primary');
                const content = document.createElement('span'); content.className = 'renew-activity-row-content';
                const name = document.createElement('strong'); name.dir = 'ltr'; name.textContent = op.email;
                const summary = document.createElement('span'); summary.textContent = vm.title;
                const sms = document.createElement('small'); sms.textContent = t(op.sms?.status === 'failed' ? 'smsFailed' : op.sms?.status === 'unknown' ? 'smsUnknown' : op.sms?.status || 'waiting');
                const date = document.createElement('time'); date.dateTime = op.created_at || '';
                date.textContent = op.created_at ? new Date(op.created_at).toLocaleString(fa ? 'fa-IR' : 'en-GB', {timeZone:'Asia/Tehran', dateStyle:'short', timeStyle:'short'}) : '';
                content.append(name, summary, sms, date); button.append(icon, content); list.appendChild(button);
            });
            if (focusedId) [...list.children].find(el => el.dataset.renewOperation === focusedId)?.focus();
        }
        text('renew-activity-status', rows.length ? '' : t('empty'));
        const count = [...operations.values()].filter(op => op.pending || op.attention).length;
        text('renew-activity-count', count > 50 ? '50+' : count);
        toggle('renew-activity-count', count > 0);
    }

    function show(overlay) {
        if (!visible(root) && !visible(activity)) opener = document.activeElement;
        [...document.body.children].forEach(el => {
            if (el !== root && el !== activity && !['SCRIPT', 'STYLE', 'LINK'].includes(el.tagName)) {
                if (!inerted.has(el)) inerted.set(el, el.inert);
                el.inert = true;
            }
        });
        [root, activity].forEach(el => el.classList.toggle('hidden', el !== overlay));
        overlay.querySelector('[role="dialog"]').focus();
        schedule(0);
    }
    function close() {
        [root, activity].forEach(el => el.classList.add('hidden'));
        inerted.forEach((value, el) => { el.inert = value; }); inerted.clear();
        if (opener && opener.isConnected && opener.getClientRects().length) opener.focus();
        else byId('renew-activity-trigger')?.focus();
        schedule(3000);
    }
    function open(id) {
        if (!operations.has(id)) return;
        selected = id; render(); show(root);
    }
    function merge(operation) {
        if (!operation || !operation.operation_id) return;
        const previous = operations.get(operation.operation_id) || {};
        operations.set(operation.operation_id, Object.assign({}, previous, {networkError:null,localFailure:false}, operation));
    }

    async function request(url, options) {
        const response = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' },
            signal: AbortSignal.timeout(15000), cache: 'no-store' }, options || {}));
        const data = await response.json();
        if (!response.ok || !data.success) {
            const error = new Error(response.status === 404 ? t('notFound') : t('network'));
            error.status = Number(response.headers.get('X-Eve-Status')) || response.status;
            throw error;
        }
        return data;
    }
    async function refresh() {
        if (polling || !permitted) return;
        polling = true;
        try {
            const data = await request('/api/client-operations/renewals');
            const ids = new Set(data.operations.map(op => op.operation_id));
            operations.forEach((op, id) => { if (!ids.has(id) && !submitting.has(id) && id !== selected) operations.delete(id); });
            data.operations.forEach(merge);
            const current = operations.get(selected);
            if (selected && !submitting.has(selected) && !current?.localFailure
                && (!ids.has(selected) || current?.detailUpdatedAt !== current?.updated_at)) {
                const detailId = selected;
                // A direct read still finds older operations outside the latest-50 window.
                try {
                    const detail = (await request('/api/client-operations/renewals/' + encodeURIComponent(detailId))).operation;
                    merge(Object.assign({}, detail, {detailUpdatedAt:detail.updated_at}));
                }
                catch (error) { const op = operations.get(detailId); if (op) { op.copy_text = null; op.networkError = error.message; } }
            }
            render(); renderHistory();
        } catch (error) {
            if ([401, 403].includes(error.status)) {
                permitted = false; toggle('renew-activity-trigger', false);
            }
            text('renew-activity-status', t('unavailable'));
            const op = operations.get(selected);
            if (op) { op.networkError = t('network'); render(); }
        } finally {
            polling = false;
            const pending = [...operations.values()].some(op => op.pending);
            if (permitted) schedule(document.hidden ? 30000 : visible(root) || visible(activity) ? 1500 : pending ? 4000 : 30000);
        }
    }
    function schedule(delay) { clearTimeout(timer); timer = setTimeout(refresh, delay); }

    async function recheck(forceEnable) {
        const op = operations.get(selected);
        if (!op || busy.has(op.operation_id)) return;
        const id = op.operation_id;
        busy.add(id); render();
        try {
            const data = await request(`/api/client/${op.server_id}/${op.inbound_id}/${encodeURIComponent(op.email)}/renew/verify`, {
                method: 'POST', body: JSON.stringify({ operation_id: id, awaiting_result: true, force_enable:Boolean(forceEnable) }) });
            if (typeof window.applyClientMutation === 'function') window.applyClientMutation(data, {serverId:op.server_id, inboundId:op.inbound_id, email:op.email});
            accept(data, {serverId:op.server_id, inboundId:op.inbound_id, email:op.email, operationId:id});
            await refresh();
        } catch (error) { const current = operations.get(id); if (current) current.networkError = error.message; }
        finally { busy.delete(id); render(); }
    }
    async function retrySms() {
        const op = operations.get(selected);
        if (!op?.sms?.retryable || busy.has(selected)) return;
        const id = op.operation_id; busy.add(id); render();
        try {
            const data = await request('/api/client-operations/renewals/' + encodeURIComponent(id) + '/retry-sms', {method:'POST', body:'{}'});
            merge(data.operation); await refresh();
        } catch (_) { const current = operations.get(id); if (current) current.networkError = t('retryBlocked'); }
        finally { busy.delete(id); render(); }
    }
    function accept(data, context) {
        const ctx = context || {};
        const id = data.operation_id || ctx.operationId;
        if (!id) return;
        const previous = operations.get(id) || {};
        // A late original response must not undo newer, confirmed ledger evidence.
        const confirmed = data.final_state === 'APPLIED_ACTIVE' && data.business_finalized === true;
        if (previous.confirmed && !confirmed) { schedule(0); return; }
        merge({ operation_id:id, server_id:ctx.serverId || previous.server_id,
            inbound_id:ctx.inboundId || previous.inbound_id, email:ctx.email || previous.email,
            state:data.localFailure ? 'failed' : confirmed ? 'completed' : data.code === 'renew_in_progress' ? previous.state || 'submitting' : 'panel_applied',
            final_state:data.final_state || data.verify?.final_state, confirmed,
            config_applied:data.config_applied === true || data.verify?.config_applied === true,
            business_finalized:data.business_finalized === true, pending:!confirmed && !data.localFailure,
            localFailure:Boolean(data.localFailure), error:data.localFailure ? data.error : previous.error,
            repairable:data.repairable === true,
            reason_codes:data.verify?.reason_codes || (data.failed_steps || []).map(item => item.code),
            copy_text:confirmed ? data.copy_text || previous.copy_text : null,
            tpl_vars:confirmed ? data.tpl_vars || previous.tpl_vars : null, trace_id:data.trace_id, timing:data.timing,
            networkError:null });
        render(); renderHistory(); schedule(0);
    }

    document.addEventListener('click', async event => {
        const trigger = event.target.closest('#renew-activity-trigger');
        if (trigger) { renderHistory(); show(activity); return; }
        const row = event.target.closest('[data-renew-operation]');
        if (row) { open(row.dataset.renewOperation); return; }
        const filterButton = event.target.closest('[data-renew-filter]');
        if (filterButton) {
            filter = filterButton.dataset.renewFilter;
            document.querySelectorAll('[data-renew-filter]').forEach(el => el.setAttribute('aria-pressed', String(el === filterButton)));
            renderHistory(); return;
        }
        const button = event.target.closest('[data-renew-action]');
        if (!button) return;
        switch (button.dataset.renewAction) {
        case 'close': close(); break;
        case 'history': renderHistory(); show(activity); break;
        case 'refresh': await refresh(); break;
        case 'recheck': await recheck(); break;
        case 'repair': await recheck(true); break;
        case 'retry': await retrySms(); break;
        case 'copy':
            try { await navigator.clipboard.writeText(operations.get(selected)?.copy_text || ''); if (typeof showToast === 'function') showToast(t('copied')); }
            catch (_) { byId('renew-operation-copy-details').open = true; }
            break;
        case 'send': {
            const op = operations.get(selected);
            if (op?.confirmed && typeof window.renderRenewApiStatus === 'function') {
                window.renderRenewApiStatus({operation_id:op.operation_id,final_state:op.final_state,
                    business_finalized:true,sending_allowed:true,copy_text:op.copy_text,tpl_vars:op.tpl_vars},
                {serverId:op.server_id,inboundId:op.inbound_id,email:op.email});
                close(); window.sendRenewToUser();
            }
            break;
        }
        }
    });
    byId('renew-activity-search').addEventListener('input', renderHistory);
    document.addEventListener('keydown', event => {
        const overlay = visible(root) ? root : visible(activity) ? activity : null;
        if (!overlay) return;
        if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(); }
        if (event.key === 'Tab') {
            const items = [...overlay.querySelectorAll('button:not(:disabled), input, summary')].filter(el => el.getClientRects().length);
            const first = items[0], last = items[items.length - 1];
            if (event.shiftKey && (document.activeElement === first || !items.includes(document.activeElement))) { event.preventDefault(); last?.focus(); }
            else if (!event.shiftKey && (document.activeElement === last || !items.includes(document.activeElement))) { event.preventDefault(); first?.focus(); }
        }
    }, true);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(0); });
    window.addEventListener('online', () => schedule(0));
    window.EveRenewals = {
        begin(operation) {
            merge(Object.assign({state:'submitting',pending:true,confirmed:false,sms:{status:'waiting'},created_at:new Date().toISOString()}, operation));
            submitting.add(operation.operation_id); open(operation.operation_id);
        },
        finish(id) { submitting.delete(id); schedule(0); },
        accept, refresh, open, recheck, view,
    };
    schedule(0);
}());
