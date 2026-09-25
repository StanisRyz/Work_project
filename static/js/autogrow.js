/*
 * Text areas grow with what is typed into them, up to 60 % of the window, so a
 * long description never hides behind its own scrollbar and nobody has to
 * drag the corner. Never smaller than the height the page drew them at.
 *
 * Delegated from `document`, so a textarea inside a live-replaced fragment or
 * a row added by an editor script grows too without being wired again. A
 * textarea marked `data-autogrow="off"` keeps its fixed size.
 */
(function () {
    'use strict';

    const MAX_SHARE = 0.6;

    const grow = (area) => {
        if (!(area instanceof HTMLTextAreaElement) || area.dataset.autogrow === 'off' || !area.offsetParent) {
            return;
        }
        if (!area.dataset.autogrowMin) {
            area.dataset.autogrowMin = String(area.offsetHeight);
        }
        const minimum = Number(area.dataset.autogrowMin) || 0;
        const maximum = Math.round(window.innerHeight * MAX_SHARE);
        area.style.height = 'auto';
        const border = area.offsetHeight - area.clientHeight;
        const wanted = area.scrollHeight + border;
        area.style.height = `${Math.min(Math.max(wanted, minimum), maximum)}px`;
        area.style.overflowY = wanted > maximum ? 'auto' : 'hidden';
    };

    const growAll = () => document.querySelectorAll('textarea').forEach((area) => {
        if (area.value) {
            grow(area);
        }
    });

    document.addEventListener('input', (event) => grow(event.target));
    // A draft restored by `form_drafts.js` arrives all at once.
    document.addEventListener('quality:form-restored', growAll);
    // A collapsed section (`<details>`) has no size until it is opened.
    document.addEventListener('toggle', (event) => {
        if (event.target.open) {
            event.target.querySelectorAll('textarea').forEach(grow);
        }
    }, true);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', growAll);
    } else {
        growAll();
    }
})();
