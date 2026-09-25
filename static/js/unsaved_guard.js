/**
 * «Покинуть страницу? Несохранённые данные будут потеряны».
 *
 * Long forms — an act, the «Проработка» tab of one, the protocol editor, an
 * СМК record, a task's «Выполнение» — are filled for many minutes, and a
 * reload, a stray click on the sidebar or on a notification used to discard
 * everything typed without a word. Any element marked `[data-unsaved-guard]`
 * (a form, or a container of forms such as the act's live work tab) arms the
 * browser's own leave-page prompt once a real user gesture changes a field
 * inside it.
 *
 * That prompt is the one thing the application modal cannot replace: nothing
 * but `beforeunload` can hold a navigation back, and the browser writes its
 * text itself. It never decides anything and never blocks a submission of the
 * guarded form — submitting it (natively or through the confirmation modal's
 * `requestSubmit()`) disarms the prompt first. A submit handler that holds the
 * submission back with `preventDefault()` (the СМК confirmation step) keeps it
 * armed, because the page is still there.
 *
 * Delegated from `window`, so a live fragment replacement needs no re-binding.
 */
(() => {
    'use strict';

    if (window.qualityUnsavedGuard) {
        return;
    }

    // A page the server re-rendered from a rejected submission already holds
    // the user's own input: it is marked `data-unsaved-guard="dirty"`.
    let dirty = Boolean(document.querySelector('[data-unsaved-guard="dirty"]'));

    const isGuarded = (target) =>
        Boolean(target && typeof target.closest === 'function' && target.closest('[data-unsaved-guard]'));

    const markDirty = (event) => {
        if (event.isTrusted === false) {
            return;
        }
        if (isGuarded(event.target)) {
            dirty = true;
        }
    };

    document.addEventListener('input', markDirty, true);
    document.addEventListener('change', markDirty, true);

    /**
     * Does submitting this form keep what was typed?
     *
     * The guarded form itself, a form inside a guarded region (the act's work
     * tab holds several), and a form that carries the typed text along — the
     * task's attachment forms post the «Выполнение» draft through
     * `[data-attachment-carry-from]`. Any other form (the bug report, a return
     * confirmed in the shared modal) leaves the page with the input unsaved,
     * so the browser still asks.
     */
    const keepsInput = (form) =>
        Boolean(
            form
            && typeof form.closest === 'function'
            && (form.closest('[data-unsaved-guard]')
                || form.querySelector('[data-unsaved-guard], [data-attachment-carry-from]')),
        );

    // Bubbling on `window`: it runs after the form's own handlers, so a
    // submission somebody deliberately held back is still unsaved input.
    window.addEventListener('submit', (event) => {
        if (!event.defaultPrevented && keepsInput(event.target)) {
            dirty = false;
        }
    });

    window.addEventListener('beforeunload', (event) => {
        if (!dirty) {
            return;
        }
        event.preventDefault();
        // Older engines only show the prompt when `returnValue` is set.
        event.returnValue = '';
    });

    window.qualityUnsavedGuard = {
        get isDirty() {
            return dirty;
        },
        markClean() {
            dirty = false;
        },
        // A restored draft (`form_drafts.js`) is unsaved input too.
        markDirty() {
            dirty = true;
        },
    };
})();
