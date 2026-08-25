/**
 * The protocol draft editor's local behaviour.
 *
 * Four repeatable blocks (участники, повестка, «Слушали», задачи), each a
 * `[data-block]` section with a `[data-row-list]`, a `<template>` prototype and
 * a `TOTAL_FORMS` counter. This file only clones, removes and renumbers rows,
 * filters the employee selectors by the chosen department and keeps the speaker
 * selectors in step with the participant rows.
 *
 * It also derives two purely presentational hints. A participant who is also an
 * assignee of a protocol task will have to approve the protocol whether or not
 * «Требует согласования» is ticked; that hint never changes the checkbox — the
 * manual flag is stored on its own, and `collect_required_approvers()` on the
 * server remains the only authority on who must sign. And «Разбить задачу для
 * участников» is offered only on a decision with two or more исполнителя,
 * which is the rule `_apply_actions()` applies again when it stores the flag.
 *
 * No business rule lives here. The server re-parses and re-validates everything
 * — required fields, duplicate participants, the department an employee really
 * belongs to and the speaker being a participant of this protocol.
 */
(() => {
    'use strict';

    /**
     * One editor instance, bound to one `[data-protocol-editor]` form.
     *
     * Written as a repeatable initialiser rather than a one-shot script: a
     * real-time refresh replaces the whole content block, and the new markup has
     * to work again. `qualityFragments.claim()` is what keeps a second call from
     * binding the same form twice, so no logic below had to change.
     */
    const initProtocolEditor = (root) => {
        const scope = root && root.querySelector ? root : document;
        const form = scope.querySelector('[data-protocol-editor]')
            || document.querySelector('[data-protocol-editor]');
        if (!form) return;
        if (window.qualityFragments && !window.qualityFragments.claim(form)) return;

        const authorId = form.dataset.authorId || '';
        const authorName = form.dataset.authorName || '';
        const blocks = [...form.querySelectorAll('[data-block]')];
        const blockByName = (name) => blocks.find((block) => block.dataset.block === name);

        const rowsOf = (block) => [...block.querySelector('[data-row-list]').children];

        /**
         * Keep the section header's count badge in step with its rows.
         *
         * Participants include the author, whose card is fixed and lives
         * outside `[data-row-list]`, so that block counts one more.
         */
        const refreshCount = (block, rowCount) => {
            const badge = block.querySelector('[data-section-count]');
            if (!badge) return;
            badge.textContent = block.dataset.block === 'participants' ? rowCount + 1 : rowCount;
        };

        /** Renumber one block's field names and refresh its `TOTAL_FORMS`. */
        const renumber = (block) => {
            const name = block.dataset.block;
            const rows = rowsOf(block);
            block.querySelector('[data-total]').value = rows.length;
            refreshCount(block, rows.length);
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
         * The one thing this never does is clear a choice that is already
         * there. An employee moved to another department after the draft was
         * saved — or a department that has since been deactivated, whose
         * `<option>` `get_editor_directory()` no longer renders — used to make
         * the row redraw itself empty, and the next save silently dropped that
         * participant or assignee. A stored selection therefore stays visible,
         * enabled and selected however badly it matches, and the row shows a
         * warning instead; changing it is the author's own explicit action.
         *
         * Both blocks that pair a department with a person — участники and the
         * исполнители of a protocol task — are `[data-employee-pair]`, so this
         * protection covers them together.
         */
        const syncPair = (pair) => {
            const department = pair.querySelector('[data-department-select]');
            const employee = pair.querySelector('[data-employee-select]');
            if (!department || !employee) return;
            const departmentId = department.value;
            const taken = pair.dataset.excludeUsers ? pair.dataset.excludeUsers.split(',') : [];
            const selected = employee.value;
            // A disabled `<select>` is left out of the POST entirely, so a row
            // that already names someone keeps its field enabled even while
            // the department next to it is blank.
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

        /**
         * Mark the participants who will be required to approve because a protocol
         * task names them. Read-only: it toggles a hint and nothing else, so a
         * participant's own «Требует согласования» value survives untouched.
         */
        const syncAutoApprovalHints = () => {
            const assigned = new Set(
                [...form.querySelectorAll('[data-assignee-row] [data-employee-select]')]
                    .map((select) => select.value)
                    .filter(Boolean),
            );
            form.querySelectorAll('[data-block="participants"] [data-row]').forEach((row) => {
                const hint = row.querySelector('[data-auto-approval-hint]');
                const user = row.querySelector('[data-participant-user]');
                if (!hint || !user) return;
                // The author never approves their own protocol, even when a task
                // names them, so their row is never hinted.
                hint.hidden = !user.value || user.value === authorId || !assigned.has(user.value);
            });
        };

        /**
         * «Разбить задачу для участников» is offered only where it means
         * something: a decision with at least two named исполнителя. Below
         * that the checkbox is disabled and cleared, because one shared task
         * and one personal task for the same single person are the same thing.
         *
         * Presentation only. The server stores the flag through
         * `_apply_actions()`, which applies exactly this rule again and is the
         * authority; this just keeps the row from offering a choice that would
         * be normalized away on save. The hint under it says which of the two
         * behaviours the current state produces.
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

        const syncSplitOptions = () => {
            const block = blockByName('actions');
            if (!block) return;
            block.querySelectorAll('[data-row]').forEach(syncSplitOption);
        };

        const syncAllPairs = () => {
            form.querySelectorAll('[data-employee-pair]').forEach(syncPair);
            syncParticipants();
            syncAutoApprovalHints();
            syncSplitOptions();
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
                const block = target.closest('[data-block]');
                // The add button lives inside the section's `<summary>`, where a
                // click would otherwise toggle the disclosure shut on the row
                // that was just added.
                event.preventDefault();
                if (block.tagName === 'DETAILS') block.open = true;
                addRow(block);
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
            if (target.matches('[data-split-checkbox]')) {
                // Only the hint under it changes; nothing else in the editor
                // depends on how a decision will be executed.
                syncSplitOption(target.closest('[data-row]'));
                return;
            }
            if (target.matches('[data-participant-user]') || target.closest('[data-block]')?.dataset.block === 'participants') {
                syncParticipants();
            } else if (target.matches('[data-department-select]') || target.matches('[data-employee-select]')) {
                syncPair(target.closest('[data-employee-pair]'));
                // Naming or clearing an исполнитель can cross the two-assignee
                // threshold the option is offered above.
                syncSplitOptions();
            } else {
                return;
            }
            // Both branches can change who a protocol task names, so the derived
            // approval hint is refreshed from one place.
            syncAutoApprovalHints();
        });

        /**
         * A required field inside a collapsed section cannot be focused, and
         * the browser then refuses to submit without saying why. Opening its
         * section during the `invalid` event — before validity is reported —
         * puts the field back on screen with the native message on it.
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
    };

    // Registered before the first run, so a live replacement of the content
    // block re-wires the new editor through the very same initialiser.
    if (window.qualityFragments) {
        window.qualityFragments.register('protocolEditor', initProtocolEditor);
    }
    initProtocolEditor(document);
})();
