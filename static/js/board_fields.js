/**
 * «+ Поле» on a board's «Поля карточек» (`form[data-board-field-form]`).
 *
 * The form works without this script: «+ Вариант» posts it back with one more
 * option row and writes nothing, and the option rows are always shown with a
 * hint that only a list takes them. This only does the same in place: «+
 * Вариант» clones the row `<template>`, «×» removes a row (never the last
 * one — it is emptied instead), and the rows are hidden while the chosen kind
 * is not «Список». Nothing here is a rule — `create_field()` refuses options
 * for any other kind, and the view drops them before asking.
 *
 * Listeners are delegated from `document`.
 */
(() => {
    'use strict';

    if (window.qualityBoardFields) {
        return;
    }

    const SELECT_KIND = 'SELECT';

    const formOf = (element) => element.closest('form[data-board-field-form]');

    const syncKind = (form) => {
        const kind = form.querySelector('[data-board-field-kind]');
        const options = form.querySelector('[data-board-field-options]');
        if (!kind || !options) {
            return;
        }
        options.hidden = kind.value !== SELECT_KIND;
        // A hidden list posts nothing: its rows are disabled with it.
        options.querySelectorAll('input, select').forEach((control) => {
            control.disabled = options.hidden;
        });
    };

    const syncRows = (form) => {
        const rows = form.querySelectorAll('[data-option-row]');
        rows.forEach((row) => {
            const remove = row.querySelector('[data-remove-option-row]');
            if (remove) {
                remove.hidden = false;
            }
        });
    };

    const addRow = (form) => {
        const template = form.querySelector('template[data-option-row-template]');
        const list = form.querySelector('[data-option-rows]');
        if (!template || !list) {
            return;
        }
        const row = template.content.firstElementChild.cloneNode(true);
        list.appendChild(row);
        syncRows(form);
        const label = row.querySelector('input[name="option_label"]');
        if (label) {
            label.focus();
        }
    };

    const removeRow = (row) => {
        const form = formOf(row);
        const list = row.parentElement;
        if (list && list.querySelectorAll('[data-option-row]').length > 1) {
            row.remove();
        } else {
            row.querySelectorAll('input').forEach((input) => {
                input.value = '';
            });
        }
        if (form) {
            syncRows(form);
        }
    };

    document.addEventListener('click', (event) => {
        const add = event.target.closest('[data-add-option-row]');
        if (add && formOf(add)) {
            event.preventDefault();
            addRow(formOf(add));
            return;
        }
        const remove = event.target.closest('[data-remove-option-row]');
        if (remove) {
            event.preventDefault();
            removeRow(remove.closest('[data-option-row]'));
        }
    });

    document.addEventListener('change', (event) => {
        if (event.target.matches && event.target.matches('[data-board-field-kind]')) {
            const form = formOf(event.target);
            if (form) {
                syncKind(form);
            }
        }
    });

    const init = () => {
        document.querySelectorAll('form[data-board-field-form]').forEach((form) => {
            syncKind(form);
            syncRows(form);
        });
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.qualityBoardFields = { init };
})();
