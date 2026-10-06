/**
 * The checkbox of a card's «Чек-лист» without a reload.
 *
 * Without this script the checkbox is what the server drew: the button of an
 * ordinary POST form (`form[data-checklist-toggle]`) posting the state it
 * asks for (`done` 1 or 0), answered by the card again. With it, the same
 * form is posted through `fetch` with `X-Requested-With: fetch` — the header
 * `boards:checklist_toggle` answers in JSON, as `boards:card_move` does — and
 * the tick changes at once:
 *
 * - `{"ok": true, "is_done", "done", "total"}`: the item keeps the state the
 *   server stored, and the counters of this card — «2/5» over the list and
 *   «☑ 2/5» on its tile — say the server's numbers;
 * - a refusal (`{"ok": false, "error"}`, 400 or 403) or a network error puts
 *   the tick back where it was and shows the server's sentence (or a generic
 *   one) in `[data-board-message]`.
 *
 * Who ticked it and when, the journal and every other open page follow from
 * `board.updated(checklist_changed)`, which the live client
 * (`realtime/boards.js`) turns into a fresh block. Nothing here is a rule:
 * the address, the state asked for and the answer are the server's, and the
 * listener is delegated from `document`, so a block replaced by the live
 * client keeps working.
 */
(() => {
    'use strict';

    if (window.qualityBoardChecklist) {
        return;
    }

    const FAILED = 'Не удалось отметить пункт. Попробуйте ещё раз.';

    const showMessage = (text) => {
        const box = document.querySelector('[data-board-message]');
        if (!box) {
            return;
        }
        box.textContent = text || '';
        box.hidden = !text;
    };

    const fieldValue = (field) => {
        if (!field) {
            return '';
        }
        return typeof field.value === 'string' ? field.value : field.getAttribute('value') || '';
    };

    const setFieldValue = (field, value) => {
        if (field) {
            field.value = value;
            field.setAttribute('value', value);
        }
    };

    /** Draw `item` ticked or not — the class, the button's state, the next request. */
    const draw = (form, isDone) => {
        const item = form.closest('[data-checklist-item]');
        if (item) {
            item.classList.toggle('is-done', isDone);
        }
        const button = form.querySelector('button');
        if (button) {
            button.setAttribute('aria-pressed', isDone ? 'true' : 'false');
        }
        // What the next click asks for: the other state.
        setFieldValue(form.querySelector('[data-checklist-done]'), isDone ? '0' : '1');
    };

    /** «2/5» over the list and «☑ 2/5» on the card's tile, from the server's numbers. */
    const drawCounts = (form, done, total) => {
        if (!Number.isInteger(done) || !Number.isInteger(total)) {
            return;
        }
        const block = form.closest('[data-live-board-checklist]');
        const count = block && block.querySelector('[data-checklist-count]');
        if (count) {
            count.textContent = `${done}/${total}`;
            count.classList.toggle('is-complete', total > 0 && done === total);
        }
        const board = document.querySelector('[data-board]');
        const cardId = board && board.querySelector('[data-current-card]')
            ? board.querySelector('[data-current-card]').dataset.currentCard : '';
        const tileItem = cardId ? document.querySelector(`[data-card-id="${cardId}"]`) : null;
        const tile = tileItem && tileItem.querySelector('[data-tile-checklist]');
        if (tile) {
            tile.textContent = `☑ ${done}/${total}`;
            tile.classList.toggle('board-tile__checklist--complete', total > 0 && done === total);
        }
    };

    const send = (form) => {
        if (form.getAttribute('data-checklist-pending') !== null) {
            return Promise.resolve(false);
        }
        const asked = fieldValue(form.querySelector('[data-checklist-done]'));
        const wasDone = asked !== '1';
        const body = new URLSearchParams();
        body.append('done', asked);
        const token = form.querySelector('input[name="csrfmiddlewaretoken"]');
        form.setAttribute('data-checklist-pending', '');
        draw(form, !wasDone);
        showMessage('');
        return window.fetch(form.getAttribute('action'), {
            method: 'POST',
            body: body.toString(),
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': fieldValue(token),
                'X-Requested-With': 'fetch',
            },
        })
            .then((response) => response.json().catch(() => ({ ok: false })))
            .then((answer) => {
                if (!answer || !answer.ok) {
                    draw(form, wasDone);
                    showMessage((answer && answer.error) || FAILED);
                    return false;
                }
                draw(form, Boolean(answer.is_done));
                drawCounts(form, answer.done, answer.total);
                return true;
            })
            .catch(() => {
                draw(form, wasDone);
                showMessage(FAILED);
                return false;
            })
            .finally(() => {
                form.removeAttribute('data-checklist-pending');
            });
    };

    document.addEventListener('submit', (event) => {
        const form = event.target;
        if (!form || typeof form.matches !== 'function' || !form.matches('form[data-checklist-toggle]')) {
            return;
        }
        if (typeof window.fetch !== 'function') {
            return;
        }
        event.preventDefault();
        send(form);
    });

    window.qualityBoardChecklist = { send };
})();
