/**
 * «Свернуть панель» of the boards frame (`boards/layout.html`).
 *
 * A convenience of this browser only: the collapsed state is kept in
 * `localStorage` (every access in try/catch — a private window or blocked
 * storage simply forgets it). With nothing remembered the panel starts
 * collapsed below 1240px, where the columns need the width. An open card
 * changes nothing here: its drawer lies over the columns and takes no width
 * from them. Without JavaScript the panel is always open. Nothing here is a
 * rule: the panel lists only what the server drew.
 */
(() => {
    'use strict';

    const STORAGE_KEY = 'quality.boards.navCollapsed';
    const NARROW = '(max-width: 1240px)';

    const shell = document.querySelector('[data-board-shell]');
    const toggle = shell && shell.querySelector('[data-board-nav-toggle]');
    if (!shell || !toggle) {
        return;
    }

    const remembered = () => {
        try {
            const value = window.localStorage.getItem(STORAGE_KEY);
            return value === null ? null : value === '1';
        } catch (error) {
            return null;
        }
    };

    const remember = (collapsed) => {
        try {
            window.localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
        } catch (error) {
            // Storage refused: the state lasts as long as this page.
        }
    };

    const apply = (collapsed) => {
        shell.classList.toggle('board-shell--collapsed', collapsed);
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        const label = collapsed ? 'Развернуть панель досок' : 'Свернуть панель досок';
        toggle.setAttribute('aria-label', label);
        toggle.title = collapsed ? 'Развернуть панель' : 'Свернуть панель';
    };

    const matches = (query) => typeof window.matchMedia === 'function' && window.matchMedia(query).matches;
    const stored = remembered();
    apply(stored === null ? matches(NARROW) : stored);

    toggle.addEventListener('click', () => {
        const collapsed = !shell.classList.contains('board-shell--collapsed');
        apply(collapsed);
        remember(collapsed);
    });
})();
