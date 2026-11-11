(function () {
  'use strict';

  function start() {
    const nav = document.querySelector('.subscription-bottom-nav');
    if (!nav) return;

    let launcher = null;
    let original = null;
    let scheduled = false;

    function position() {
      scheduled = false;
      if (!window.matchMedia('(max-width: 639px)').matches) {
        if (launcher && launcher.isConnected && original) {
          for (const name of ['bottom', 'left', 'right']) {
            launcher.style.setProperty(name, original[name].value, original[name].priority);
          }
        }
        launcher = null;
        original = null;
        return;
      }
      const navRect = nav.getBoundingClientRect();
      const bottom = `${Math.ceil(window.innerHeight - navRect.top + 12)}px`;
      if (launcher && launcher.isConnected) {
        launcher.style.setProperty('bottom', bottom, 'important');
        launcher.style.setProperty('left', '12px', 'important');
        launcher.style.setProperty('right', 'auto', 'important');
        return;
      }
      for (const element of document.body.querySelectorAll('*')) {
        if (element.closest('.subscription-bottom-nav, .pwa-sheet, .pwa-sheet-overlay')) continue;
        const style = window.getComputedStyle(element);
        if (style.position !== 'fixed') continue;
        const rect = element.getBoundingClientRect();
        if (rect.width < 35 || rect.width > 110 || rect.height < 35 || rect.height > 110) continue;
        if (rect.left > 120 || rect.bottom < navRect.top || rect.top > navRect.bottom) continue;
        launcher = element;
        original = Object.fromEntries(['bottom', 'left', 'right'].map((name) => [name, {
          value: element.style.getPropertyValue(name), priority: element.style.getPropertyPriority(name)
        }]));
        launcher.style.setProperty('bottom', bottom, 'important');
        launcher.style.setProperty('left', '12px', 'important');
        launcher.style.setProperty('right', 'auto', 'important');
        break;
      }
    }

    function schedule() {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(position);
    }

    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
    window.addEventListener('resize', schedule);
    schedule();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}());
