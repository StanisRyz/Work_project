/**
 * Dragging cards on a board — mouse, finger and pen, through Pointer Events.
 *
 * The browser decides nothing. It shows the move at once and asks the server;
 * `move_card()` / `complete_card()` decide, and a refusal puts the card back
 * where it was with the server's own words in `[data-board-message]`. What
 * may be dragged, and where to, is read only from the markup the server drew:
 *
 *   [data-card-movable]         a tile this user may drag (its <li>)
 *   data-card-move-url          where a drop in a working column is posted
 *   [data-card-complete-trigger]  the tile's hidden «Завершить» trigger of the
 *                               shared confirmation modal — present only when
 *                               the user may complete the task
 *   [data-column-move]          a working column that takes a moved card
 *   [data-column-complete]      «Готово»: a drop there only means «complete»
 *   [data-column-list]          the column's list, [data-column-count] its count
 *   data-current-card           the card the panel is showing
 *   [data-board] data-board-url the board's own address, for the reload after
 *                               the open card moved
 *
 * A drop in a working column posts `stage` and `before_card_id` (the next
 * movable card in the list, or empty for the end) with `X-Requested-With:
 * fetch`, and reads `{"ok", "stage", "counts"}` or `{"ok": false, "error"}`.
 * A drop on «Готово» clicks the tile's trigger, exactly as
 * `attachment_upload.js` clicks its hidden one: the modal asks for the result
 * and posts it as an ordinary form; closing the modal puts the card back.
 *
 * After moving the card the panel shows, see `openCardMoved()`.
 *
 * Every listener is delegated from `document`, so columns replaced wholesale by
 * a later live update keep working with nothing to re-bind. Without
 * JavaScript, «Переместить в…» and «Завершить» in the card panel do the same.
 *
 * While a card is in the air, a move awaits the server or the «Готово» modal
 * is open, `[data-board]` carries `data-board-busy`: the live client
 * (`realtime/boards.js`) must not replace the columns under a gesture, and
 * waits for `quality:board-idle`, dispatched on `document` once the attribute
 * goes.
 */
