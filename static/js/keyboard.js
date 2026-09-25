/*
 * Two keys, the same on every page. Presentation only — each one does exactly
 * what a click already does.
 *
 * «/» puts the caret into the page's own list search (`[data-registry-search]`)
 * or, on a page without one, into the topbar's quick search. Never while the
 * user is typing somewhere else.
 *
 * Ctrl+Enter (⌘+Enter) submits the form the caret is in — only a form that
 * opts in with `data-hotkey-submit`, where there is exactly one thing to send
 * (a comment, the «Выполнение» of a task). Forms with several submit buttons
 * or a confirmation step are left alone on purpose. `requestSubmit()` runs the
 * browser's own validation and the form's own `submit` listeners, as a click
 * on its button would.
 */
(function () {
    'use strict';

    const isTyping = (element) => Boolean(element) && (
        element.isContentEditable
        || ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)
    );

    document.addEventListener('keydown', (event) => {
        if (event.defaultPrevented) {
            return;
        }
        if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !isTyping(event.target)) {
            const target = document.querySelector('[data-registry-search]')
                || document.querySelector('[data-quick-search]');
            if (target) {
                event.preventDefault();
                target.focus();
                target.select();
            }
            return;
        }
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
            const form = event.target.closest && event.target.closest('form[data-hotkey-submit]');
            if (!form) {
                return;
            }
            event.preventDefault();
            if (typeof form.requestSubmit === 'function') {
                form.requestSubmit();
            } else {
                form.submit();
            }
            return;
        }
        if (event.key === 'Escape' && event.target.matches && event.target.matches('[data-quick-search]')) {
            event.target.blur();
        }
    });
})();
