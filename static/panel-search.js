(function () {
    'use strict';

    function start() {
        const trigger = document.getElementById('panel-search-trigger');
        const overlay = document.getElementById('panel-search-overlay');
        const input = document.getElementById('panel-search-input');
        const results = document.getElementById('panel-search-results');
        const closeButton = document.getElementById('panel-search-close');
        if (!trigger || !overlay || !input || !results || !closeButton) return;
        const fa = document.documentElement.lang.toLowerCase().startsWith('fa');
        const descriptions = {
            dashboard: ['نمای کلی و آمار پنل', 'Panel overview and statistics'],
            monitor: ['پایش سرورها و سرویس‌ها', 'Server and service monitoring'],
            client: ['مدیریت کاربران و اشتراک‌ها', 'Manage clients and subscriptions'],
            sms: ['ارسال، گزارش و تنظیمات پیامک', 'SMS delivery, reports and settings'],
            telegram: ['اتصال و پیام‌های تلگرام', 'Telegram connection and messages'],
            setting: ['پیکربندی پنل', 'Panel configuration'],
            finance: ['مدیریت مالی و پرداخت‌ها', 'Payments and finance']
        };
        const smsTabs = [
            ['overview', 'نمای کلی پیامک', 'SMS overview', 'سلامت درگاه و آمار ارسال', 'Gateway health and sending statistics'],
            ['audience', 'مخاطبان پیامک', 'SMS audience', 'انتخاب و بررسی گیرندگان', 'Review and select recipients'],
            ['runs', 'اجرای پیامک', 'SMS runs', 'کمپین‌ها و اجراهای پیامک', 'SMS campaigns and runs'],
            ['delivery', 'تحویل پیامک', 'SMS delivery', 'نتیجه و گزارش تحویل', 'Delivery results and reports'],
            ['debt', 'بدهی پیامک', 'SMS debt', 'پیامک‌های معوق و بدهی', 'Pending messages and debt'],
            ['gateway', 'درگاه پیامک', 'SMS gateway', 'وضعیت و اتصال درگاه', 'Gateway status and connection']
        ];
        const normalize = (value) => String(value || '').toLocaleLowerCase().replace(/ي/g, 'ی').replace(/ك/g, 'ک').trim();
        const pages = [];
        for (const link of document.querySelectorAll('.sidebar-nav a.nav-item[href]')) {
            const url = new URL(link.href, location.href);
            if (url.origin !== location.origin) continue;
            const title = link.textContent.trim().replace(/\s+/g, ' ');
            if (!title) continue;
            const path = url.pathname.toLowerCase();
            const key = Object.keys(descriptions).find((candidate) => path.includes(candidate));
            pages.push({ title, description: key ? descriptions[key][fa ? 0 : 1] : (fa ? 'باز کردن این بخش از پنل' : 'Open this panel section'), href: url.href, search: title + ' ' + path });
            if (key === 'sms') {
                for (const [hash, titleFa, titleEn, detailFa, detailEn] of smsTabs) {
                    pages.push({
                        title: fa ? titleFa : titleEn,
                        description: fa ? detailFa : detailEn,
                        href: url.pathname + '#' + hash,
                        search: 'sms پیامک ' + hash + ' ' + titleFa + ' ' + titleEn
                    });
                }
            }
            if (key === 'setting' && document.querySelector('.sidebar-nav a[href*="sms-center"]')) {
                pages.push({
                    title: fa ? 'تنظیمات پیامک' : 'SMS settings',
                    description: fa ? 'درگاه و تنظیمات ارسال پیامک' : 'SMS gateway and sending configuration',
                    href: url.pathname + '#tab-sms',
                    search: 'sms پیامک settings تنظیمات درگاه gateway'
                });
            }
        }

        function render() {
            const query = normalize(input.value);
            const matches = pages.map((page) => {
                const title = normalize(page.title);
                const haystack = normalize(page.search + ' ' + page.description);
                const score = !query ? 1 : title === query ? 4 : title.startsWith(query) ? 3 : haystack.includes(query) ? 2 : 0;
                return { page, score };
            }).filter((item) => item.score).sort((a, b) => b.score - a.score).slice(0, 12);
            results.replaceChildren();
            if (!matches.length) {
                const empty = document.createElement('p');
                empty.className = 'panel-search-empty';
                empty.textContent = fa ? 'بخشی پیدا نشد.' : 'No sections found.';
                results.append(empty);
                return;
            }
            for (const { page } of matches) {
                const link = document.createElement('a');
                link.className = 'panel-search-result';
                link.href = page.href;
                link.setAttribute('role', 'listitem');
                const title = document.createElement('strong');
                title.textContent = page.title;
                const description = document.createElement('small');
                description.textContent = page.description;
                link.append(title, description);
                results.append(link);
            }
        }

        function open() {
            overlay.classList.remove('hidden');
            input.value = '';
            render();
            input.focus();
        }
        function close() {
            overlay.classList.add('hidden');
            trigger.focus();
        }
        trigger.addEventListener('click', open);
        closeButton.addEventListener('click', close);
        overlay.addEventListener('click', (event) => { if (event.target === overlay) close(); });
        input.addEventListener('input', render);
        input.addEventListener('keydown', (event) => {
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                results.querySelector('a')?.focus();
            }
            if (event.key === 'Enter') results.querySelector('a')?.click();
        });
        document.addEventListener('keydown', (event) => {
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
                event.preventDefault();
                overlay.classList.contains('hidden') ? open() : close();
            } else if (event.key === 'Escape' && !overlay.classList.contains('hidden')) {
                close();
            }
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
}());
