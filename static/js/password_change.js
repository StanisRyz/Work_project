/**
 * «Сменить пароль» as a dialog.
 *
 * A progressive upgrade of one link, nothing more. The profile menu holds a
 * real `<a>` to `accounts:password_change`; where `<dialog>` is usable this
 * intercepts the click and opens the topbar's own form instead, so the user
 * changes their password without leaving the page they were on. Where it is
 * not, the link is left exactly as the server rendered it and the full page
 * does the job.
 *
 * No rule of consequence lives here. The current password is verified, the new
 * one validated and hashed, and the session re-signed by
 * `accounts.views.AppPasswordChangeView` — this only saves a round trip on the
 * one mistake a browser can see for itself, the two new passwords disagreeing.
 * Field values are cleared whenever the dialog closes, so a password is never
 * left sitting in the DOM behind a closed dialog.
 */
(() => {
    'use strict';

    const dialog = document.querySelector('[data-password-modal]');
    const opener = document.querySelector('[data-password-modal-open]');
    if (!dialog || !opener || typeof dialog.showModal !== 'function') {
        return;
    }

    const form = dialog.querySelector('[data-password-modal-form]');
    const cancel = dialog.querySelector('[data-password-modal-cancel]');
    const error = dialog.querySelector('[data-password-modal-error]');
    const current = dialog.querySelector('[data-password-modal-current]');
    const next = dialog.querySelector('[data-password-modal-new]');
    const repeat = dialog.querySelector('[data-password-modal-repeat]');
    if (!form || !cancel || !error || !current || !next || !repeat) {
        return;
    }

    const clearError = () => {
        error.textContent = '';
        error.hidden = true;
    };

    opener.addEventListener('click', (event) => {
        event.preventDefault();
        // The menu is a <details>: leaving it open would keep the popup
        // hanging over the backdrop.
        const menu = opener.closest('details');
        if (menu) {
            menu.open = false;
        }
        clearError();
        dialog.showModal();
        current.focus();
    });

    cancel.addEventListener('click', () => dialog.close());

    dialog.addEventListener('close', () => {
        form.reset();
        clearError();
    });

    [next, repeat].forEach((field) => {
        field.addEventListener('input', () => {
            if (next.value === repeat.value) {
                clearError();
            }
        });
    });

    form.addEventListener('submit', (event) => {
        if (next.value !== repeat.value) {
            event.preventDefault();
            error.textContent = 'Новые пароли не совпадают.';
            error.hidden = false;
            repeat.focus();
        }
    });
})();
