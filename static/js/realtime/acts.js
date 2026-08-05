/**
 * Act registry and the open act page.
 *
 * The work tab contains real forms, so it is replaced only when the user has
 * no unsaved input. A dirty form is never touched: the user gets a conflict
 * banner instead and keeps everything they typed.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('acts')) {
        // A repeated include would register duplicate dirty-state listeners
        // and a second coordinator per block.
        return;
    }

    // ---------------------------------------------------------------- registry

    const registry = document.querySelector('[data-live-act-registry]');
    if (registry && registry.dataset.fragmentUrl) {
        const kpis = registry.querySelector('[data-live-act-registry-kpis]');
        const results = registry.querySelector('[data-live-act-registry-results]');

        const coordinator = core.createRefreshCoordinator({
            url: () => core.withCurrentQuery(registry.dataset.fragmentUrl),
            apply(payload) {
                if (kpis && typeof payload.kpis_html === 'string') {
                    kpis.innerHTML = payload.kpis_html;
                }
                if (results && typeof payload.results_html === 'string') {
                    results.innerHTML = payload.results_html;
                    if (window.qualityFragments) {
                        window.qualityFragments.reinitialise(results);
                    }
                }
            },
            // A lost session on this endpoint means every block is stale, not
            // just the registry: stop the whole client.
            onDenied: () => core.stop(),
        });

        core.registerAdapter({
            name: 'actRegistry',
            revisions: ['acts'],
            refresh: () => coordinator.schedule(null),
            stop: () => coordinator.stop(),
        });
        core.onOpen(() => coordinator.schedule(null));
        // A new act must appear without F5 — silently, with no extra toast.
        [
            core.EVENT_TYPES.ACT_CREATED,
            core.EVENT_TYPES.ACT_UPDATED,
            core.EVENT_TYPES.ACT_STATUS_CHANGED,
        ].forEach((eventType) => core.subscribe(eventType, () => coordinator.schedule(null)));

        core.actRegistry = { coordinator };
    }

    // ------------------------------------------------------------------ detail

    const config = document.querySelector('[data-live-act-config]');
    if (!config) {
        return;
    }
    const actId = Number(config.dataset.liveActId);
    if (!core.isPositiveInteger(actId)) {
        return;
    }

    const conflictBanner = document.querySelector('[data-act-conflict-banner]');
    const accessBanner = document.querySelector('[data-act-access-banner]');
    const reloadButton = document.querySelector('[data-act-conflict-reload]');
    if (reloadButton) {
        reloadButton.addEventListener('click', () => window.location.reload());
    }

    // -- dirty-state tracking ---------------------------------------------
    //
    // Only a real user gesture marks the page dirty. A programmatic fragment
    // replacement dispatches nothing, so it cannot raise a false positive.
    let dirty = false;
    const markDirty = (event) => {
        if (event && event.isTrusted === false) {
            return;
        }
        dirty = true;
    };
    ['input', 'change'].forEach((type) =>
        document.addEventListener(type, (event) => {
            const target = event.target;
            if (!target || !target.tagName) {
                return;
            }
            const tag = target.tagName.toUpperCase();
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
                markDirty(event);
            }
        }),
    );
    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || typeof target.closest !== 'function') {
            return;
        }
        if (
            target.closest(
                '[data-add-root-analysis], [data-add-corrective-action], [data-remove-root-analysis], [data-remove-corrective-action]',
            )
        ) {
            markDirty(event);
        }
    });

    const showConflictBanner = () => {
        if (conflictBanner) {
            conflictBanner.hidden = false;
        }
    };

    const disableStaleWorkflowActions = () => {
        // These submits would act on a status that no longer exists. Ordinary
        // fields and textareas stay editable so nothing typed is lost, and the
        // server-side row lock plus status re-check remains the real guard.
        document.querySelectorAll('[data-workflow-submit]').forEach((button) => {
            button.disabled = true;
        });
    };

    const blocks = {};

    const handleAccessLoss = () => {
        Object.keys(blocks).forEach((name) => {
            if (blocks[name]) {
                blocks[name].coordinator.stop();
            }
        });
        disableStaleWorkflowActions();
        if (accessBanner) {
            accessBanner.hidden = false;
        }
        if (conflictBanner) {
            conflictBanner.hidden = true;
        }
    };

    const makeBlock = (selector, urlKey, options = {}) => {
        const element = document.querySelector(selector);
        const url = config.dataset[urlKey];
        if (!element || !url) {
            return null;
        }
        const coordinator = core.createRefreshCoordinator({
            url,
            apply(payload) {
                if (typeof payload.html !== 'string') {
                    return;
                }
                if (options.guardDirty && dirty) {
                    // Unsaved input wins: warn instead of replacing.
                    showConflictBanner();
                    return;
                }
                element.innerHTML = payload.html;
                if (window.qualityFragments) {
                    // Dynamic formsets and assignee filtering must work again
                    // after the swap, using the very same initialisers.
                    window.qualityFragments.reinitialise(element);
                }
            },
            // A lost session (401, or a stray redirect) means every block on
            // every page is stale, not just this act: stop the whole client.
            // A per-act 404 only means this act became inaccessible — the
            // bell and every other coordinator keep working.
            onDenied: (reason) => (reason === 'auth' ? core.stop() : handleAccessLoss()),
        });
        return { element, coordinator };
    };

    // Only blocks present on the current tab exist; opening another tab is an
    // ordinary server render and is therefore already current.
    Object.assign(blocks, {
        summary: makeBlock('[data-live-act-summary]', 'summaryUrl'),
        work: makeBlock('[data-live-act-work]', 'workUrl', { guardDirty: true }),
        history: makeBlock('[data-live-act-history]', 'historyUrl'),
        comments: makeBlock('[data-live-act-comments]', 'commentsUrl'),
        activities: makeBlock('[data-live-act-activities]', 'activitiesUrl'),
    });

    const refresh = (names) => {
        names.forEach((name) => {
            if (blocks[name]) {
                blocks[name].coordinator.schedule(null);
            }
        });
    };

    const isForThisAct = (payload, key) => Number(payload.data && payload.data[key]) === actId;

    core.registerAdapter({
        name: 'actDetail',
        revisions: ['acts', 'comments', 'activities'],
        refresh: () => refresh(['summary', 'work', 'history', 'comments', 'activities']),
        stop: () => Object.keys(blocks).forEach((name) => blocks[name] && blocks[name].coordinator.stop()),
    });

    core.onOpen(() => refresh(['summary', 'work', 'history', 'comments', 'activities']));

    core.subscribe(core.EVENT_TYPES.ACT_UPDATED, (payload) => {
        if (payload.resource_id !== actId) {
            return;
        }
        refresh(['summary']);
        if (dirty) {
            showConflictBanner();
        }
    });

    core.subscribe(core.EVENT_TYPES.ACT_STATUS_CHANGED, (payload) => {
        if (payload.resource_id !== actId) {
            return;
        }
        // A clean work tab is replaced with current server markup, including
        // the actions the new status allows. A dirty one is left alone.
        refresh(['summary', 'work', 'history']);
        if (dirty) {
            showConflictBanner();
            disableStaleWorkflowActions();
        }
    });

    core.subscribe(core.EVENT_TYPES.COMMENT_CREATED, (payload) => {
        if (!isForThisAct(payload, 'act_id')) {
            return;
        }
        refresh(['comments']);
    });

    [
        core.EVENT_TYPES.TASK_CREATED,
        core.EVENT_TYPES.TASK_UPDATED,
        core.EVENT_TYPES.TASK_COMPLETED,
    ].forEach((eventType) =>
        core.subscribe(eventType, (payload) => {
            if (!isForThisAct(payload, 'act_id')) {
                return;
            }
            refresh(['activities']);
        }),
    );

    core.actDetail = {
        blocks,
        get isDirty() {
            return dirty;
        },
    };
})();
