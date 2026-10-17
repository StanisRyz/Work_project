/*
 * «Утилизация МП»: «Выбрать все» and the count on «Утилизировать».
 *
 * Presentation only, delegated from `document`. Without JavaScript the tick
 * boxes post as they are, and an empty selection comes back with the server's
 * message — the button is never the rule.
 */
(() => {
    const items = () => Array.from(document.querySelectorAll('[data-disposal-item]'));

    const sync = () => {
        const checked = items().filter((box) => box.checked).length;
        const button = document.querySelector('[data-disposal-submit]');
        const count = document.querySelector('[data-disposal-count]');
        const all = document.querySelector('[data-disposal-all]');
        if (button) {
            button.disabled = checked === 0;
        }
        if (count) {
            count.textContent = checked ? ` (${checked})` : '';
        }
        if (all) {
            all.checked = checked > 0 && checked === items().length;
            all.indeterminate = checked > 0 && checked < items().length;
        }
    };

    document.addEventListener('change', (event) => {
        if (event.target.matches('[data-disposal-all]')) {
            items().forEach((box) => { box.checked = event.target.checked; });
        }
        if (event.target.matches('[data-disposal-all], [data-disposal-item]')) {
            sync();
        }
    });

    document.addEventListener('DOMContentLoaded', sync);
})();
