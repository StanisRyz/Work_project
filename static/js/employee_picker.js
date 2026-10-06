/**
 * «Подразделение | Сотрудник»: the one department → employee picker.
 *
 * Every form that picks people the same way uses this — the protocol editor
 * (участники and исполнители), the СМК form (исполнители), and a board's
 * «Новая доска» and «Участники». A pair is `[data-employee-pair]` holding a
 * `[data-department-select]` and a `[data-employee-select]`; the page renders
 * every active employee once as an `<option data-department-id>`
 * (`accounts.directory.get_employee_directory()`), and the department only
 * filters what is already there. No rule lives here: the server re-checks
 * whoever comes back.
 *
 * `syncPair()` never clears a selection that is already there. An employee
 * moved to another department after the form was saved — or a department since
 * deactivated, whose `<option>` the directory no longer renders — used to make
 * a row redraw itself empty, and the next save silently dropped that person. A
 * stored selection therefore stays visible, enabled and selected however badly
 * it matches, and the row's `[data-pair-warning]` says so instead; changing it
 * is the user's own explicit action. `pair.dataset.excludeUsers` (comma
 * separated ids) hides people the surrounding form says are taken.
 *
 * A repeatable list of pairs is `[data-employee-picker]`: its
 * `[data-employee-picker-list]` holds the rows (`[data-employee-picker-row]`),
 * a `<template data-employee-picker-template>` is the empty row,
 * `[data-employee-picker-add]` adds one and `[data-employee-picker-remove]`
 * takes one away. Every row posts under the same field name, so nothing is
 * renumbered. `data-employee-picker-exclude` names people never offered (the
 * board's owner, its current members); a person chosen in one row is hidden in
 * the others. The add button is a real submit button: without JavaScript it
 * posts the form back for one more row, and every row then offers the full
 * list of employees.
 *
 * Listeners are delegated from `document`, so a row added later or markup
 * replaced by a live refresh needs no binding. `window.qualityEmployeePicker`
 * exposes `syncPair()` for the forms that keep their own exclusion rules.
 */
(() => {
    'use strict';

    if (window.qualityEmployeePicker) {
        return;
    }

    const syncPair = (pair) => {
        if (!pair) return;
        const department = pair.querySelector('[data-department-select]');
        const employee = pair.querySelector('[data-employee-select]');
        if (!department || !employee) return;
        const departmentId = department.value;
        const taken = pair.dataset.excludeUsers ? pair.dataset.excludeUsers.split(',') : [];
        const selected = employee.value;
        // A disabled `<select>` is left out of the POST entirely, so a row
        // that already names someone keeps its field enabled even while the
        // department next to it is blank.
        employee.disabled = !departmentId && !selected;
        let mismatched = '';
        [...employee.options].forEach((option) => {
            if (!option.value) return;
            if (option.value === selected) {
                // The saved choice: never hidden, never disabled, never
                // dropped — only reported when it no longer fits.
                option.hidden = false;
                option.disabled = false;
                if (option.dataset.departmentId !== departmentId) {
                    mismatched = option.textContent.trim();
                }
                return;
            }
            const available = option.dataset.departmentId === departmentId
                && !taken.includes(option.value);
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

    /** One repeatable list: hide whoever is fixed or chosen elsewhere, then sync each row. */
    const syncPicker = (picker) => {
        if (!picker) return;
        const fixed = (picker.dataset.employeePickerExclude || '').split(',').filter(Boolean);
        const rows = [...picker.querySelectorAll('[data-employee-picker-row]')];
        const chosen = rows.map((row) => {
            const select = row.querySelector('[data-employee-select]');
            return select ? select.value : '';
        });
        rows.forEach((row, index) => {
            const others = chosen.filter((value, at) => value && at !== index);
            row.dataset.excludeUsers = [...fixed, ...others].join(',');
            syncPair(row);
        });
    };

    document.addEventListener('change', (event) => {
        const target = event.target;
        if (!target || typeof target.closest !== 'function') return;
        if (!target.matches('[data-department-select], [data-employee-select]')) return;
        const picker = target.closest('[data-employee-picker]');
        if (picker) {
            syncPicker(picker);
        } else {
            syncPair(target.closest('[data-employee-pair]'));
        }
    });

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || typeof target.closest !== 'function') return;
        const add = target.closest('[data-employee-picker-add]');
        if (add) {
            const picker = add.closest('[data-employee-picker]');
            const template = picker && picker.querySelector('[data-employee-picker-template]');
            const list = picker && picker.querySelector('[data-employee-picker-list]');
            if (!template || !list) return;
            // With JavaScript the row is cloned here; the button's own submit
            // is the no-JavaScript path.
            event.preventDefault();
            list.append(template.content.cloneNode(true));
            syncPicker(picker);
            const added = list.lastElementChild;
            const first = added && added.querySelector('select');
            if (first) first.focus();
            return;
        }
        const remove = target.closest('[data-employee-picker-remove]');
        if (remove) {
            const picker = remove.closest('[data-employee-picker]');
            const row = remove.closest('[data-employee-picker-row]');
            if (!picker || !row) return;
            event.preventDefault();
            row.remove();
            syncPicker(picker);
        }
    });

    const syncAll = () => document.querySelectorAll('[data-employee-picker]').forEach(syncPicker);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', syncAll);
    } else {
        syncAll();
    }

    window.qualityEmployeePicker = { syncPair, syncPicker };
})();
