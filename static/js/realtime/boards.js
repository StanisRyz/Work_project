/**
 * The open board page — one sub-board — kept current: its tabs, its columns
 * and the open card's drawer.
 *
 * Every live block is fetched from `boards:fragment`, which renders the very
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
 *   [data-live-board-panel] +  the card drawer's guarded block, in three
 *   [data-live-board-card] +   containers with one fingerprint
 *   [data-live-board-facts]    (`panel_revision`): the heading with
 *                              «Завершить», «Описание»'s text or the card's
 *                              form, and the facts with the tools. It holds
 *                              forms, so it is guarded exactly like the act and
 *                              protocol work blocks: an unchanged fingerprint
 *                              does nothing, a changed one replaces a clean
 *                              block, and a block with unsaved input keeps
 *                              every typed character and raises the conflict
 *                              banner instead.
 *   [data-live-board-comments] «Чат»: its messages with their files, and the
 *                              list «Только файлы» — read-only, replaced
 *                              whenever its fingerprint moved (a message, a
 *                              file deleted), keeping a reader at the bottom
 *                              there. Its form — and the files chosen in it —
 *                              is outside every block, and no message is part
 *                              of the guarded block: a message never raises
 *                              the banner.
 *   [data-live-board-followers] «Подписчики» on «Описание»: read-only,
 *                              replaced whenever its fingerprint moved — a new
 *                              follower never raises the banner either.
 *   [data-live-board-log]      «Лог»: read-only, replaced whenever its
 *                              fingerprint moved; no entry is part of the
 *                              guarded block either.
 *   [data-live-board-checklist] the card's «Чек-лист» on «Описание»: buttons
 *                              only, replaced whenever its fingerprint moved —
 *                              a tick never raises the banner over an edit —
 *                              but not while one item's «Изменить» form is
 *                              open (`[data-checklist-edit]`): deferred like
 *                              the menus, and fetched again once it is gone.
 *                              «Добавить пункт» is outside it.
 *
 * The numbers beside «Чат» and «Только файлы» are set with the messages'
 * block (`chat_count`, `files_count`). Which tab is shown is the drawer's own
 * `data-board-tab`, outside every block, so no replacement changes it.
 *
 * The drawer is opened, switched and closed without a reload by
 * `board_drawer.js`, which rewrites the addresses and fingerprints on
 * `[data-board]` and says so with `quality:board-drawer`. Everything here
 * therefore reads them off `[data-board]` and finds the drawer's blocks anew
 * each time; an answer fetched for an address the page no longer shows is
 * dropped and fetched again.
 *
 * What makes it refetch: `board.updated` for this board, a `task.*` event for
 * a task whose tile or panel is on the page, and the `boards` sync revision
 * moving (a reconnect, a recovery sync, a task closed from its own page).
 *
 * Without real-time (`QualityRealtime` is null) none of this runs and the
 * board is exactly the page the server drew; dragging and the drawer do not
 * depend on it.
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
    if (!core.isPositiveInteger(boardId) || !root.dataset.boardFragmentUrl) {
        return;
    }

    const tabsElement = root.querySelector('[data-live-board-tabs]');
    const columnsElement = root.querySelector('[data-live-board-columns]');
    // The drawer's blocks come and go with the card it shows: found anew.
    const panelElement = () => root.querySelector('[data-live-board-panel]');
    const cardElement = () => root.querySelector('[data-live-board-card]');
    const factsElement = () => root.querySelector('[data-live-board-facts]');
    const followersElement = () => root.querySelector('[data-live-board-followers]');
    const commentsElement = () => root.querySelector('[data-live-board-comments]');
    const logElement = () => root.querySelector('[data-live-board-log]');
    const checklistElement = () => root.querySelector('[data-live-board-checklist]');
    const conflictBanner = document.querySelector('[data-board-conflict-banner]');
    const reloadButton = document.querySelector('[data-board-conflict-reload]');
    if (reloadButton) {
        // The board's own address for this panel, never `reload()`: a page
        // drawn in answer to a refused POST would post it again.
        reloadButton.addEventListener('click', () =>
            window.location.replace(root.dataset.boardPageUrl || root.dataset.boardUrl || window.location.pathname),
        );
    }

    // The fingerprints live on `[data-board]`, where `board_drawer.js` also
    // writes those of a card it opens.
    const revision = (name) => root.dataset[name] || '';
    const setRevision = (name, value) => {
        root.dataset[name] = value;
    };
    // A page re-rendered from a refused form already shows input that is not
    // stored.
    let dirty = root.dataset.panelHoldsInput === 'true';
    let deferred = false;

    // -- dirty-state tracking ---------------------------------------------
    //
    // Only a real gesture inside the guarded block counts: a drag, the
    // confirmation modal, the chat's form or the bug-report dialog type
    // nothing a refresh could discard, and a programmatic replacement
    // dispatches nothing.
    const insidePanel = (target) =>
        Boolean(target) && [panelElement(), cardElement(), factsElement()].some(
            (element) => element && typeof element.contains === 'function' && element.contains(target),
        );
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
    // An item of the «Чек-лист» being renamed: its form holds typed text.
    const checklistEditing = () => {
        const checklist = checklistElement();
        return Boolean(checklist && checklist.querySelector('[data-checklist-edit]'));
    };

    const revisionOf = (value) => (typeof value === 'string' ? value : '');

    const applyColumns = (payload) => {
        if (!columnsElement || typeof payload.columns_html !== 'string') {
            return;
        }
        const next = revisionOf(payload.columns_revision);
        if (next && next === revision('columnsRevision')) {
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
        setRevision('columnsRevision', next);
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(columnsElement);
        }
    };

    const applyTabs = (payload) => {
        if (!tabsElement || typeof payload.tabs_html !== 'string') {
            return;
        }
        const next = revisionOf(payload.tabs_revision);
        if (next && next === revision('tabsRevision')) {
            return;
        }
        if (menuOpen()) {
            // A tab's menu may hold a name half typed: wait for it to close.
            deferred = true;
            return;
        }
        tabsElement.innerHTML = payload.tabs_html;
        setRevision('tabsRevision', next);
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(tabsElement);
        }
    };

    const setText = (selector, value) => {
        const count = root.querySelector(selector);
        if (count && Number.isInteger(value)) {
            count.textContent = String(value);
        }
    };
    const setCount = (name, value) => setText(`[data-board-tab-count="${name}"]`, value);

    const applyPanel = (payload) => {
        const panel = panelElement();
        const card = cardElement();
        const facts = factsElement();
        if (!panel || typeof payload.panel_html !== 'string') {
            return;
        }
        const next = revisionOf(payload.panel_revision);
        if (next && next === revision('panelRevision')) {
            return;
        }
        if (dirty) {
            if (conflictBanner) {
                conflictBanner.hidden = false;
            }
            return;
        }
        if (payload.panel_html === '') {
            // The panel this page asked for is no longer drawn (the right to
            // create a card here is gone, for one): nothing is left to show.
            if (window.qualityBoardDrawer) {
                window.qualityBoardDrawer.close({ push: false });
            }
            return;
        }
        panel.innerHTML = payload.panel_html;
        if (card && typeof payload.card_html === 'string') {
            card.innerHTML = payload.card_html;
        }
        if (facts && typeof payload.facts_html === 'string') {
            facts.innerHTML = payload.facts_html;
        }
        setRevision('panelRevision', next);
        if (window.qualityFragments) {
            [panel, card, facts].forEach((element) => {
                if (element) {
                    window.qualityFragments.reinitialise(element);
                }
            });
        }
    };

    /**
     * «Подписчики»: avatars only, so replaced whenever the fingerprint moved —
     * a colleague who starts following the card never touches the guarded
     * block, and never raises the banner over a result being typed.
     */
    const applyFollowers = (payload) => {
        const followers = followersElement();
        if (!followers || typeof payload.followers_html !== 'string') {
            return;
        }
        const next = revisionOf(payload.followers_revision);
        if (next && next === revision('followersRevision')) {
            return;
        }
        followers.innerHTML = payload.followers_html;
        setRevision('followersRevision', next);
    };

    /**
     * The card's «Чек-лист»: buttons and links only, so replaced whenever its
     * fingerprint moved — except while an item's «Изменить» form is open,
     * which holds typed text: then the refresh waits, like the menus.
     */
    const applyChecklist = (payload) => {
        const checklist = checklistElement();
        if (!checklist || typeof payload.checklist_html !== 'string') {
            return;
        }
        const next = revisionOf(payload.checklist_revision);
        if (next && next === revision('checklistRevision')) {
            return;
        }
        if (checklistEditing()) {
            deferred = true;
            return;
        }
        checklist.innerHTML = payload.checklist_html;
        setRevision('checklistRevision', next);
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(checklist);
        }
    };

    // A reader within this many pixels of the end of the message list counts
    // as «at the bottom» and is kept there when a new message arrives.
    const BOTTOM_SLACK = 24;

    /**
     * «Чат»'s messages: read-only, so replaced whenever the fingerprint
     * moved. Its form is outside this block, so what is being typed there is
     * never redrawn, and no message is part of the guarded block. A reader at
     * the bottom of the list stays at the bottom and sees the new message; one
     * who scrolled up stays where they were. The number beside «Чат» follows.
     */
    const applyComments = (payload) => {
        const list = commentsElement();
        if (!list || typeof payload.comments_html !== 'string' || !payload.comments_html) {
            return;
        }
        const next = revisionOf(payload.comments_revision);
        if (next && next === revision('commentsRevision')) {
            return;
        }
        const scrollTop = Number(list.scrollTop) || 0;
        const atBottom =
            Number(list.scrollHeight || 0) - scrollTop - Number(list.clientHeight || 0)
            <= BOTTOM_SLACK;
        list.innerHTML = payload.comments_html;
        list.scrollTop = atBottom ? Number(list.scrollHeight || 0) : scrollTop;
        setRevision('commentsRevision', next);
        setCount('chat', payload.chat_count);
        setText('[data-board-chat-files-count]', payload.files_count);
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(list);
        }
    };

    /** «Лог»: read-only, newest first, replaced whenever its fingerprint moved. */
    const applyLog = (payload) => {
        const log = logElement();
        if (!log || typeof payload.log_html !== 'string' || !payload.log_html) {
            return;
        }
        const next = revisionOf(payload.log_revision);
        if (next && next === revision('logRevision')) {
            return;
        }
        log.innerHTML = payload.log_html;
        setRevision('logRevision', next);
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(log);
        }
    };

    // The address the newest request was made for: an answer for any other
    // (a card opened or a tab switched meanwhile) is dropped and refetched.
    let requested = '';

    const coordinator = core.createRefreshCoordinator({
        url: () => {
            requested = root.dataset.boardFragmentUrl;
            return requested;
        },
        apply(payload) {
            if (requested !== root.dataset.boardFragmentUrl) {
                refresh();
                return;
            }
            applyTabs(payload);
            applyColumns(payload);
            applyPanel(payload);
            applyComments(payload);
            applyLog(payload);
            applyChecklist(payload);
            applyFollowers(payload);
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
    // A card opened or the drawer closed by `board_drawer.js`: a new block
    // starts clean (or as the page says), the banner belongs to the old one,
    // and the columns are brought up to the new address.
    document.addEventListener('quality:board-drawer', () => {
        dirty = root.dataset.panelHoldsInput === 'true';
        if (conflictBanner) {
            conflictBanner.hidden = true;
        }
        refresh();
    });
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
    const showsTask = (taskId) => {
        if (!core.isPositiveInteger(taskId)) {
            return false;
        }
        const panel = panelElement();
        return Boolean(
            (columnsElement && columnsElement.querySelector(`[data-task-id="${taskId}"]`))
            || (panel && Number(panel.dataset.taskId) === taskId),
        );
    };
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
         * moving the open card and, when it is, leaves the drawer to the
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
