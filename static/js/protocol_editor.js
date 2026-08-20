/**
 * The protocol draft editor's local behaviour.
 *
 * Four repeatable blocks (участники, повестка, «Слушали», задачи), each a
 * `[data-block]` section with a `[data-row-list]`, a `<template>` prototype and
 * a `TOTAL_FORMS` counter. This file only clones, removes and renumbers rows,
 * filters the employee selectors by the chosen department and keeps the speaker
 * selectors in step with the participant rows.
 *
 * No business rule lives here. The server re-parses and re-validates everything
 * — required fields, duplicate participants, the department an employee really
 * belongs to and the speaker being a participant of this protocol.
 */
(() => {
    'use strict';

    const form = document.querySelector('[data-protocol-editor]');
    if (!form) return;

    const authorId = form.dataset.authorId || '';
    const authorName = form.dataset.authorName || '';
    const blocks = [...form.querySelectorAll('[data-block]')];
    const blockByName = (name) => blocks.find((block) => block.dataset.block === name);

    const rowsOf = (block) => [...block.querySelector('[data-row-list]').children];

    /** Renumber one block's field names and refresh its `TOTAL_FORMS`. */
    const renumber = (block) => {
        const name = block.dataset.block;
        const rows = rowsOf(block);
        block.querySelector('[data-total]').value = rows.length;
        const pattern = new RegExp(`^${name}-\\d+`);
        rows.forEach((row, index) => {
            row.querySelectorAll('[name]').forEach((field) => {
                field.name = field.name.replace(pattern, `${name}-${index}`);
            });
        });
    };

    /** Employee options are filtered by the department chosen next to them. */
    const syncPair = (pair) => {
        const department = pair.querySelector('[data-department-select]');
        const employee = pair.querySelector('[data-employee-select]');
        if (!department || !employee) return;
        const departmentId = department.value;
        const taken = pair.dataset.excludeUsers ? pair.dataset.excludeUsers.split(',') : [];
        employee.disabled = !departmentId;
        [...employee.options].forEach((option) => {
            if (!option.value) return;
            const available = option.dataset.departmentId === departmentId
                && !(option.value !== employee.value && taken.includes(option.value));
            option.hidden = !available;
            option.disabled = !available;
            if (!available && option.selected) employee.value = '';
        });
    };

    /**
     * The same person may not be added to the protocol twice, so every
     * participant selector hides the author and whoever the other rows chose.
     */
    const syncParticipants = () => {
        const block = blockByName('participants');
        if (!block) return;
        const selects = [...block.querySelectorAll('[data-participant-user]')];
        const chosen = selects.map((select) => select.value).filter(Boolean);
        selects.forEach((select) => {
            const pair = select.closest('[data-employee-pair]');
            pair.dataset.excludeUsers = [authorId, ...chosen].filter(Boolean).join(',');
            syncPair(pair);
        });
        syncSpeakers();
    };

    /** The speaker list is exactly «автор + текущие участники». */
    const syncSpeakers = () => {
        const block = blockByName('participants');
        const options = [{ value: authorId, label: authorName }];
        if (block) {
            block.querySelectorAll('[data-participant-user]').forEach((select) => {
                if (!select.value) return;
                const option = select.options[select.selectedIndex];
                options.push({ value: select.value, label: option ? option.textContent : select.value });
            });
        }
        form.querySelectorAll('[data-speaker-select]').forEach((select) => {
            const current = select.value;
            select.textContent = '';
            const placeholder = new Option('Выберите выступающего', '');
            select.append(placeholder);
            options.forEach((item) => select.append(new Option(item.label, item.value)));
            select.value = options.some((item) => item.value === current) ? current : '';
        });
    };

    const syncAllPairs = () => {
        form.querySelectorAll('[data-employee-pair]').forEach(syncPair);
        syncParticipants();
    };

    const addRow = (block) => {
        const list = block.querySelector('[data-row-list]');
        const template = block.querySelector('[data-row-template]');
        list.append(template.content.cloneNode(true));
        renumber(block);
        syncAllPairs();
    };

    form.addEventListener('click', (event) => {
        const target = event.target;
        if (target.matches('[data-add-row]')) {
            addRow(target.closest('[data-block]'));
            return;
        }
        if (target.matches('[data-remove-row]')) {
            const block = target.closest('[data-block]');
            target.closest('[data-row]').remove();
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
            // The server requires at least one assignee on a task that exists,
            // so the last row stays and is simply cleared.
            if (list.querySelectorAll('[data-assignee-row]').length > 1) {
                target.closest('[data-assignee-row]').remove();
            } else {
                list.querySelectorAll('select').forEach((select) => { select.value = ''; });
            }
            syncAllPairs();
        }
    });

    form.addEventListener('change', (event) => {
        const target = event.target;
        if (target.matches('[data-participant-user]') || target.closest('[data-block]')?.dataset.block === 'participants') {
            syncParticipants();
            return;
        }
        if (target.matches('[data-department-select]') || target.matches('[data-employee-select]')) {
            syncPair(target.closest('[data-employee-pair]'));
        }
    });

    blocks.forEach(renumber);
    syncAllPairs();
})();
