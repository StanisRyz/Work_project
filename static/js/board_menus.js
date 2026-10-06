/**
 * The tab and column menus of a board (`details[data-board-menu]`).
 *
 * Each menu is a native `<details>` of ordinary POST forms, so it opens and
 * posts without this script. This only makes it behave like a menu: one open
 * at a time, closed by a click elsewhere or Esc, the name field focused when
 * it opens. Nothing here is a rule — the forms post to routes that ask the
 * right and the service again.
 *
 * Listeners are delegated from `document`: the menus live in the structure
 * block, which the live client replaces wholesale (never while a menu is
 * open — see `realtime/boards.js`).
 */
(() => {
    'use strict';

    if (window.qualityBoardMenus) {
        return;
    }

    const openMenus = () => [...document.querySelectorAll('details[data-board-menu][open]')];

    const closeAll = (except) => {
        openMenus().forEach((menu) => {
            if (menu !== except) {
                menu.open = false;
            }
        });
    };

    // `toggle` does not bubble; a capturing listener sees every menu.
    document.addEventListener('toggle', (event) => {
        const menu = event.target;
        if (!menu || typeof menu.matches !== 'function' || !menu.matches('details[data-board-menu]')) {
            return;
        }
        if (menu.open) {
            closeAll(menu);
            // The filter row's «Поля» is a panel of several fields, reopened
            // by `registry_tools.js` with the caret where it was: nothing to
            // take the focus to.
            if (menu.hasAttribute('data-board-menu-keep-focus')) {
                return;
            }
            const field = menu.querySelector('input[type="text"]');
            if (field) {
                field.focus();
                field.select();
            }
        }
    }, true);

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || typeof target.closest !== 'function') {
            return;
        }
        // The confirmation modal opened from a menu is not «elsewhere».
        if (target.closest('[data-confirm-modal]')) {
            return;
        }
        closeAll(target.closest('details[data-board-menu]'));
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') {
            return;
        }
        const menus = openMenus();
        if (!menus.length) {
            return;
        }
        menus.forEach((menu) => {
            menu.open = false;
        });
        const summary = menus[0].querySelector('summary');
        if (summary) {
            summary.focus();
        }
    });

    window.qualityBoardMenus = { closeAll };
})();
