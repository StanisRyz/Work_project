/**
 * The open board page — one sub-board — kept current: its tabs, its columns,
 * its card panel and the open card's messages.
 *
 * Four live blocks, all fetched from `boards:fragment`, which renders the very
 * same partials from the very same context builder as the page — no markup is
 * built here, and nothing here is a rule:
 *
 *   [data-live-board-tabs]     the sub-board tabs: read-only, replaced
 *                              whenever its fingerprint moved — but not while
 *                              one of its menus is open (deferred like the
 *                              columns below);
 *   [data-live-board-columns]  the columns — read-only;
 *                              replaced wholesale whenever its fingerprint
 *                              moved (a card, or a tab or a column created,
 *                              renamed, moved or deleted) — but never under a
 *                              gesture: while `[data-board]` carries
 *                              `data-board-busy` (`board_dnd.js`: a card in
 *                              the air, a move awaiting the server, the
 *                              completion modal open) the refresh waits, and
 *                              runs once on `quality:board-idle`. A tab or
 *                              column menu left open (`details[data-board-menu]`,
 *                              perhaps with a new name half typed) holds it
 *                              back the same way, until the menu closes.
 *   [data-live-board-comments] the messages of the open card's «Обсуждение»:
 *                              read-only, replaced whenever its fingerprint
 *                              moved, keeping a reader at the bottom there.
 *                              Its form is outside every block, and no message
 *                              is part of the panel's fingerprint — a message
 *                              never raises the banner over a «Выполнение».
 *   [data-live-board-panel]    holds forms, so it is guarded exactly like the
 *                              act and protocol work blocks: an unchanged
 *                              fingerprint does nothing, a changed one replaces
 *                              a clean panel, and a panel with unsaved input
 *                              keeps every typed character and raises the
 *                              conflict banner instead.
 *
 * What makes it refetch: `board.updated` for this board, a `task.*` event for
 * a task whose tile or panel is on the page, and the `boards` sync revision
 * moving (a reconnect, a recovery sync, a task closed from its own page).
 *
 * Without real-time (`QualityRealtime` is null) none of this runs and the
 * board is exactly the page the server drew; dragging does not depend on it.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('boards')) {
        return;
    }

    const root = document.querySelector('[data-board]');
    if (!root) {
        return;
    }
    const boardId = Number(root.dataset.boardId);
    const fragmentUrl = root.dataset.boardFragmentUrl;
    if (!core.isPositiveInteger(boardId) || !fragmentUrl) {
        return;
    }

    const tabsElement = root.querySelector('[data-live-board-tabs]');
    const columnsElement = root.querySelector('[data-live-board-columns]');
    const panelElement = root.querySelector('[data-live-board-panel]');
    const commentsElement = root.querySelector('[data-live-board-comments]');
    const conflictBanner = document.querySelector('[data-board-conflict-banner]');
    const reloadButton = document.querySelector('[data-board-conflict-reload]');
    if (reloadButton) {
        // The board's own address for this panel, never `reload()`: a page
        // drawn in answer to a refused POST would post it again.
        reloadButton.addEventListener('click', () =>
            window.location.replace(root.dataset.boardPageUrl || root.dataset.boardUrl || window.location.pathname),
        );
    }

    let tabsRevision = root.dataset.tabsRevision || '';
    let columnsRevision = root.dataset.columnsRevision || '';
    let panelRevision = root.dataset.panelRevision || '';
    let commentsRevision = root.dataset.commentsRevision || '';
    // A page re-rendered from a refused form, or holding a «Выполнение» draft
    // parked by an attachment request, already shows input that is not stored.
    let dirty = root.dataset.panelHoldsInput === 'true';
    let deferred = false;

    // -- dirty-state tracking ---------------------------------------------
    //
    // Only a real gesture inside the panel counts: a drag, the confirmation
    // modal or the bug-report dialog type nothing a refresh could discard, and
    // a programmatic replacement dispatches nothing.
    const insidePanel = (target) =>
        Boolean(panelElement && target && typeof panelElement.contains === 'function' && panelElement.contains(target));
    ['input', 'change'].forEach((type) =>
        document.addEventListener(type, (event) => {
            if (event.isTrusted === false) {
                return;
            }
            if (insidePanel(event.target)) {
                dirty = true;
            }
        }),
    );
    // A draft restored from this browser is unsaved input as much as typing.
    document.addEventListener('quality:form-restored', (event) => {
        if (insidePanel(event.target)) {
            dirty = true;
        }
    });

    const isBusy = () => root.getAttribute('data-board-busy') !== null;
    const menuOpenIn = (element) =>
        Boolean(element && element.querySelector('details[data-board-menu][open]'));
    const menuOpen = () => menuOpenIn(tabsElement) || menuOpenIn(columnsElement);

    const revisionOf = (value) => (typeof value === 'string' ? value : '');

    const applyColumns = (payload) => {
        if (!columnsElement || typeof payload.columns_html !== 'string') {
            return;
        }
        const revision = revisionOf(payload.columns_revision);
        if (revision && revision === columnsRevision) {
            return;
        }
        if (isBusy() || menuOpen()) {
            // A card is in the air or on its way to the server, or a menu of
            // the structure is open: the markup it was picked from must stay.
            // Fetched again — not applied stale — once the gesture is over.
            deferred = true;
            return;
        }
        // Each column scrolls on its own, and the row of columns sideways; a
        // replacement keeps where they were.
        const scrolled = {};
        columnsElement.querySelectorAll('[data-column-id]').forEach((column) => {
            const list = column.querySelector('[data-column-list]');
            if (list) {
                scrolled[column.dataset.columnId] = list.scrollTop;
            }
        });
        const rowBefore = columnsElement.querySelector('[data-board-columns]');
        const rowScroll = rowBefore ? Number(rowBefore.scrollLeft) || 0 : 0;
        columnsElement.innerHTML = payload.columns_html;
        columnsElement.querySelectorAll('[data-column-id]').forEach((column) => {
            const list = column.querySelector('[data-column-list]');
            if (list && scrolled[column.dataset.columnId]) {
                list.scrollTop = scrolled[column.dataset.columnId];
            }
        });
        const rowAfter = columnsElement.querySelector('[data-board-columns]');
        if (rowAfter && rowScroll) {
            rowAfter.scrollLeft = rowScroll;
        }
        columnsRevision = revision;
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(columnsElement);
        }
    };

    const applyTabs = (payload) => {
        if (!tabsElement || typeof payload.tabs_html !== 'string') {
            return;
        }
        const revision = revisionOf(payload.tabs_revision);
        if (revision && revision === tabsRevision) {
            return;
        }
        if (menuOpen()) {
            // A tab's menu may hold a name half typed: wait for it to close.
            deferred = true;
            return;
        }
        tabsElement.innerHTML = payload.tabs_html;
        tabsRevision = revision;
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(tabsElement);
        }
    };

    const applyPanel = (payload) => {
        if (!panelElement || typeof payload.panel_html !== 'string') {
            return;
        }
        const revision = revisionOf(payload.panel_revision);
        if (revision && revision === panelRevision) {
            return;
        }
        if (dirty) {
            if (conflictBanner) {
                conflictBanner.hidden = false;
            }
            return;
        }
        panelElement.innerHTML = payload.panel_html;
        // The panel this page asked for is no longer drawn (the right to
        // create a card here is gone, for one): nothing is left to show.
        panelElement.hidden = payload.panel_html === '';
        panelRevision = revision;
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(panelElement);
        }
    };

    // A reader within this many pixels of the end of the message list counts
    // as «at the bottom» and is kept there when a new message arrives.
    const BOTTOM_SLACK = 24;

    /**
     * «Обсуждение»'s messages: read-only, so replaced whenever the
     * fingerprint moved. Its form is outside this block, so what is being
     * typed there is never redrawn, and no message is part of the guarded
     * panel. A reader at the bottom of the list stays at the bottom and sees
     * the new message; one who scrolled up stays where they were.
     */
    const applyComments = (payload) => {
        if (!commentsElement || typeof payload.comments_html !== 'string' || !payload.comments_html) {
            return;
        }
        const revision = revisionOf(payload.comments_revision);
        if (revision && revision === commentsRevision) {
            return;
        }
        const scrollTop = Number(commentsElement.scrollTop) || 0;
        const atBottom =
            Number(commentsElement.scrollHeight || 0) - scrollTop - Number(commentsElement.clientHeight || 0)
            <= BOTTOM_SLACK;
        commentsElement.innerHTML = payload.comments_html;
        commentsElement.scrollTop = atBottom ? Number(commentsElement.scrollHeight || 0) : scrollTop;
        commentsRevision = revision;
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(commentsElement);
        }
    };

    const coordinator = core.createRefreshCoordinator({
        url: fragmentUrl,
        apply(payload) {
            applyTabs(payload);
            applyColumns(payload);
            applyPanel(payload);
            applyComments(payload);
        },
        // A lost session stops the whole client; a board that is gone stops
        // only this coordinator, which `createRefreshCoordinator` already did.
        onDenied: (reason) => {
            if (reason === 'auth') {
                core.stop();
            }
        },
    });

    const refresh = () => coordinator.schedule(null);

    const resumeDeferred = () => {
        if (deferred && !isBusy() && !menuOpen()) {
            deferred = false;
            refresh();
        }
    };
    document.addEventListener('quality:board-idle', resumeDeferred);
    // `toggle` does not bubble; a capturing listener still sees a menu close.
    document.addEventListener('toggle', (event) => {
        const target = event.target;
        if (target && typeof target.matches === 'function' && target.matches('details[data-board-menu]') && !target.open) {
            resumeDeferred();
        }
    }, true);

    core.registerAdapter({
        name: 'board',
        revisions: ['boards'],
        refresh,
        stop: () => coordinator.stop(),
    });
    core.onOpen(refresh);

    core.subscribe(core.EVENT_TYPES.BOARD_UPDATED, (payload) => {
        if (Number(payload.resource_id) === boardId) {
            refresh();
        }
    });

    // A task closed from its own page, reopened by an administrator or given
    // another исполнитель says so on `task.*`, which reaches its исполнители;
    // everybody else learns it from the `boards` revision.
    const showsTask = (taskId) =>
        core.isPositiveInteger(taskId)
        && Boolean(
            (columnsElement && columnsElement.querySelector(`[data-task-id="${taskId}"]`))
            || (panelElement && Number(panelElement.dataset.taskId) === taskId),
        );
    [
        core.EVENT_TYPES.TASK_CREATED,
        core.EVENT_TYPES.TASK_UPDATED,
        core.EVENT_TYPES.TASK_COMPLETED,
    ].forEach((eventType) =>
        core.subscribe(eventType, (payload) => {
            if (showsTask(Number(payload.resource_id))) {
                refresh();
            }
        }),
    );

    core.boardLive = {
        coordinator,
        /**
         * Whether this page is kept current: `board_dnd.js` asks it after
         * moving the open card and, when it is, leaves the panel to the
         * `board.updated` that move publishes. A lost session (`core.stop()`)
         * or a vanished board ends it.
         */
        get isActive() {
            return core.state !== core.STATES.STOPPED && !coordinator.isStopped;
        },
        get isDirty() {
            return dirty;
        },
        get isDeferred() {
            return deferred;
        },
    };
})();
