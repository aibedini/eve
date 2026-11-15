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
        actionsPending: ['Available after the renewal is verified.', 'پس از تأیید تمدید در دسترس قرار می‌گیرد.'],
        actionsMissing: ['Account information is not ready. Re-check status.', 'اطلاعات حساب هنوز آماده نیست؛ وضعیت را دوباره بررسی کنید.'],
        sendFromDashboard: ['Open the dashboard to send to the client.', 'برای ارسال به مشتری، داشبورد را باز کنید.'],
        backgroundNote: ['You can close this window. Follow progress in recent operations.', 'می‌توانید این پنجره را ببندید؛ ادامهٔ کار در عملیات اخیر قابل پیگیری است.'],
        historyNote: ['History remains available after closing a dialog or refreshing the page.', 'پس از بستن پنجره یا بارگذاری دوبارهٔ صفحه، سابقهٔ عملیات در دسترس است.'],
        historyLimit: ['Latest 50 renewals · All time · Started by you', '۵۰ تمدید اخیر · تمام زمان‌ها · ثبت‌شده توسط شما'],
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
        smsAccount: ['Account', 'تأیید حساب'], smsPrepare: ['Prepare', 'آماده‌سازی'],
        smsGateway: ['Gateway', 'درگاه پیامک'], smsDelivery: ['Delivery', 'تحویل'],
        dispatchPending: ['Not queued yet', 'هنوز در صف ارسال نیست'],
        dispatchPendingNote: ['The account is verified, but no SMS submission is recorded yet. Re-check this operation.', 'حساب تأیید شده، اما هنوز ثبت ارسال پیامک موجود نیست؛ همین عملیات را دوباره بررسی کنید.'],
        dispatchStalled: ['Gateway registration not confirmed', 'ثبت درخواست در درگاه تأیید نشده'],
        dispatchStalledNote: ['EVE scheduled the notification, but no gateway receipt has been recorded yet. Re-check; do not renew again.', 'EVE ارسال را زمان‌بندی کرده، اما رسید ثبت در درگاه هنوز موجود نیست. بررسی مجدد کنید؛ حساب را دوباره تمدید نکنید.'],
        queued: ['Queued at gateway', 'در صف درگاه پیامک'],
        queuedNote: ['The gateway has the request. Sending and delivery are still pending.', 'درگاه درخواست را دریافت کرده است؛ ارسال و تحویل هنوز در انتظارند.'],
        gatewayProcessing: ['Gateway is processing', 'درگاه در حال پردازش است'],
        gatewayProcessingNote: ['The SMS request is registered at the gateway and is being processed.', 'درخواست پیامک در درگاه ثبت شده و در حال پردازش است.'],
        deliveryUnavailable: ['Delivery report unavailable', 'گزارش تحویل در دسترس نیست'],
        deliveryUnavailableNote: ['The gateway accepted the SMS, but no carrier delivery report is available.', 'درگاه پیامک را پذیرفته، اما گزارش تحویل اپراتور در دسترس نیست.'],
        manualReview: ['Manual review required', 'نیاز به بررسی دستی'],
        manualReviewNote: ['The gateway outcome is ambiguous. Review it in SMS center before any resend.', 'نتیجهٔ درگاه مبهم است؛ پیش از ارسال دوباره، آن را در مرکز پیامک بررسی کنید.'],
        smsDisabled: ['SMS automation is disabled.', 'ارسال خودکار پیامک خاموش است.'],
        smsTriggerDisabled: ['Renewal SMS notifications are disabled.', 'پیامک تمدید در تنظیمات غیرفعال است.'],
        smsNotConfigured: ['The SMS gateway connection is not configured.', 'اتصال درگاه پیامک تنظیم نشده است.'],
        smsReseller: ['System SMS is disabled for reseller-owned accounts.', 'پیامک سیستمی برای حساب متعلق به نماینده ارسال نمی‌شود.'],
        smsOptedOut: ['This account has opted out of notifications.', 'اطلاع‌رسانی برای این حساب غیرفعال شده است.'],
        smsNoRecipient: ['No valid recipient phone number was found.', 'شمارهٔ معتبر گیرنده پیدا نشد.'],
        smsNoContent: ['No message text is available for this operation.', 'متن پیام برای این عملیات در دسترس نیست.'],
        smsQuota: ['The SMS sending budget or rate limit was reached.', 'سقف یا محدودیت سرعت ارسال پیامک پر شده است.'],
        smsDuplicate: ['An existing message prevented a duplicate send.', 'به‌دلیل وجود پیام قبلی، از ارسال تکراری جلوگیری شد.'],
        smsVerificationFailed: ['The gateway could not verify the send. Review its record before retrying.', 'درگاه نتوانست ارسال را تأیید کند؛ پیش از تلاش مجدد، سابقهٔ آن را بررسی کنید.'],
        statusUpdated: ['Last recorded update: {time}', 'آخرین وضعیت ثبت‌شده: {time}'],
        historyChecked: ['{count} renewals · Last refreshed {time}', '{count} تمدید · آخرین به‌روزرسانی {time}'],
        checkingBoth: ['Checking account and SMS gateway…', 'در حال بررسی حساب و درگاه پیامک…'],
        checkUpdated: ['Checked {time} · Status updated: {status}', 'بررسی در {time} · وضعیت به‌روز شد: {status}'],
        checkUnchanged: ['Checked {time} · No change: {status}', 'بررسی در {time} · بدون تغییر: {status}'],
        checkPartial: ['Checked {time} · {status} · {error}', 'بررسی در {time} · {status} · {error}'],
        checkFailed: ['Re-check failed at {time}: {error}', 'بررسی مجدد در {time} ناموفق بود: {error}'],
        ratePaused: ['Live updates paused by the request limit. Next retry: {time}.', 'به‌روزرسانی زنده به محدودیت درخواست رسیده است؛ تلاش بعدی: {time}.'],
        historyRatePaused: ['History updates paused by the request limit. Next retry: {time}.', 'به‌روزرسانی سابقه به محدودیت درخواست رسیده است؛ تلاش بعدی: {time}.'],
        verifiedRefreshFailed: ['Renewal is confirmed. The latest notification status could not be refreshed.', 'تمدید تأیید شده است؛ دریافت آخرین وضعیت اطلاع‌رسانی ممکن نشد.'],
        verifiedHistoryRefreshFailed: ['Renewal is confirmed. Activity history could not be refreshed; this does not change the renewal result.', 'تمدید تأیید شده است؛ سابقهٔ فعالیت‌ها به‌روز نشد. این خطا نتیجهٔ تمدید را تغییر نمی‌دهد.'],
        historyServerError: ['EVE could not load history. Your renewals are not shown as failed; try refreshing history.', 'دریافت سابقه از EVE ممکن نشد؛ این خطا به معنی شکست تمدیدها نیست. سابقه را دوباره به‌روز کنید.'],
        gatewayCheckFailed: ['The SMS gateway could not be checked. Renewal status is separate.', 'بررسی درگاه پیامک ممکن نشد؛ وضعیت تمدید مستقل است.'],
        sessionExpired: ['Your session has expired. Sign in again to refresh status.', 'نشست شما تمام شده است؛ برای دریافت وضعیت دوباره وارد شوید.'],
        accessLost: ['You no longer have access to this operation.', 'دیگر به این عملیات دسترسی ندارید.'],
        invalidResponse: ['EVE returned an unreadable response. Re-check this operation.', 'پاسخ EVE قابل خواندن نبود؛ همین عملیات را دوباره بررسی کنید.'],
    };
    const t = (key, values = {}) => (labels[key] || labels.unknown)[fa ? 1 : 0]
        .replace(/\{(\w+)\}/g, (match, name) => values[name] == null ? match : String(values[name]));
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
    const checkFeedback = new Map();
    const historyState = {loaded:false,loading:false,error:null,checkedAt:null,retryAt:0,failures:0};
    let selected = null, filter = 'all', timer = null, polling = false, opener = null;
    let permitted = true;
    const activity = byId('renew-activity-modal');
    const visible = el => !el.classList.contains('hidden');
    const text = (id, value) => { byId(id).textContent = value == null ? '' : String(value); };
    const toggle = (id, show) => byId(id).classList.toggle('hidden', !show);

    function formatCheckTime(value) {
        return new Date(value).toLocaleTimeString(fa ? 'fa-IR' : 'en-GB', {
            timeZone:root.dataset.renewTimezone || 'Asia/Tehran', hour:'2-digit',minute:'2-digit',second:'2-digit'});
    }

    function retryDelay(value, now = Date.now()) {
        const seconds = Number(value);
        if (value && Number.isFinite(seconds)) return Math.max(1000, seconds * 1000);
        const deadline = Date.parse(value || '');
        return Number.isFinite(deadline) ? Math.max(1000, deadline - now) : 30000;
    }

    function notificationView(operation, now = Date.now()) {
        const sms = operation.sms || {};
        let status = sms.status || 'waiting';
        if (status === 'waiting' && operation.confirmed) status = 'dispatch_pending';
        if (status === 'sending' && !sms.request_id && Number.isFinite(Date.parse(sms.scheduled_at))
            && now - Date.parse(sms.scheduled_at) > 30000) status = 'stalled';
        const stages = ['pending', 'pending', 'pending', 'pending'];
        let phase = 0, state = 'waiting';
        let key = status === 'stalled' ? 'dispatchStalled' : status === 'failed' ? 'smsFailed' : status === 'unknown' ? 'smsUnknown'
            : status === 'dispatch_pending' ? 'dispatchPending' : status === 'manual_review' ? 'manualReview' : status;
        if (operation.confirmed) stages[0] = 'complete';
        if (status === 'dispatch_pending') { phase = 1; state = 'waiting'; }
        else if (status === 'stalled') { phase = 2; state = 'warning'; }
        else if (status === 'sending' || status === 'queued') {
            phase = 2; state = 'active';
            if (status === 'sending' && sms.request_id) key = 'gatewayProcessing';
        } else if (status === 'accepted') {
            phase = 3;
            if (sms.delivery_state === 'unavailable') { key = 'deliveryUnavailable'; state = 'warning'; }
        } else if (status === 'delivered') { phase = 4; }
        else if (['failed','skipped','manual_review','unknown'].includes(status)) {
            phase = ['failed','undelivered'].includes(sms.delivery_state) ? 3 : sms.request_id ? 2 : operation.confirmed ? 1 : 0;
            state = status === 'failed' ? 'failed' : 'warning';
        }
        for (let index = 0; index < phase; index++) stages[index] = 'complete';
        if (phase < stages.length) stages[phase] = state;
        const reasonCode = String(sms.reason || '').split(':')[0];
        const reasonKey = {
            feature_disabled:'smsDisabled',trigger_disabled:'smsTriggerDisabled',gateway_not_configured:'smsNotConfigured',
            reseller_owned:'smsReseller',opted_out:'smsOptedOut',opted_out_recheck:'smsOptedOut',
            no_recipient:'smsNoRecipient',empty_message:'smsNoContent',missing_content:'smsNoContent',
            duplicate_suppressed:'smsDuplicate',manual_review_pending:'manualReviewNote',
            unverified_manual_review:'manualReviewNote',gateway_status_expired:'smsVerificationFailed',
            daily_limit:'smsQuota',rate_limited:'smsQuota',daily_segment_limit:'smsQuota',
            recipient_rate_limited:'smsQuota',hourly_limit_reached:'smsQuota',daily_limit_reached:'smsQuota',
        }[reasonCode];
        return {status,key,descriptionKey:key + 'Note',stages,reasonKey};
    }

    function renderStepList(id, titles, states) {
        const list = byId(id);
        list.replaceChildren();
        titles.forEach((title, index) => {
            const state = states[index];
            const item = document.createElement('li');
            item.className = 'renew-progress-step' + (state === 'complete' ? ' is-complete' : '');
            item.dataset.stepState = state;
            if (!['complete','pending'].includes(state)) item.setAttribute('aria-current','step');
            const icon = document.createElement('span'); icon.setAttribute('aria-hidden','true');
            mark(icon, {complete:'success',active:'primary',warning:'warning',failed:'danger',waiting:'muted',pending:'muted'}[state], state === 'active');
            const label = document.createElement('span'); label.textContent = title;
            item.append(icon,label);list.appendChild(item);
        });
    }

    function renderNotification(operation) {
        const sms = operation.sms || {};
        const vm = notificationView(operation);
        const badge = byId('renew-operation-sms');
        if (badge.textContent !== t(vm.key)) badge.textContent = t(vm.key);
        badge.className = 'badge renew-notification-' + vm.status;
        renderStepList('renew-operation-sms-steps', ['smsAccount','smsPrepare','smsGateway','smsDelivery'].map(key => t(key)), vm.stages);
        text('renew-operation-sms-description', t(vm.descriptionKey));
        toggle('renew-operation-sms-reason', Boolean(vm.reasonKey));
        text('renew-operation-sms-reason', vm.reasonKey ? t(vm.reasonKey) : '');
        const updated = sms.updated_at || sms.scheduled_at;
        toggle('renew-operation-sms-time', Boolean(updated));
        text('renew-operation-sms-time', updated ? t('statusUpdated',{time:formatCheckTime(updated)}) : '');
        toggle('renew-operation-retry-sms', Boolean(sms.retryable));
        byId('renew-operation-retry-sms').disabled = busy.has(selected);
    }

    function renderCheckFeedback() {
        const entry = checkFeedback.get(selected);
        toggle('renew-operation-check-result', Boolean(entry));
        if (!entry) return;
        const element = byId('renew-operation-check-result');
        if (element.textContent !== entry.message) element.textContent = entry.message;
        element.dataset.tone = entry.tone;
    }

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
        const stepTitles = [t(vm.phase > 0 ? 'saved' : 'recordRequest'),
            t(vm.phase > 1 ? operation.config_applied || operation.confirmed || operation.final_state === 'CONFIG_APPLIED_ACTIVATION_PENDING' ? 'applied' : 'acceptedUpdate' : 'apply'),
            t(vm.phase === 3 ? 'accountVerified' : 'verify')];
        const stepStates = [0,1,2].map(index => index < vm.phase ? 'complete' : index > vm.phase ? 'pending'
            : vm.tone === 'primary' ? 'active' : vm.tone === 'danger' ? 'failed' : 'warning');
        renderStepList('renew-operation-steps',stepTitles,stepStates);
        renderNotification(operation);
        renderCheckFeedback();
        const canCopy = operation.confirmed && Boolean(String(operation.copy_text || '').trim());
        toggle('renew-operation-copy-details', canCopy);
        const copyButton = byId('renew-operation-copy-button');
        const sendButton = byId('renew-operation-send');
        copyButton.disabled = !canCopy;
        sendButton.disabled = !canCopy || typeof window.sendRenewToUser !== 'function';
        copyButton.title = canCopy ? '' : t(operation.confirmed ? 'actionsMissing' : 'actionsPending');
        sendButton.title = !canCopy ? copyButton.title : sendButton.disabled ? t('sendFromDashboard') : '';
        const complete = operation.confirmed || operation.state === 'failed';
        const actions = byId('renew-operation-actions');
        const phase = complete ? 'complete' : 'pending';
        if (actions.dataset.phase !== phase) {
            const focused = document.activeElement;
            const order = complete ? ['done', 'send', 'copy-button', 'recheck']
                : ['copy-button', 'send', 'recheck', 'done'];
            actions.append(...order.map(key => byId('renew-operation-' + key)), byId('renew-operation-repair'));
            actions.dataset.phase = phase;
            if (focused && actions.contains(focused)) focused.focus({preventScroll:true});
        }
        text('renew-operation-copy', canCopy ? operation.copy_text : '');
        text('renew-operation-done', t(operation.confirmed || operation.state === 'failed' ? 'done' : 'background'));
        text('renew-operation-background-note', t(operation.confirmed || operation.state === 'failed' ? 'historyNote' : 'backgroundNote'));
        byId('renew-operation-recheck').disabled = busy.has(selected);
        toggle('renew-operation-repair', Boolean(operation.repairable));
        byId('renew-operation-repair').disabled = busy.has(selected);
        text('renew-operation-recheck', t(busy.has(selected) ? 'checking' : 'recheck'));
        text('renew-operation-diagnostic', JSON.stringify({ operation_id: operation.operation_id,
            state: operation.state, final_state: operation.final_state, updated_at: operation.updated_at,
            trace_id: operation.trace_id, timing: operation.timing, sms:operation.sms, error: operation.error }, null, 2));
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
                const sms = document.createElement('small'); sms.textContent = t(notificationView(op).key);
                const date = document.createElement('time'); date.dateTime = op.created_at || '';
                date.textContent = op.created_at ? new Date(op.created_at).toLocaleString(fa ? 'fa-IR' : 'en-GB', {timeZone:'Asia/Tehran', dateStyle:'short', timeStyle:'short'}) : '';
                content.append(name, summary, sms, date); button.append(icon, content); list.appendChild(button);
            });
            if (focusedId) [...list.children].find(el => el.dataset.renewOperation === focusedId)?.focus();
        }
        const message = historyState.error ? explainError(historyState.error,null,true)
            : historyState.loading && !historyState.loaded ? t('loading')
                : !historyState.loaded ? t('loading') : !rows.length ? t('empty')
                    : t('historyChecked',{count:rows.length,time:formatCheckTime(historyState.checkedAt)});
        text('renew-activity-status', message);
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
        const next = Object.assign({}, previous, {networkError:null,localFailure:false}, operation);
        // Notification-only updates must not discard verified text for this same renewal.
        if (next.confirmed && previous.confirmed && !next.copy_text && previous.copy_text) {
            next.copy_text = previous.copy_text;
            next.tpl_vars = next.tpl_vars || previous.tpl_vars;
        }
        operations.set(operation.operation_id, next);
    }

    function explainError(error, operation, history) {
        if (error.status === 429) return t(history ? 'historyRatePaused' : 'ratePaused', {
            time:formatCheckTime(error.retryAt || Date.now() + (error.retryAfterMs || 30000))});
        if (error.status === 401) return t('sessionExpired');
        if (error.status === 403) return t('accessLost');
        if (error.status === 404) return t('notFound');
        if (error.code === 'sms_gateway_status_unavailable') return t('gatewayCheckFailed');
        if (history) return t('historyServerError');
        if (operation?.confirmed) return t('verifiedRefreshFailed');
        return t(error.code === 'invalid_response' ? 'invalidResponse' : 'network');
    }

    async function request(url, options, readOutcome = false) {
        const response = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' },
            signal: AbortSignal.timeout(15000), cache: 'no-store' }, options || {}));
        const status = Number(response.headers.get('X-Eve-Status')) || response.status;
        let data;
        try { data = await response.json(); }
        catch (_) {
            const error = new Error(t('invalidResponse'));
            error.status = status === 200 && response.redirected ? 401 : status;
            error.code = 'invalid_response';
            error.retryAfterMs = retryDelay(response.headers.get('Retry-After'));
            throw error;
        }
        if (readOutcome && ![401,403,429].includes(status) && status < 500 && (data.verify || data.final_state)) return data;
        if (!response.ok || status >= 400 || !data.success) {
            const error = new Error(t('network'));
            error.status = status; error.code = data.code;
            error.retryAfterMs = retryDelay(response.headers.get('Retry-After'));
            error.retryAt = Date.now() + error.retryAfterMs;
            throw error;
        }
        return data;
    }
    async function refresh(force = false) {
        if (polling || !permitted) return;
        if (force && historyState.error?.status !== 429) historyState.retryAt = 0;
        if (historyState.retryAt > Date.now()) {
            renderHistory(); schedule(historyState.retryAt - Date.now()); return;
        }
        polling = true;
        historyState.loading = true;
        try {
            const data = await request('/api/client-operations/renewals');
            historyState.loaded = true; historyState.error = null;
            historyState.failures = 0;
            historyState.checkedAt = data.checked_at || new Date().toISOString();
            historyState.retryAt = 0;
            const ids = new Set(data.operations.map(op => op.operation_id));
            operations.forEach((op, id) => { if (!ids.has(id) && !submitting.has(id) && id !== selected) operations.delete(id); });
            data.operations.forEach(merge);
            const current = operations.get(selected);
            if (selected && !current?.localFailure
                && (ids.has(selected) ? current?.detailUpdatedAt !== current?.updated_at : !submitting.has(selected))) {
                const detailId = selected;
                // A direct read still finds older operations outside the latest-50 window.
                try {
                    const detail = (await request('/api/client-operations/renewals/' + encodeURIComponent(detailId))).operation;
                    merge(Object.assign({}, detail, {detailUpdatedAt:detail.updated_at}));
                }
                catch (error) {
                    const op = operations.get(detailId);
                    if (op) {
                        if ([401, 403, 404].includes(error.status)) {
                            op.confirmed = false; op.copy_text = null; op.tpl_vars = null;
                        }
                        op.networkError = explainError(error,op,false);
                    }
                }
            }
            render(); renderHistory();
        } catch (error) {
            if ([401, 403].includes(error.status)) {
                permitted = false; toggle('renew-activity-trigger', false);
            }
            historyState.error = error;
            historyState.failures++;
            historyState.retryAt = error.status === 429 ? error.retryAt || Date.now() + (error.retryAfterMs || 30000)
                : Date.now() + Math.min(30000,1500 * 2 ** Math.min(historyState.failures,5));
            const op = operations.get(selected);
            if (op) {
                op.networkError = op.confirmed && ![401,403,404,429].includes(error.status)
                    ? t('verifiedHistoryRefreshFailed') : explainError(error,op,false);
                render();
            }
            renderHistory();
        } finally {
            polling = false;
            historyState.loading = false;
            const pending = [...operations.values()].some(op => op.pending);
            const cadence = document.hidden ? 30000 : visible(root) || visible(activity) ? 1500 : pending ? 4000 : 30000;
            if (permitted) schedule(Math.max(cadence,historyState.retryAt - Date.now()));
        }
    }
    function schedule(delay) { clearTimeout(timer); timer = setTimeout(refresh, delay); }

    async function recheck(forceEnable) {
        const op = operations.get(selected);
        if (!op || busy.has(op.operation_id)) return;
        const id = op.operation_id;
        const before = JSON.stringify([op.final_state,op.confirmed,op.sms]);
        busy.add(id); render();
        checkFeedback.set(id,{message:t('checkingBoth'),tone:'checking'}); renderCheckFeedback();
        try {
            const results = await Promise.allSettled([
                request(`/api/client/${op.server_id}/${op.inbound_id}/${encodeURIComponent(op.email)}/renew/verify`, {
                    method:'POST',body:JSON.stringify({operation_id:id,awaiting_result:true,force_enable:Boolean(forceEnable)})},true),
                request('/api/client-operations/renewals/' + encodeURIComponent(id) + '/recheck-sms', {method:'POST',body:'{}'})
            ]);
            const errors = [];
            if (results[1].status === 'fulfilled') merge(results[1].value.operation);
            else errors.push(explainError(results[1].reason,operations.get(id),false));
            if (results[0].status === 'fulfilled') {
                const data = results[0].value;
                if (typeof window.applyClientMutation === 'function') window.applyClientMutation(data,{serverId:op.server_id,inboundId:op.inbound_id,email:op.email});
                accept(data,{serverId:op.server_id,inboundId:op.inbound_id,email:op.email,operationId:id});
            } else errors.push(explainError(results[0].reason,operations.get(id),false));
            await refresh();
            const current = operations.get(id) || op;
            const changed = before !== JSON.stringify([current.final_state,current.confirmed,current.sms]);
            const status = view(current,Date.now()).title + ' · ' + t(notificationView(current).key);
            const key = errors.length === 2 ? 'checkFailed' : errors.length ? 'checkPartial' : changed ? 'checkUpdated' : 'checkUnchanged';
            checkFeedback.set(id,{message:t(key,{time:formatCheckTime(new Date()),status,error:errors.join(' ')}),
                tone:errors.length === 2 ? 'error' : errors.length || !current.confirmed
                    || ['failed','skipped','unknown','manual_review','dispatch_pending','stalled'].includes(notificationView(current).status) ? 'warning' : 'success'});
        } catch (error) {
            checkFeedback.set(id,{message:t('checkFailed',{time:formatCheckTime(new Date()),error:explainError(error,operations.get(id),false)}),tone:'error'});
        }
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
        if (previous.confirmed && !confirmed) {
            // The reconciler can confirm before the original response brings its text.
            if (data.business_finalized === true && String(data.copy_text || '').trim()) {
                merge({operation_id:id,copy_text:data.copy_text,tpl_vars:data.tpl_vars || previous.tpl_vars});
                render(); renderHistory();
            }
            schedule(0); return;
        }
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
            sms: confirmed && ['sent','already_sent'].includes(data.customer_sms) && (!previous.sms || ['waiting','dispatch_pending','unknown'].includes(previous.sms.status))
                ? {status:'sending',retryable:false} : previous.sms || {status:confirmed ? 'dispatch_pending' : 'waiting',retryable:false},
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
        case 'refresh': await refresh(true); break;
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
    window.addEventListener('online', () => {
        if (historyState.error?.status !== 429) historyState.retryAt = 0;
        schedule(0);
    });
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
