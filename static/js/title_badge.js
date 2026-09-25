/*
 * «(3) Акты · Экосистема качества»: the unread-notification count in front of
 * the browser tab's title, so a background tab says there is something new.
 *
 * The number is the bell's own `[data-notification-counter]`, which the server
 * renders and the realtime client keeps current; this only mirrors it, so the
 * tab and the bell cannot disagree. Nothing is fetched here.
 */
(function () {
    'use strict';

    const counter = document.querySelector('[data-notification-counter]');
    if (!counter) {
        return;
    }
    const base = document.title.replace(/^\(\d+\+?\)\s+/, '');

    const sync = () => {
        const value = counter.hidden ? '' : counter.textContent.trim();
        const unread = value && value !== '0' ? `(${value}) ` : '';
        const next = `${unread}${base}`;
        if (document.title !== next) {
            document.title = next;
        }
    };

    sync();
    new MutationObserver(sync).observe(counter, {
        attributes: true,
        attributeFilter: ['hidden'],
        childList: true,
        characterData: true,
        subtree: true,
    });
})();
