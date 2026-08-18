/**
 * Calculator → «Проработка».
 *
 * The journal is a shared table every authenticated user reads, so a row
 * somebody else creates, confirms, reopens or deletes has to appear here
 * without F5. The events say only *that* the journal moved; the rows
 * themselves always come from the ordinary `calculator:entry_list` endpoint,
 * which is the same permission-checked GET the page already uses on load and
 * behind «Обновить журнал».
 *
 * Nothing about the table is rendered here: the payload is handed straight to
 * the calculator controller, which owns filtering, formatting and the row
 * markup. Because the response is the authoritative list keyed by id, a
 * browser that receives the echo of its own mutation simply re-renders the
 * same rows — no duplicate is possible.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('workup')) {
        // A repeated include would register a second coordinator and double
        // every journal request.
        return;
    }

    const root = document.querySelector('[data-calculator]');
    if (!root || !root.dataset.entriesUrl) {
        // Any page other than the calculator: nothing to keep in sync.
        return;
    }

    // Resolved per call rather than captured: `calculator/app.js` publishes the
    // controller only when the page it belongs to actually initialised.
    const journalView = () => (window.windingCalculator || {}).journal || null;

    const coordinator = core.createRefreshCoordinator({
        url: root.dataset.entriesUrl,
        apply(payload) {
            const view = journalView();
            if (!view || !Array.isArray(payload.entries)) {
                return;
            }
            view.applyEntries(payload.entries);
        },
        // A lost session means every block on the page is stale, not just the
        // journal: stop the whole client. Anything else leaves the table as it
        // is — the manual «Обновить журнал» button keeps working.
        onDenied: (reason) => {
            if (reason === 'auth') {
                core.stop();
            }
        },
    });

    // Recovery: `/realtime/sync/` moves the `workup` token whenever the journal
    // changed, so a reconnect after a dropped stream refetches it once instead
    // of the calculator polling on its own.
    // How the controller asks for a current list when a live update arrived
    // while one of its own mutations was still in flight.
    const view = journalView();
    if (view) {
        view.requestRefresh = () => coordinator.schedule(null);
    }

    core.registerAdapter({
        name: 'workupJournal',
        revisions: ['workup'],
        refresh: () => coordinator.schedule(null),
        stop: () => coordinator.stop(),
    });

    core.onOpen(() => coordinator.schedule(null));

    // Silently, like the act and task registries: the journal is a shared list,
    // not a message addressed to this user.
    [
        core.EVENT_TYPES.WORKUP_CREATED,
        core.EVENT_TYPES.WORKUP_UPDATED,
        core.EVENT_TYPES.WORKUP_DELETED,
    ].forEach((eventType) => core.subscribe(eventType, () => coordinator.schedule(null)));

    core.workupJournal = { coordinator };
})();
