/**
 * The single application confirmation modal.
 *
 * Replaces every browser confirm()/prompt() in the workflow. One <dialog> per
 * page (`includes/confirm_modal.html`) is filled from the data attributes of
 * the trigger that was clicked:
 *
 *   data-confirm                     marks the trigger
 *   data-confirm-title               modal heading
 *   data-confirm-text                short description (optional)
 *   data-confirm-label               confirm button caption
 *   data-confirm-variant             confirm button modifier: warning | danger
 *   data-confirm-url                 POST target — the modal posts its own form
 *   data-confirm-form                id of an existing form to submit instead
 *   data-confirm-form-action         POST target for that form (default: its own action)
 *   data-confirm-comment="required"  show a mandatory comment field
 *   data-confirm-comment-label       caption of that field
 *   data-confirm-comment-name        posted field name (default: comment)
 *
 * The click handler is delegated on `document`, so triggers that arrive with a
 * live fragment replacement keep working without being re-bound. No business
 * rule lives here: the server re-checks role, status and the comment.
 */
(() => {
    'use strict';

    const dialog = document.querySelector('[data-confirm-modal]');
    if (!dialog || typeof dialog.showModal !== 'function') {
        return;
    }

    const form = dialog.querySelector('[data-confirm-modal-form]');
    const title = dialog.querySelector('[data-confirm-modal-title]');
    const text = dialog.querySelector('[data-confirm-modal-text]');
    const commentLabel = dialog.querySelector('[data-confirm-modal-comment]');
    const commentLabelText = dialog.querySelector('[data-confirm-modal-comment-label]');
    const comment = dialog.querySelector('[data-confirm-modal-comment-input]');
    const error = dialog.querySelector('[data-confirm-modal-error]');
    const cancel = dialog.querySelector('[data-confirm-modal-cancel]');
    const accept = dialog.querySelector('[data-confirm-modal-accept]');
    const VARIANTS = ['link-button--warning', 'link-button--danger'];
    const DEFAULT_COMMENT_ERROR = 'Укажите комментарий.';

    let trigger = null;
    let targetForm = null;
    let commentRequired = false;
    let submitting = false;

    const showError = (message) => {
        error.textContent = message;
        error.hidden = false;
    };

    const clearError = () => {
        error.textContent = '';
        error.hidden = true;
    };

    const close = () => {
        if (dialog.open) {
            dialog.close();
        }
    };

    const open = (button) => {
        trigger = button;
        targetForm = button.dataset.confirmForm
            ? document.getElementById(button.dataset.confirmForm)
            : null;
        commentRequired = button.dataset.confirmComment === 'required';
        submitting = false;

        title.textContent = button.dataset.confirmTitle || 'Подтвердите действие';
        text.textContent = button.dataset.confirmText || '';
        accept.textContent = button.dataset.confirmLabel || 'Подтвердить';
        accept.disabled = false;
        accept.classList.remove(...VARIANTS);
        if (button.dataset.confirmVariant) {
            accept.classList.add(`link-button--${button.dataset.confirmVariant}`);
        }

        commentLabel.hidden = !commentRequired;
        comment.value = '';
        if (commentRequired) {
            commentLabelText.textContent = button.dataset.confirmCommentLabel || 'Комментарий';
            comment.name = button.dataset.confirmCommentName || 'comment';
            comment.placeholder = button.dataset.confirmCommentPlaceholder || '';
        } else {
            // Without a name the field is never posted, so an optional-comment
            // action cannot smuggle an empty value into the view.
            comment.removeAttribute('name');
        }

        // Only the standalone mode posts this form; in form mode the existing
        // page form keeps its own fields. Its action is overridden only when
        // the trigger names another endpoint — that is how one editor form can
        // be posted either to «сохранить» or to «отправить на согласование»
        // with exactly the content currently on screen.
        form.action = targetForm ? '' : button.dataset.confirmUrl || '';
        if (targetForm && button.dataset.confirmFormAction) {
            targetForm.action = button.dataset.confirmFormAction;
        }

        clearError();
        dialog.showModal();
        (commentRequired ? comment : accept).focus();
    };

    document.addEventListener('click', (event) => {
        const button = event.target.closest ? event.target.closest('[data-confirm]') : null;
        if (!button || button.disabled) {
            return;
        }
        event.preventDefault();
        const named = button.dataset.confirmForm;
        const linked = named ? document.getElementById(named) : null;
        if (named && !linked) {
            return;
        }
        // Let the browser point at a missing required field before the modal
        // covers the page: a dialog makes everything behind it inert.
        if (linked && typeof linked.reportValidity === 'function' && !linked.reportValidity()) {
            return;
        }
        open(button);
    });

    cancel.addEventListener('click', close);

    dialog.addEventListener('close', () => {
        submitting = false;
        accept.disabled = false;
        if (trigger && typeof trigger.focus === 'function' && document.contains(trigger)) {
            trigger.focus();
        }
        trigger = null;
    });

    comment.addEventListener('input', () => {
        if (comment.value.trim()) {
            clearError();
        }
    });

    form.addEventListener('submit', (event) => {
        if (submitting) {
            event.preventDefault();
            return;
        }
        if (commentRequired && !comment.value.trim()) {
            event.preventDefault();
            showError(
                (trigger && trigger.dataset.confirmCommentError) || DEFAULT_COMMENT_ERROR,
            );
            comment.focus();
            return;
        }
        submitting = true;
        if (targetForm) {
            event.preventDefault();
            accept.disabled = true;
            if (typeof targetForm.requestSubmit === 'function') {
                targetForm.requestSubmit();
            } else {
                targetForm.submit();
            }
            return;
        }
        // Standalone POST: let the modal's own form submit natively so the
        // server-rendered CSRF token and the comment travel with it. The button
        // is disabled afterwards, never before the browser collects the data.
        window.setTimeout(() => {
            accept.disabled = true;
        }, 0);
    });
})();
