/**
 * «Причина переноса» on a card's edit form, shown while the срок moves.
 *
 * A срок that existed is moved only with a reason — `update_card()` decides
 * and refuses without one; this script decides nothing. The server draws the
 * two fields (`[data-due-reason]`) always, marked «— если меняете срок»: that
 * is the page without JavaScript. With it, they are hidden while the date in
 * the form (`input[data-stored-due]`) is the stored one, shown as soon as it
 * differs, and the mark goes away. A block holding an error or a chosen
 * reason (a refused save) stays shown. A `window.qualityFragments`
 * initialiser, so a drawer opened without a reload is wired too; listeners
 * are delegated from `document`.
 */
(() => {
    'use strict';

    if (window.qualityBoardDue) {
        return;
    }

    const sync = (form) => {
        if (!form || typeof form.querySelector !== 'function') {
            return;
        }
        const block = form.querySelector('[data-due-reason]');
        const input = form.querySelector('input[data-stored-due]');
        if (!block || !input) {
            return;
        }
        const select = block.querySelector('select');
        const holds = Boolean(block.querySelector('.errorlist') || (select && select.value));
        const moved = Boolean(input.value) && input.value !== input.getAttribute('data-stored-due');
        block.hidden = !(moved || holds);
        const hint = block.querySelector('[data-due-reason-hint]');
        if (hint) {
            hint.hidden = true;
        }
    };

    const initialise = (root) => {
        const scope = root && typeof root.querySelectorAll === 'function' ? root : document;
        scope.querySelectorAll('[data-due-reason]').forEach((block) => sync(block.closest('form')));
    };

    const onChange = (event) => {
        const target = event.target;
        if (target && typeof target.matches === 'function' && target.matches('input[data-stored-due]')) {
            sync(target.closest('form'));
        }
    };
    document.addEventListener('input', onChange);
    document.addEventListener('change', onChange);

    if (window.qualityFragments) {
        window.qualityFragments.register('board-due', initialise);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => initialise(document));
    } else {
        initialise(document);
    }

    window.qualityBoardDue = { initialise, sync };
})();
