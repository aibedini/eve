(function () {
  'use strict';

  const fa = (document.documentElement.lang || '').toLowerCase().startsWith('fa');
  const labels = fa
    ? { warning: 'پرداخت مشابه در ۲۴ ساعت گذشته ثبت شده؛ دوباره بررسی کنید.', error: 'بررسی پرداخت انجام نشد. دوباره تلاش کنید.', amount: 'تومان' }
    : { warning: 'Similar payment declared in the past 24 hours. Double-check it.', error: 'Payment check failed. Please try again.', amount: 'Toman' };

  function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(fa ? 'fa-IR-u-ca-persian' : 'en-US', {
      dateStyle: 'short', timeStyle: 'short',
      timeZone: window.EveDate?.timezone || 'Asia/Tehran'
    }).format(date);
  }

  function setup(prefix) {
    const sender = document.getElementById(`${prefix}-sender-card`);
    const destination = document.getElementById(`${prefix}-dest-card`);
    const icon = document.getElementById(`${prefix}-card-check-icon`);
    const results = document.getElementById(`${prefix}-card-check-results`);
    if (!sender || !destination || !icon || !results) return;

    let timer;
    let controller;
    let generation = 0;
    function clear() {
      icon.classList.add('hidden');
      results.classList.add('hidden');
      results.replaceChildren();
    }
    function schedule() {
      generation += 1;
      clearTimeout(timer);
      controller?.abort();
      clear();
      const digits = sender.value.match(/\p{Nd}/gu) || [];
      if (digits.length < 4 || !destination.value) return;
      const current = generation;
      timer = setTimeout(async () => {
        controller = new AbortController();
        const params = new URLSearchParams({ sender_card: sender.value.trim(), card_id: destination.value });
        try {
          const response = await fetch(`/api/payments/check-sender-card?${params}`, {
            credentials: 'same-origin', signal: controller.signal, cache: 'no-store'
          });
          if (!response.ok) throw new Error('check failed');
          const payload = await response.json();
          if (current !== generation) return;
          const matches = Array.isArray(payload.matches) ? payload.matches : [];
          if (!matches.length) {
            icon.classList.remove('hidden');
            return;
          }
          const heading = document.createElement('strong');
          heading.textContent = labels.warning;
          const list = document.createElement('ul');
          for (const match of matches) {
            const item = document.createElement('li');
            const amount = new Intl.NumberFormat(fa ? 'fa-IR' : 'en-US').format(match.amount);
            item.textContent = `${match.user} · ${formatDate(match.created_at)} · ${amount} ${labels.amount}`;
            list.append(item);
          }
          results.append(heading, list);
          results.classList.remove('hidden');
        } catch (error) {
          if (error.name === 'AbortError' || current !== generation) return;
          results.textContent = labels.error;
          results.classList.remove('hidden');
        }
      }, 250);
    }
    sender.addEventListener('input', schedule);
    destination.addEventListener('change', schedule);
  }

  function start() {
    for (const prefix of ['renew', 'reset', 'add-client']) setup(prefix);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}());
