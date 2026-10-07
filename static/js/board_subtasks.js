/**
 * «Позже срока карточки ZAP-12: 30.10.2026» beside a subtask's срок.
 *
 * A subtask may be due later than the card it lives in — that is not a
 * refusal, only a warning. The server draws it: a form holding a subtask's
 * срок (`input[data-subtask-due]`, the new subtask's row on «Подзадачи» or a
 * subtask's own edit form) carries the warning `[data-subtask-due-warning]`
 * with the card's срок in `data-parent-due` (ISO), shown when the stored
 * срок is later. This script only shows and hides it as the date is
 * changed, and once when a form arrives (a drawer opened without a reload is
 * a `window.qualityFragments` initialiser). Nothing here is a rule: the date
 * is posted as it was typed and the service stores it either way.
 */
(() => {
    'use strict';

    if (window.qualityBoardSubtasks) {
        return;
    }

    const check = (form) => {
        if (!form || typeof form.querySelector !== 'function') {
            return;
        }
        const warning = form.querySelector('[data-subtask-due-warning]');
        const input = form.querySelector('input[data-subtask-due]');
        if (!warning || !input) {
            return;
        }
        const parentDue = warning.getAttribute('data-parent-due') || '';
        // ISO dates compare as strings: «2026-10-31» > «2026-10-30».
        warning.hidden = !(input.value && parentDue && input.value > parentDue);
    };

    const initialise = (root) => {
        const scope = root && typeof root.querySelectorAll === 'function' ? root : document;
        scope.querySelectorAll('[data-subtask-due-warning]').forEach((warning) => {
            check(warning.closest('form'));
        });
    };

    const onChange = (event) => {
        const target = event.target;
        if (target && typeof target.matches === 'function' && target.matches('input[data-subtask-due]')) {
            check(target.closest('form'));
        }
    };
    document.addEventListener('input', onChange);
    document.addEventListener('change', onChange);

    if (window.qualityFragments) {
        window.qualityFragments.register('board-subtasks', initialise);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => initialise(document));
    } else {
        initialise(document);
    }

    window.qualityBoardSubtasks = { initialise, check };
})();