(() => {
    'use strict';

    if (window.qualityBoardDnd) {
        return;
    }

    const DRAG_THRESHOLD = 5;      // px a mouse must travel before a click becomes a drag
    const TOUCH_SLOP = 10;         // px a finger may wander during the long press
    const LONG_PRESS_MS = 300;     // a finger has to rest this long to pick a card up
    const EDGE = 48;               // px from an edge where scrolling starts
    const SCROLL_STEP = 14;        // px per frame of auto-scroll

    const FAILED = 'Не удалось переместить карточку. Попробуйте ещё раз.';
    const MOVED_PANEL_STALE = 'Карточка перемещена — обновите страницу, чтобы панель показала новую колонку.';

    let pending = null;   // a press that may still become a drag
    let drag = null;      // the drag in progress
    let suppressClick = false;
    let awaitingCompletion = null;
    let movesInFlight = 0;

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    const csrfToken = () => {
        const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
        return match ? decodeURIComponent(match[1]) : '';
    };

    // A gesture or a request in progress: the columns are this script's until
    // it is over. Recomputed after every change of any of the three.
    const syncBusy = () => {
        const root = document.querySelector('[data-board]');
        if (!root) {
            return;
        }
        const busy = Boolean(drag || movesInFlight > 0 || awaitingCompletion);
        if (busy === root.hasAttribute('data-board-busy')) {
            return;
        }
        if (busy) {
            root.setAttribute('data-board-busy', '');
        } else {
            root.removeAttribute('data-board-busy');
            document.dispatchEvent(new CustomEvent('quality:board-idle'));
        }
    };

    const showMessage = (text) => {
        const box = document.querySelector('[data-board-message]');
        if (!box) {
            return;
        }
        box.textContent = text || '';
        box.hidden = !text;
    };

    const nextMovable = (element, skip) => {
        let node = element ? element.nextElementSibling : null;
        while (node && (node === skip || !node.matches('[data-card-movable]'))) {
            node = node.nextElementSibling;
        }
        return node;
    };

    const nextSiblingSkipping = (element, skip) => {
        let node = element.nextElementSibling;
        while (node && node === skip) {
            node = node.nextElementSibling;
        }
        return node;
    };

    const updateCounts = (counts) => {
        Object.keys(counts || {}).forEach((code) => {
            const column = document.querySelector(`[data-column="${code}"]`);
            const badge = column ? column.querySelector('[data-column-count]') : null;
            if (badge) {
                badge.textContent = String(counts[code]);
            }
        });
    };

    const restore = (state) => {
        // Back to exactly where it was picked up.
        const { list, next } = state.origin;
        list.insertBefore(state.item, next && next.parentElement === list ? next : null);
        state.item.classList.remove('board-column__item--pending', 'board-column__item--dragging');
    };

    // ------------------------------------------------------------------
    // Where the pointer is
    // ------------------------------------------------------------------

    const targetAt = (x, y) => {
        const element = document.elementFromPoint(x, y);
        const column = element && element.closest ? element.closest('[data-column]') : null;
        if (!column) {
            return null;
        }
        if (column.hasAttribute('data-column-move')) {
            return { column, mode: 'move' };
        }
        if (column.hasAttribute('data-column-complete') && drag.item.querySelector('[data-card-complete-trigger]')) {
            return { column, mode: 'complete' };
        }
        return null;
    };

    const placePlaceholder = (target, y) => {
        const list = target.column.querySelector('[data-column-list]');
        if (!list) {
            return;
        }
        if (target.mode === 'complete') {
            // «Готово» lists the newest completion first.
            list.insertBefore(drag.placeholder, list.firstElementChild);
            return;
        }
        const items = Array.from(list.querySelectorAll(':scope > [data-card-movable]'))
            .filter((node) => node !== drag.item);
        const before = items.find((node) => {
            const rect = node.getBoundingClientRect();
            return y < rect.top + rect.height / 2;
        });
        if (before) {
            list.insertBefore(drag.placeholder, before);
        } else {
            const empty = list.querySelector(':scope > .board-column__empty');
            list.insertBefore(drag.placeholder, empty || null);
        }
    };

    const highlight = (column) => {
        if (drag.highlighted === column) {
            return;
        }
        if (drag.highlighted) {
            drag.highlighted.classList.remove('board-column--drop');
        }
        drag.highlighted = column;
        if (column) {
            column.classList.add('board-column--drop');
        }
    };

    // ------------------------------------------------------------------
    // Auto-scroll near the edges of a column and of the row of columns
    // ------------------------------------------------------------------

    const autoScroll = () => {
        if (!drag) {
            return;
        }
        const { x, y } = drag.pointer;
        const row = document.querySelector('[data-board-columns]');
        if (row) {
            const rect = row.getBoundingClientRect();
            if (x < rect.left + EDGE) {
                row.scrollLeft -= SCROLL_STEP;
            } else if (x > rect.right - EDGE) {
                row.scrollLeft += SCROLL_STEP;
            }
        }
        const element = document.elementFromPoint(x, y);
        const list = element && element.closest
            ? element.closest('[data-column]')?.querySelector('[data-column-list]')
            : null;
        if (list) {
            const rect = list.getBoundingClientRect();
            if (y < rect.top + EDGE) {
                list.scrollTop -= SCROLL_STEP;
            } else if (y > rect.bottom - EDGE) {
                list.scrollTop += SCROLL_STEP;
            }
        }
        drag.frame = window.requestAnimationFrame(autoScroll);
    };

    // ------------------------------------------------------------------
    // Starting, moving, finishing
    // ------------------------------------------------------------------

    const start = () => {
        const { item, pointerId, x, y } = pending;
        const rect = item.getBoundingClientRect();
        const placeholder = document.createElement('li');
        placeholder.className = 'board-placeholder';
        placeholder.style.height = `${rect.height}px`;
        placeholder.setAttribute('aria-hidden', 'true');

        const ghost = item.cloneNode(true);
        ghost.removeAttribute('data-card-movable');
        ghost.removeAttribute('data-card-id');
        ghost.querySelectorAll('[data-confirm]').forEach((node) => node.remove());
        ghost.classList.add('board-ghost');
        ghost.style.width = `${rect.width}px`;
        ghost.setAttribute('aria-hidden', 'true');
        document.body.appendChild(ghost);

        drag = {
            item,
            ghost,
            placeholder,
            pointerId,
            offsetX: x - rect.left,
            offsetY: y - rect.top,
            origin: { list: item.parentElement, next: item.nextElementSibling },
            target: null,
            highlighted: null,
            pointer: { x, y },
            frame: null,
        };
        pending = null;
        item.parentElement.insertBefore(placeholder, item);
        item.classList.add('board-column__item--dragging');
        document.documentElement.classList.add('board-is-dragging');
        showMessage('');
        try {
            item.setPointerCapture(pointerId);
        } catch (error) {
            // Capture is a convenience; the document listeners still follow.
        }
        moveGhost(x, y);
        drag.frame = window.requestAnimationFrame(autoScroll);
        syncBusy();
    };

    const moveGhost = (x, y) => {
        drag.pointer = { x, y };
        drag.ghost.style.transform = `translate(${x - drag.offsetX}px, ${y - drag.offsetY}px)`;
        const target = targetAt(x, y);
        drag.target = target;
        highlight(target ? target.column : null);
        if (target) {
            placePlaceholder(target, y);
        } else {
            // Nowhere to drop: the gap goes back to where the card came from.
            drag.origin.list.insertBefore(drag.placeholder, drag.item);
        }
    };

    const finish = (commit) => {
        const state = drag;
        drag = null;
        window.cancelAnimationFrame(state.frame);
        state.ghost.remove();
        if (state.highlighted) {
            state.highlighted.classList.remove('board-column--drop');
        }
        document.documentElement.classList.remove('board-is-dragging');
        try {
            state.item.releasePointerCapture(state.pointerId);
        } catch (error) {
            // Already released.
        }
        // Whatever happens next, the click that ends this gesture is not a
        // click on the link.
        suppressClick = true;
        window.setTimeout(() => {
            suppressClick = false;
        }, 0);

        const { placeholder, item, target } = state;
        const samePlace = placeholder.parentElement === state.origin.list
            && nextSiblingSkipping(placeholder, item) === state.origin.next;
        if (!commit || !target || samePlace) {
            placeholder.remove();
            item.classList.remove('board-column__item--dragging');
            syncBusy();
            return;
        }
        placeholder.parentElement.insertBefore(item, placeholder);
        placeholder.remove();
        item.classList.remove('board-column__item--dragging');
        if (target.mode === 'complete') {
            complete(state);
        } else {
            move(state, target.column.dataset.column);
        }
        syncBusy();
    };

    // ------------------------------------------------------------------
    // Talking to the server
    // ------------------------------------------------------------------

    const move = (state, stage) => {
        const { item } = state;
        const next = nextMovable(item, item);
        const body = new FormData();
        body.append('stage', stage);
        body.append('before_card_id', next ? next.dataset.cardId : '');
        item.classList.add('board-column__item--pending');
        movesInFlight += 1;
        fetch(item.dataset.cardMoveUrl, {
            method: 'POST',
            body,
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'fetch' },
        })
            .then((response) => response.json().catch(() => ({ ok: false })))
            .then((answer) => {
                if (!answer || !answer.ok) {
                    restore(state);
                    showMessage((answer && answer.error) || FAILED);
                    return;
                }
                item.classList.remove('board-column__item--pending');
                updateCounts(answer.counts);
                const row = document.querySelector('[data-board-columns]');
                if (row && row.dataset.currentCard === item.dataset.cardId) {
                    openCardMoved(item.dataset.cardId);
                }
            })
            .catch(() => {
                restore(state);
                showMessage(FAILED);
            })
            .finally(() => {
                movesInFlight -= 1;
                syncBusy();
            });
    };

    /**
     * The card the panel shows was just moved: its panel now names the wrong
     * column. With the live client running (`realtime/boards.js`) nothing more
     * is done — the move's own `board.updated` redraws a clean panel, or
     * raises the conflict banner over unsaved input. Without it, the board's
     * own address for this card is loaded (`data-board-page-url`, filter
     * included — never `reload()`: a board drawn in answer to a refused POST
     * would post it again), unless the panel holds unsaved input, when a
     * message asks the user to reload when ready. Returns what it did.
     */
    const openCardMoved = (cardId) => {
        const core = window.QualityRealtime;
        if (core && core.boardLive && core.boardLive.isActive) {
            return 'live';
        }
        const guard = window.qualityUnsavedGuard;
        if (guard && guard.isDirty) {
            showMessage(MOVED_PANEL_STALE);
            return 'message';
        }
        const root = document.querySelector('[data-board]');
        if (!root || !root.dataset.boardUrl) {
            return 'none';
        }
        window.location.replace(
            root.dataset.boardPageUrl
            || `${root.dataset.boardUrl}?card=${encodeURIComponent(cardId)}`,
        );
        return 'replace';
    };

    // The one entry point a test (and nothing else) calls from outside.
    window.qualityBoardDnd = { openCardMoved };

    const complete = (state) => {
        const trigger = state.item.querySelector('[data-card-complete-trigger]');
        const dialog = document.querySelector('[data-confirm-modal]');
        if (!trigger || !dialog || typeof dialog.showModal !== 'function') {
            restore(state);
            return;
        }
        awaitingCompletion = state;
        trigger.click();
        if (!dialog.open) {
            // The modal did not open: nothing will complete the card.
            awaitingCompletion = null;
            restore(state);
        }
    };

    // Only a cancel, Escape or a click on «Отмена» closes the dialog: on
    // «Завершить» the modal posts its form and the page navigates away.
    document.addEventListener('close', (event) => {
        if (!awaitingCompletion || !event.target.matches || !event.target.matches('[data-confirm-modal]')) {
            return;
        }
        restore(awaitingCompletion);
        awaitingCompletion = null;
        syncBusy();
    }, true);

    // ------------------------------------------------------------------
    // Pointer events, all delegated
    // ------------------------------------------------------------------

    document.addEventListener('pointerdown', (event) => {
        if (drag || pending || !event.isPrimary) {
            return;
        }
        if (event.pointerType === 'mouse' && event.button !== 0) {
            return;
        }
        const item = event.target.closest ? event.target.closest('[data-card-movable]') : null;
        if (!item || !item.dataset.cardMoveUrl) {
            return;
        }
        pending = {
            item,
            pointerId: event.pointerId,
            pointerType: event.pointerType,
            startX: event.clientX,
            startY: event.clientY,
            x: event.clientX,
            y: event.clientY,
            timer: null,
        };
        if (event.pointerType === 'touch') {
            pending.timer = window.setTimeout(() => {
                if (pending) {
                    start();
                }
            }, LONG_PRESS_MS);
        }
    });

    document.addEventListener('pointermove', (event) => {
        if (drag && event.pointerId === drag.pointerId) {
            event.preventDefault();
            moveGhost(event.clientX, event.clientY);
            return;
        }
        if (!pending || event.pointerId !== pending.pointerId) {
            return;
        }
        pending.x = event.clientX;
        pending.y = event.clientY;
        const distance = Math.hypot(event.clientX - pending.startX, event.clientY - pending.startY);
        if (pending.pointerType === 'touch') {
            // Moving before the long press is over is a scroll, not a drag.
            if (distance > TOUCH_SLOP) {
                window.clearTimeout(pending.timer);
                pending = null;
            }
            return;
        }
        if (distance > DRAG_THRESHOLD) {
            start();
            moveGhost(event.clientX, event.clientY);
        }
    });

    const endPointer = (event, commit) => {
        if (drag && event.pointerId === drag.pointerId) {
            if (commit) {
                moveGhost(event.clientX, event.clientY);
            }
            finish(commit);
            return;
        }
        if (pending && event.pointerId === pending.pointerId) {
            window.clearTimeout(pending.timer);
            pending = null;
        }
    };

    document.addEventListener('pointerup', (event) => endPointer(event, true));
    document.addEventListener('pointercancel', (event) => endPointer(event, false));

    // Once a finger has picked a card up, the page must not scroll under it.
    document.addEventListener('touchmove', (event) => {
        if (drag) {
            event.preventDefault();
        }
    }, { passive: false });

    // A long press on a link would otherwise open the browser's own menu.
    document.addEventListener('contextmenu', (event) => {
        if (drag || (pending && pending.pointerType === 'touch')) {
            event.preventDefault();
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && drag) {
            event.preventDefault();
            finish(false);
        }
    });

    // The click that ends a drag lands on the tile's link; it must not open it.
    // Only the browser's own click: the script's click on the hidden
    // «Завершить» trigger must reach the confirmation modal.
    document.addEventListener('click', (event) => {
        if (suppressClick && event.isTrusted) {
            event.preventDefault();
            event.stopPropagation();
            suppressClick = false;
        }
    }, true);
})();
