/**
 * The СМК record form's local behaviour.
 *
 * Two repeatable blocks (несоответствия and мероприятия), each a
 * `[data-block]` section with a `[data-row-list]`, a `<template>` prototype and
 * a `TOTAL_FORMS` counter; a мероприятие additionally holds repeatable
 * assignee rows. This file only clones, removes and renumbers rows, filters the
 * employee selectors by the chosen department, and keeps each measure's
 * «Связано с несоответствием» list in step with the findings above it.
 *
 * Deliberately the same row mechanics `protocol_editor.js` uses, minus what
 * only a protocol has (participants, speakers and the approval hint). The
 * «Разбить задачу по исполнителям» option is kept in step exactly as the
 * protocol editor keeps its own. It additionally holds back the submit and
 * shows the confirmation dialog, filled from the form as it stands.
 *
 * No business rule lives here, and the dialog is not the guarantee: the server
 * re-parses and re-validates everything — required fields, duplicate
 * исполнители, the department an employee really belongs to — and writes
 * nothing at all until a POST arrives carrying the confirmation flag. This
 * only spares the user a round trip.
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

    /**
     * Rebuild every «Связано с несоответствием» selector from the findings
     * currently on screen.
     *
     * The value is a finding's *row index*, because the findings do not exist
     * in the database yet; the server maps that index onto the rows it kept,
     * so a link can never survive the row it pointed at being emptied. A row
     * with no text is not offered — there would be nothing to point at.
     *
     * A selection already made is preserved whenever its row is still offered,
     * including the one the server rendered into `data-selected` after a failed
     * or unconfirmed submission.
     */
    const syncNonConformityOptions = () => {
        const options = [];
        form.querySelectorAll(
            '[data-block="nonconformities"] [data-row] textarea[name$="-text"]',
        ).forEach((field, index) => {
            const text = field.value.trim();
            if (!text) return;
            const short = text.length > 60 ? `${text.slice(0, 60)}…` : text;
            options.push({ value: String(index), label: `№${index + 1} — ${short}` });
        });
        form.querySelectorAll('[data-non-conformity-select]').forEach((select) => {
            // `data-selected` is the server's answer and is only consulted
            // while the field has not been rebuilt yet; afterwards the live
            // value wins, so a user's own choice is never overwritten.
            const current = select.value || select.dataset.selected || '';
            select.textContent = '';
            select.append(new Option('Не указано', ''));
            options.forEach((item) => select.append(new Option(item.label, item.value)));
            select.value = options.some((item) => item.value === current) ? current : '';
        });
    };

    /**
     * «Разбить задачу по исполнителям» is only offered from two исполнителя up.
     *
     * Presentation only: `SmkSourceForm` normalizes the flag off for a single
     * исполнитель, and the server is the authority. This just keeps the row
     * from offering a choice that would be normalized away — which would also
     * make an unchanged мероприятие look changed to `update_smk_source()`.
     */
    const syncSplitOption = (action) => {
        const toggle = action.querySelector('[data-split-checkbox]');
        if (!toggle) return;
        const named = [...action.querySelectorAll('[data-assignee-row] [data-employee-select]')]
            .filter((select) => select.value).length;
        const offered = named > 1;
        toggle.disabled = !offered;
        if (!offered) toggle.checked = false;
        const off = action.querySelector('[data-split-hint-off]');
        const on = action.querySelector('[data-split-hint-on]');
        if (off) off.hidden = toggle.checked;
        if (on) on.hidden = !toggle.checked;
    };

    const syncAllPairs = () => {
        form.querySelectorAll('[data-employee-pair]').forEach(syncPair);
        form.querySelectorAll('[data-block="actions"] [data-row]').forEach(syncSplitOption);
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
            syncNonConformityOptions();
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
                // Cleared, not removed — and the hidden identity goes with the
                // content: what is left is a new empty row, not the measure
                // that used to be there.
                list.querySelectorAll(
                    'textarea, input[type="date"], input[type="hidden"]',
                ).forEach((field) => { field.value = ''; });
                list.querySelectorAll('input[type="checkbox"]').forEach((field) => {
                    field.checked = false;
                });
            }
            renumber(block);
            syncAllPairs();
            syncNonConformityOptions();
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
        if (event.target.matches('[data-split-checkbox]')) {
            // Only the hint under it changes; nothing else in the editor
            // depends on how a measure will be executed.
            syncSplitOption(event.target.closest('[data-row]'));
            return;
        }
        const pair = event.target.closest('[data-employee-pair]');
        if (pair) syncPair(pair);
    });

    // Typing a finding changes what the measures below may point at. `input`
    // rather than `change`, so the list follows the text as it is written.
    form.addEventListener('input', (event) => {
        if (event.target.matches('[data-block="nonconformities"] textarea')) {
            syncNonConformityOptions();
        }
    });

    // ------------------------------------------------------------ confirmation

    const dialog = form.querySelector('[data-smk-confirm]');

    /**
     * Fill the dialog from the form as it stands right now.
     *
     * Counts a row only if it carries text, and an исполнитель only if one is
     * named — the same rows the server would keep — so the summary cannot
     * promise more than would actually be created. Names are read from the
     * selected `<option>`, deduplicated in the order they appear.
     */
    const fillSummary = () => {
        // Scoped by block, so «несоответствия» and «мероприятия» are counted
        // apart even though both name their text field `…-text`.
        const filled = (block) => [...form.querySelectorAll(
            `[data-block="${block}"] [data-row] textarea[name$="-text"]`,
        )].filter((field) => field.value.trim()).length;

        const origin = form.querySelector('[name="origin"]');
        const originLabel = origin && origin.selectedIndex > 0
            ? origin.options[origin.selectedIndex].textContent.trim()
            : '—';

        const assignees = [];
        form.querySelectorAll('[data-assignee-row] [data-employee-select]').forEach((select) => {
            if (!select.value) return;
            const label = select.options[select.selectedIndex].textContent.trim();
            if (label && !assignees.includes(label)) assignees.push(label);
        });

        const set = (selector, text) => {
            const node = dialog.querySelector(selector);
            if (node) node.textContent = text;
        };
        const auditDate = form.querySelector('[name="audit_date"]');
        set('[data-smk-confirm-origin]', originLabel);
        set('[data-smk-confirm-date]', auditDate && auditDate.value
            ? auditDate.value.split('-').reverse().join('.')
            : '—');
        set('[data-smk-confirm-nonconformities]', String(filled('nonconformities')));
        set('[data-smk-confirm-actions]', String(filled('actions')));
        set('[data-smk-confirm-assignees]', assignees.length ? assignees.join(', ') : '—');
    };

    if (dialog && typeof dialog.showModal === 'function') {
        // The server renders the dialog with a plain `open` attribute so the
        // step works without JavaScript; re-open it modally so both paths look
        // and behave the same once scripting is available.
        if (dialog.open) {
            dialog.close();
            dialog.showModal();
        }

        form.addEventListener('submit', (event) => {
            // The dialog's own «Создать» is a submit button carrying the
            // confirmation flag as its value — letting it through is exactly
            // how the confirmed POST is made.
            if (event.submitter && event.submitter.hasAttribute('data-smk-confirm-accept')) {
                return;
            }
            event.preventDefault();
            // A dialog makes the page behind it inert, so the browser must
            // point at a missing required field first.
            if (typeof form.reportValidity === 'function' && !form.reportValidity()) return;
            fillSummary();
            dialog.showModal();
        });

        dialog.querySelector('[data-smk-confirm-cancel]').addEventListener('click', () => {
            dialog.close();
        });
    }

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
    syncNonConformityOptions();
})();
