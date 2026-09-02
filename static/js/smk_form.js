/**
 * The СМК record form's local behaviour.
 *
 * Two repeatable blocks (несоответствия and мероприятия), each a
 * `[data-block]` section with a `[data-row-list]`, a `<template>` prototype and
 * a `TOTAL_FORMS` counter; a мероприятие additionally holds repeatable
 * assignee rows. This file only clones, removes and renumbers rows and filters
 * the employee selectors by the chosen department.
 *
 * Deliberately the same row mechanics `protocol_editor.js` uses, minus what
 * only a protocol has (participants, speakers, the approval hint and the split
 * option). No business rule lives here: the server re-parses and re-validates
 * everything — required fields, duplicate исполнители and the department an
 * employee really belongs to.
 */
(() => {
    'use strict';

    const form = document.querySelector('[data-smk-form]');
    if (!form) return;

    const blocks = [...form.querySelectorAll('[data-block]')];

    const rowsOf = (block) => [...block.querySelector('[data-row-list]').children];

    /** Renumber one block's field names and refresh its `TOTAL_FORMS`. */
    const renumber = (block) => {
        const name = block.dataset.block;
        const rows = rowsOf(block);
        block.querySelector('[data-total]').value = rows.length;
        const badge = block.querySelector('[data-section-count]');
        if (badge) badge.textContent = rows.length;
        const pattern = new RegExp(`^${name}-\\d+`);
        rows.forEach((row, index) => {
            row.querySelectorAll('[name]').forEach((field) => {
                field.name = field.name.replace(pattern, `${name}-${index}`);
            });
        });
    };

    /**
     * Employee options are filtered by the department chosen next to them.
     *
     * A selection that is already there is never cleared: an employee moved to
     * another department keeps their row, visible and selected, and the row
     * warns instead. Changing it is the author's own explicit action — the
     * same protection `protocol_editor.js` applies for the same reason.
     */
    const syncPair = (pair) => {
        const department = pair.querySelector('[data-department-select]');
        const employee = pair.querySelector('[data-employee-select]');
        if (!department || !employee) return;
        const departmentId = department.value;
        const selected = employee.value;
        // A disabled `<select>` is left out of the POST entirely, so a row that
        // already names someone keeps its field enabled.
        employee.disabled = !departmentId && !selected;
        let mismatched = '';
        [...employee.options].forEach((option) => {
            if (!option.value) return;
            if (option.value === selected) {
                option.hidden = false;
                option.disabled = false;
                if (option.dataset.departmentId !== departmentId) {
                    mismatched = option.textContent.trim();
                }
                return;
            }
            const available = option.dataset.departmentId === departmentId;
            option.hidden = !available;
            option.disabled = !available;
        });
        const warning = pair.querySelector('[data-pair-warning]');
        if (!warning) return;
        warning.hidden = !mismatched;
        warning.textContent = mismatched
            ? (departmentId
                ? `«${mismatched}» больше не относится к выбранному подразделению. `
                    + 'Выбор сохранён — измените подразделение или выберите другого сотрудника.'
                : `Подразделение сотрудника «${mismatched}» недоступно. `
                    + 'Выбор сохранён — укажите подразделение или выберите другого сотрудника.')
            : '';
    };

    const syncAllPairs = () => {
        form.querySelectorAll('[data-employee-pair]').forEach(syncPair);
    };

    form.addEventListener('click', (event) => {
        const target = event.target;
        if (target.matches('[data-add-row]')) {
            const block = target.closest('[data-block]');
            // The add button lives inside the section's `<summary>`, where a
            // click would otherwise toggle the disclosure shut on the row that
            // was just added.
            event.preventDefault();
            if (block.tagName === 'DETAILS') block.open = true;
            const list = block.querySelector('[data-row-list]');
            list.append(block.querySelector('[data-row-template]').content.cloneNode(true));
            renumber(block);
            syncAllPairs();
            return;
        }
        if (target.matches('[data-remove-row]')) {
            const block = target.closest('[data-block]');
            const list = block.querySelector('[data-row-list]');
            // The server requires at least one row in both blocks, so the last
            // one stays and is simply cleared.
            if (rowsOf(block).length > 1) {
                target.closest('[data-row]').remove();
            } else {
                list.querySelectorAll('textarea, input[type="date"]').forEach((field) => {
                    field.value = '';
                });
            }
            renumber(block);
            syncAllPairs();
            return;
        }
        if (target.matches('[data-add-assignee]')) {
            const action = target.closest('[data-row]');
            const template = form.querySelector('[data-assignee-template]');
            action.querySelector('[data-assignee-list]').append(template.content.cloneNode(true));
            renumber(target.closest('[data-block]'));
            syncAllPairs();
            return;
        }
        if (target.matches('[data-remove-assignee]')) {
            const list = target.closest('[data-assignee-list]');
            if (list.querySelectorAll('[data-assignee-row]').length > 1) {
                target.closest('[data-assignee-row]').remove();
            } else {
                list.querySelectorAll('select').forEach((select) => { select.value = ''; });
            }
            syncAllPairs();
        }
    });

    form.addEventListener('change', (event) => {
        const pair = event.target.closest('[data-employee-pair]');
        if (pair) syncPair(pair);
    });

    /**
     * A required field inside a collapsed section cannot be focused, and the
     * browser then refuses to submit without saying why. Opening its section
     * during the `invalid` event puts the field back on screen with the native
     * message on it.
     */
    form.addEventListener(
        'invalid',
        (event) => {
            const section = event.target.closest('details');
            if (section) section.open = true;
        },
        true,
    );

    blocks.forEach(renumber);
    syncAllPairs();
})();
