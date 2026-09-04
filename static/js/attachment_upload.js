/**
 * Attachments: picking the file *is* the upload.
 *
 * One behaviour for every `[data-attachment-upload]` form — акты, протоколы,
 * задачи — so no module has an upload script of its own. On `change` the
 * chosen names are written into the shared confirmation modal and the hidden
 * trigger is clicked, exactly as the documentation library already does it:
 * `confirm_modal.js` owns the dialog, and nothing here reimplements one.
 *
 * «Загрузить» submits the form the file was picked in — the same endpoint, the
 * same CSRF token, the same fields. «Нет» clears the selection, so picking the
 * same file again reopens the dialog instead of silently doing nothing.
 *
 * The form's own «Добавить вложение» button is the path for a browser without
 * JavaScript and is hidden here once the dialog is known to work: two primary
 * upload actions on one form is the confusion this replaces. No rule lives
 * here either — the server re-checks the permission, the file type and the
 * size, and a selection this script never saw is validated the same way.
 */
(() => {
    'use strict';

    const forms = [...document.querySelectorAll('[data-attachment-upload]')];
    if (!forms.length) {
        return;
    }
    const dialog = document.querySelector('[data-confirm-modal]');
    // Without a usable dialog there is no confirmation step: the forms keep
    // their visible submit button and behave exactly as they did before.
    const enhanced = Boolean(dialog) && typeof dialog.showModal === 'function';

    // The form whose confirmation is on screen, so the dialog's `close` event
    // is only acted on when it belongs to an upload.
    let awaiting = null;

    const describe = (files) => {
        const names = Array.from(files, (file) => file.name);
        if (names.length === 1) {
            return `Файл: «${names[0]}».`;
        }
        return `Файлов: ${names.length}. ${names.join(', ')}.`;
    };

    forms.forEach((form) => {
        const input = form.querySelector('input[type="file"]');
        const trigger = form.querySelector('[data-attachment-upload-confirm]');
        if (!input || !trigger) {
            return;
        }
        // The picker's own label, shared with the act and protocol markup: the
        // chosen name replaces the placeholder and the card turns green.
        const picker = form.querySelector('.attachment-picker');
        const label = form.querySelector('[data-attachment-file-name]');
        const browse = form.querySelector('[data-attachment-file-trigger]');
        const placeholder = label ? label.textContent : '';

        if (browse) {
            browse.addEventListener('click', () => input.click());
        }
        if (enhanced) {
            form.querySelectorAll('[data-attachment-upload-submit]').forEach((button) => {
                button.hidden = true;
            });
        }

        const clear = () => {
            input.value = '';
            if (label) {
                label.textContent = placeholder;
            }
            if (picker) {
                picker.classList.remove('attachment-picker--selected');
            }
        };
        form.addEventListener('attachment-upload:clear', clear);

        input.addEventListener('change', () => {
            const files = input.files;
            if (!files || files.length === 0) {
                clear();
                return;
            }
            if (label) {
                label.textContent = Array.from(files, (file) => file.name).join(', ');
            }
            if (picker) {
                picker.classList.add('attachment-picker--selected');
            }
            if (!enhanced) {
                return;
            }
            trigger.dataset.confirmTitle = files.length === 1
                ? 'Загрузить файл?'
                : 'Загрузить файлы?';
            trigger.dataset.confirmText = describe(files);
            awaiting = form;
            trigger.click();
        });
    });

    if (enhanced) {
        dialog.addEventListener('close', () => {
            // Only a cancel or Escape gets here: on confirm the modal submits
            // the form and the page navigates away.
            if (!awaiting) {
                return;
            }
            awaiting.dispatchEvent(new Event('attachment-upload:clear'));
            awaiting = null;
        });
    }
})();
