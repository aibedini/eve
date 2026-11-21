(() => {
    'use strict';

    const root = document.querySelector('[data-support-root]');
    if (!root) return;
    const fa = root.dataset.fa === '1';
    const t = (en, persian) => fa ? persian : en;
    const status = document.getElementById('support-status');
    const caps = new Set(JSON.parse(document.getElementById('support-capabilities')?.textContent || '[]'));
    const output = document.getElementById('support-action');
    const errors = {
        daily_sms_limit: t('Daily SMS limit reached.', 'سقف پیامک روزانه پر شده است.'),
        daily_free_limit: t('Daily free operation limit reached.', 'سقف عملیات رایگان روزانه پر شده است.'),
        free_volume_limit: t('This package exceeds your free volume limit.', 'این پکیج از سقف حجم رایگان بیشتر است.'),
        pricing_changed: t('Price changed. Select the package again.', 'قیمت تغییر کرد. پکیج را دوباره انتخاب کنید.'),
        no_verified_phone: t('No verified phone number.', 'شمارهٔ تأییدشده‌ای وجود ندارد.'),
        operation_in_progress: t('Operation is in progress. Use the same confirmation to check again.', 'عملیات در حال انجام است. برای بررسی دوباره از همین دکمه استفاده کنید.'),
        renew_in_progress: t('An account renewal is already in progress.', 'تمدید این حساب در حال انجام است.'),
        account_scope_changed: t('The account changed. Search for it again.', 'حساب تغییر کرده است. دوباره جست‌وجو کنید.'),
        username_used: t('This username is already in use.', 'این نام کاربری قبلاً استفاده شده است.'),
        role_name_used: t('This role name is already in use.', 'این نام نقش قبلاً استفاده شده است.'),
    };

    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    async function api(path, body, key) {
        const response = await fetch(path, body === undefined ? {} : {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) },
            body: JSON.stringify(body),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.success === false) {
            throw new Error(errors[result.code] || t('Could not complete the request. Check access and fields.', 'درخواست انجام نشد. دسترسی و فیلدها را بررسی کنید.'));
        }
        return result;
    }

    async function run(form, action) {
        const button = form.querySelector('button[type=submit]');
        if (button?.disabled) return;
        if (button) button.disabled = true;
        status.textContent = t('Working…', 'در حال انجام…');
        try {
            await action();
        } catch (error) {
            status.textContent = error.message;
        } finally {
            if (button) button.disabled = false;
        }
    }

    function actionButton(parent, text, onClick) {
        const button = element('button', text, 'btn btn-secondary');
        button.type = 'button';
        button.addEventListener('click', async () => {
            button.disabled = true;
            try {
                await onClick();
            } catch (error) {
                status.textContent = error.message;
            } finally {
                button.disabled = false;
            }
        });
        parent.append(button);
    }

    function accountPath(account) {
        return `/api/support/account/${account.server_id}/${account.inbound_id}/${encodeURIComponent(account.email)}`;
    }

    async function choosePackage(account, kind, free) {
        output.replaceChildren();
        status.textContent = t('Loading packages…', 'در حال بارگیری پکیج‌ها…');
        const { items } = await api(`/api/support/packages/${account.server_id}/${account.inbound_id}/${kind}?free=${Number(free)}`);
        if (!items.length) {
            status.textContent = t('No packages are available.', 'پکیج قابل استفاده‌ای وجود ندارد.');
            return;
        }
        status.textContent = '';
        const form = element('form', '', 'support-form');
        form.append(element('strong', account.email));
        const label = element('label', t('Package and customer price', 'پکیج و قیمت مشتری'), 'form-group');
        const plans = element('select', '', 'form-select');
        for (const item of items) {
            const gb = item.gb ? `${item.gb} GB` : t('Unlimited GB', 'حجم نامحدود');
            plans.add(new Option(`${item.name} · ${item.days} ${t('days', 'روز')} · ${gb} · ${item.price.toLocaleString()} ${t('T', 'تومان')}`, item.id));
        }
        label.append(plans);
        const button = element('button', t('Confirm operation', 'تأیید عملیات'), 'btn btn-primary');
        button.type = 'submit';
        form.append(label, button);
        let key = crypto.randomUUID();
        plans.addEventListener('change', () => { key = crypto.randomUUID(); });
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            run(form, async () => {
                const plan = items.find(item => item.id === Number(plans.value));
                const path = `/api/support/${account.server_id}/${account.inbound_id}/${kind}/${encodeURIComponent(account.email)}`;
                const result = await api(path, { email: account.email, mode: 'package', package_id: plan.id, free, quote_revision: plan.revision }, key);
                output.replaceChildren();
                const completed = ['completed', 'applied_active'].includes(result.state);
                const done = t('Operation recorded.', 'عملیات ثبت شد.');
                status.textContent = completed ? done : t('Operation submitted for verification.', 'عملیات برای بررسی ثبت شد.');
                if (!completed) actionButton(output, t('Check status', 'بررسی وضعیت'), async () => {
                    const { state } = await api(`/api/support/operation/${encodeURIComponent(result.operation_id)}`);
                    status.textContent = state === 'completed' ? done : state === 'failed'
                        ? t('Operation failed.', 'عملیات ناموفق بود.')
                        : t('Verification is still pending.', 'بررسی هنوز تکمیل نشده است.');
                });
            });
        });
        output.append(form);
        output.focus();
    }

    function messageForm(account, action) {
        output.replaceChildren();
        const isSms = action === 'sms';
        const form = element('form', '', 'support-form');
        const label = element('label', isSms ? t('Message', 'پیام') : t('What needs correction?', 'چه چیزی باید اصلاح شود؟'), 'form-group');
        const text = element('textarea');
        text.required = true;
        text.rows = 3;
        text.maxLength = isSms ? 480 : 1000;
        label.append(text);
        const button = element('button', isSms ? t('Send SMS', 'ارسال پیامک') : t('Submit request', 'ثبت درخواست'), 'btn btn-primary');
        button.type = 'submit';
        form.append(element('strong', account.email), label, button);
        const key = crypto.randomUUID();
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            run(form, async () => {
                const result = await api(`${accountPath(account)}/${action}`, isSms ? { message: text.value, key } : { reason: text.value });
                output.replaceChildren();
                status.textContent = isSms ? t('Accepted by the gateway.', 'درگاه پیامک را پذیرفت.')
                    : result.notified ? t('Request saved and manager notified.', 'درخواست ثبت شد و به مدیر اطلاع داده شد.')
                        : t('Request saved in the team inbox.', 'درخواست در بخش تیم ثبت شد.');
            });
        });
        output.append(form);
        text.focus();
    }

    async function history(account) {
        const { items } = await api(`${accountPath(account)}/history`);
        output.replaceChildren(element('h3', t('My changes on this account', 'تغییرات من روی این حساب')));
        for (const row of items) {
            const item = element('div', `${row.kind} · ${row.state} · ${new Date(row.at + 'Z').toLocaleString()}`, 'support-item');
            if (row.reason) item.append(element('p', row.reason));
            for (const [title, values] of [[t('Before', 'قبل'), row.before], [t('After', 'بعد'), row.after]]) {
                if (values && Object.keys(values).length) item.append(element('small', `${title} · ${JSON.stringify(values)}`));
            }
            output.append(item);
        }
        if (!items.length) output.append(element('p', t('No changes recorded yet.', 'هنوز تغییری ثبت نشده است.'), 'field-note'));
        output.focus();
    }

    const search = document.getElementById('support-search');
    search?.addEventListener('submit', (event) => {
        event.preventDefault();
        run(search, async () => {
            const results = document.getElementById('support-results');
            const query = document.getElementById('support-query').value.trim();
            const { items } = await api(`/api/support/customers?q=${encodeURIComponent(query)}`);
            output.replaceChildren();
            results.replaceChildren();
            for (const account of items) {
                const row = element('div', '', 'support-item');
                const name = element('strong', account.email);
                name.dir = 'auto';
                const state = account.enabled ? t('Enabled', 'فعال') : t('Disabled', 'غیرفعال');
                row.append(name, element('span', `${state} · ${account.gb || t('Unlimited', 'نامحدود')} GB`));
                const actions = element('div', '', 'support-actions');
                if (caps.has('renew')) actionButton(actions, t('Renew', 'تمدید'), () => choosePackage(account, 'renew', false));
                if (caps.has('free_renew')) actionButton(actions, t('Free renewal', 'تمدید رایگان'), () => choosePackage(account, 'renew', true));
                if (caps.has('sms') && account.sms) actionButton(actions, t('SMS', 'پیامک'), () => messageForm(account, 'sms'));
                if (caps.has('correction')) actionButton(actions, t('Request correction', 'درخواست اصلاح'), () => messageForm(account, 'correction'));
                actionButton(actions, t('History', 'تاریخچه'), () => history(account));
                row.append(actions);
                results.append(row);
            }
            status.textContent = items.length ? '' : t('No matching accounts in your scope.', 'حساب مطابقی در محدودهٔ شما وجود ندارد.');
        });
    });

    const create = document.getElementById('support-create');
    create?.addEventListener('submit', (event) => {
        event.preventDefault();
        const [server_id, inbound_id] = document.getElementById('support-location').value.split('/');
        const account = { server_id, inbound_id, email: document.getElementById('support-email').value.trim() };
        output.replaceChildren();
        if (caps.has('create')) actionButton(output, t('Paid account', 'حساب پولی'), () => choosePackage(account, 'create', false));
        if (caps.has('free_create')) actionButton(output, t('Free account', 'حساب رایگان'), () => choosePackage(account, 'create', true));
        output.focus();
    });

    document.querySelectorAll('[data-ticket-action]').forEach(form => {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            run(form, async () => {
                const data = Object.fromEntries(new FormData(form));
                await api(`/api/support/ticket/${form.dataset.ticket}/${form.dataset.ticketAction}`, data);
                location.reload();
            });
        });
    });

    const chatForm = document.querySelector('[data-chat-url]');
    if (chatForm) {
        const select = document.querySelector('[data-chat-select]');
        const log = document.querySelector('[data-chat-messages]');
        let latest = '';
        let reading = false;
        const refresh = async (body) => {
            const path = select?.value || chatForm.dataset.chatUrl;
            const { items, open } = await api(path, body);
            if (path !== (select?.value || chatForm.dataset.chatUrl)) return;
            const signature = JSON.stringify(items);
            if (signature !== latest) {
                latest = signature;
                log.replaceChildren(...items.map(message => {
                    const row = element('div', '', 'support-item');
                    row.append(element('strong', message.operator ? t('Support', 'پشتیبان') : t('Customer', 'مشتری')));
                    const text = element('p', message.body, 'support-message');
                    text.dir = 'auto';
                    row.append(text);
                    return row;
                }));
            }
            chatForm.classList.toggle('hidden', open === false);
            status.textContent = open === false ? t('Conversation resolved.', 'گفتگو بسته شد.') : '';
        };
        const poll = async () => {
            if (document.hidden || reading || chatForm.querySelector('button[type=submit]').disabled) return;
            reading = true;
            try {
                await refresh();
            } catch (error) {
                status.textContent = error.message;
            } finally {
                reading = false;
            }
        };
        chatForm.addEventListener('submit', (event) => {
            event.preventDefault();
            run(chatForm, async () => {
                const fields = new FormData(chatForm);
                await refresh({ message: fields.get('message'), complete: fields.has('complete') });
                chatForm.reset();
            });
        });
        select?.addEventListener('change', () => {
            latest = '';
            log.replaceChildren();
            poll();
        });
        poll();
        window.setInterval(poll, 12000);
    }

    const roleData = JSON.parse(document.getElementById('support-role-data')?.textContent || 'null');
    const teamPaths = {
        operator: '/operators', policy: '/policy', role: '/roles',
        contract: id => `/operators/${id}/contract`, payroll: id => `/operators/${id}/payroll`,
        pay: id => `/payroll/${id}/pay`, resolve: id => `/activities/${id}/resolve`,
        'review-amount': (id, data) => `/activities/${id}/${data.action}`,
    };

    function addTier(container) {
        const row = element('div', '', 'support-actions');
        for (const [name, text] of [['cap', t('Up to GB', 'تا گیگ')], ['rate', t('Toman per GB', 'تومان هر گیگ')]]) {
            const label = element('label', text, 'form-group');
            const input = element('input');
            input.type = 'number';
            input.min = '0';
            input.max = '2000000000';
            input.dataset.name = name;
            label.append(input);
            row.append(label);
        }
        actionButton(row, t('Remove tier', 'حذف پله'), () => row.remove());
        container.append(row);
    }

    document.querySelectorAll('[data-team-action]').forEach(form => {
        const action = form.dataset.teamAction;
        const tierRows = form.querySelector('[data-tier-rows]');
        if (tierRows) {
            const mode = form.elements.mode;
            const showFields = () => {
                const byMode = { flat: ['create', 'renew'], percent: ['percent'], tiers: ['tiers'] };
                form.querySelectorAll('[data-contract-field]').forEach(field => {
                    const name = field.dataset.contractField;
                    field.classList.toggle('hidden', ['create', 'renew', 'percent', 'tiers'].includes(name) && !byMode[mode.value]?.includes(name));
                });
            };
            form.querySelector('[data-add-tier]').addEventListener('click', () => addTier(tierRows));
            mode.addEventListener('change', showFields);
            addTier(tierRows);
            showFields();
        }
        if (action === 'operator' && roleData) {
            const preview = form.querySelector('[data-permission-preview]');
            const update = () => {
                const allowed = new Set(roleData.roles[form.elements.role_id.value]);
                for (const field of form.querySelectorAll('[name^="permission-"]')) {
                    const key = field.name.slice('permission-'.length);
                    if (field.value === 'allow') allowed.add(key);
                    if (field.value === 'deny') allowed.delete(key);
                }
                const labels = [...allowed].filter(key => !roleData.ceiling || roleData.ceiling.includes(key))
                    .map(key => roleData.caps[key][Number(fa)]);
                const list = labels.join(fa ? '، ' : ', ') || t('None', 'بدون مجوز');
                preview.textContent = `${t('Effective permissions', 'مجوزهای مؤثر')} · ${list}`;
            };
            form.addEventListener('change', update);
            update();
        }
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            if (action === 'pay' && !confirm(t('Record this payout as a paid expense?', 'این پرداخت به‌عنوان هزینهٔ پرداخت‌شده ثبت شود؟'))) return;
            run(form, async () => {
                const fields = new FormData(form);
                const data = Object.fromEntries(fields);
                data.owner_id = Number(root.dataset.owner);
                if (action === 'operator') {
                    data.locations = fields.getAll('locations');
                    data.enabled = fields.has('enabled');
                    data.overrides = {};
                    for (const [key, value] of fields) {
                        if (key.startsWith('permission-') && value) data.overrides[key.slice(11)] = value === 'allow';
                    }
                } else if (action === 'role' || action === 'policy') {
                    data.permissions = fields.getAll('permissions');
                } else if (action === 'contract') {
                    data.basis_points = Math.round(Number(data.percent) * 100);
                    data.tiers = Array.from(tierRows.children, row => {
                        const cap = row.querySelector('[data-name=cap]').value;
                        const rate = row.querySelector('[data-name=rate]').value;
                        return [cap === '' ? null : Number(cap), Number(rate)];
                    });
                }
                const resolver = teamPaths[action];
                const path = typeof resolver === 'function' ? resolver(form.dataset.record, data) : resolver;
                await api('/api/support/team' + path, data);
                location.reload();
            });
        });
    });
})();
