/**
 * Documentation: the «+» upload control.
 *
 * Picking files *is* the action — there is no separate submit button. On
 * `change` this fills the shared confirmation modal with the chosen names and
 * clicks the hidden trigger, so an upload is confirmed through exactly the
 * same dialog as every other action in the project (`confirm_modal.js` owns
 * the dialog; nothing here reimplements it).
 *
 * Cancelling clears the selection, so picking the same files again reopens the
 * dialog instead of silently doing nothing. No business rule lives here: the
 * server re-checks the role, the folder and every file.
 */
(() => {
    'use strict';

    const form = document.querySelector('[data-document-upload]');
    if (!form) {
        return;
    }

    const input = form.querySelector('input[type="file"]');
    const trigger = form.querySelector('[data-document-upload-confirm]');
    const dialog = document.querySelector('[data-confirm-modal]');
    if (!input || !trigger) {
        return;
    }

    // Set while our own confirmation is on screen, so the shared dialog's
    // `close` event is only acted on when it belongs to this upload.
    let awaitingConfirmation = false;

    const describe = (files) => {
        const names = Array.from(files, (file) => file.name);
        const folder = form.dataset.folderName || '';
        const where = folder ? ` в папку «${folder}»` : '';
        if (names.length === 1) {
            return `Загрузить файл «${names[0]}»${where}?`;
        }
        return `Загрузить файлы (${names.length})${where}: ${names.join(', ')}?`;
    };

    input.addEventListener('change', () => {
        if (!input.files || input.files.length === 0) {
            return;
        }
        trigger.dataset.confirmText = describe(input.files);
        awaitingConfirmation = true;
        trigger.click();
    });

    if (dialog) {
        dialog.addEventListener('close', () => {
            // Only a cancel or Escape gets here: on confirm the modal submits
            // the form and the page navigates away.
            if (!awaitingConfirmation) {
                return;
            }
            awaitingConfirmation = false;
            input.value = '';
        });
    }
})();
