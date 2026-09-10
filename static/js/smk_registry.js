/**
 * The СМК registry's one interaction: the arrow next to «Срок выполнения»
 * expands the record's мероприятия.
 *
 * The list is a second `<tr>` rendered right after the record's own row and
 * hidden with the `hidden` attribute — not a popup. `.act-table-card` scrolls
 * inside itself, so an absolutely positioned panel would be clipped at the
 * bottom of the list, and putting the list inside the «Срок» cell would widen
 * that column for every row. A full-width row is neither, and it scrolls with
 * the table it belongs to.
 *
 * The toggle is a `<button>`, which `clickable_rows.js` already counts as a
 * control of its own, so opening a row never navigates to the record. The
 * listener is delegated on `document` for the same reason that one is: the
 * table is re-rendered as a whole, and a delegated handler keeps working on
 * markup that arrived after page load.
 */
(() => {
    'use strict';

    document.addEventListener('click', (event) => {
        const toggle = event.target.closest
            ? event.target.closest('[data-smk-tasks-toggle]')
            : null;
        if (!toggle) {
            return;
        }
        const list = document.getElementById(toggle.getAttribute('aria-controls'));
        if (!list) {
            return;
        }
        // `aria-expanded` is the state, and `hidden` follows it — one answer
        // for the screen reader and the layout rather than two that could drift.
        const expanded = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        toggle.setAttribute(
            'aria-label',
            expanded ? 'Показать мероприятия аудита' : 'Скрыть мероприятия аудита',
        );
        list.hidden = expanded;
    });
})();
