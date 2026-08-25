/**
 * Protocol registry and the open protocol page.
 *
 * The same shape as `acts.js`, for the same reason: the protocol page contains
 * a real editor, so its content block is replaced only while the user has no
 * unsaved input. A dirty editor is never touched — the user gets a conflict
 * banner, keeps everything they typed, and the workflow buttons that would act
 * on a state that no longer exists are disabled. The server-side row lock and
 * status re-check remain the real guard; this is presentation.
 *
 * No markup is built here. Every block comes from the ordinary
 * permission-checked fragment endpoints, which render the very same partials
 * as a full page load.
 *
 * The collaboration blocks follow the same rule from the other side: the
 * comment feed and the attachment list are live, and the textarea and the file
 * picker beside them are not part of either fragment, so a refresh can never
 * discard half-typed input.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('protocols')) {
        // A repeated include would register duplicate dirty-state listeners
        // and a second coordinator per block.
        return;
    }

    const PROTOCOL_EVENTS = [
        core.EVENT_TYPES.PROTOCOL_CREATED,
        core.EVENT_TYPES.PROTOCOL_UPDATED,
        core.EVENT_TYPES.PROTOCOL_DELETED,
        core.EVENT_TYPES.PROTOCOL_STATUS_CHANGED,
        core.EVENT_TYPES.PROTOCOL_APPROVAL_CHANGED,
    ];

    // ---------------------------------------------------------------- registry

    const registry = document.querySelector('[data-live-protocol-registry]');
    if (registry && registry.dataset.fragmentUrl) {
        const results = registry.querySelector('[data-live-protocol-registry-results]');

        const coordinator = core.createRefreshCoordinator({
            // The page's own query string goes back with the request, so the
            // active tab is decided by the same server-side queryset as a
            // reload — never by anything this module knows about filtering.
            url: () => core.withCurrentQuery(registry.dataset.fragmentUrl),
            apply(payload) {
                if (!results || typeof payload.results_html !== 'string') {
                    return;
                }
                results.innerHTML = payload.results_html;
                if (window.qualityFragments) {
                    window.qualityFragments.reinitialise(results);
                }
            },
            // A lost session on this endpoint means every block is stale, not
            // just the registry: stop the whole client.
            onDenied: () => core.stop(),
        });

        core.registerAdapter({
            name: 'protocolRegistry',
            revisions: ['protocols'],
            refresh: () => coordinator.schedule(null),
            stop: () => coordinator.stop(),
        });
        core.onOpen(() => coordinator.schedule(null));
        // Silently, like the act and task registries: a protocol somebody else
        // created, edited, signed or deleted is a shared list moving, not a
        // message addressed to this user.
        PROTOCOL_EVENTS.forEach((eventType) =>
            core.subscribe(eventType, () => coordinator.schedule(null)),
        );

        core.protocolRegistry = { coordinator };
    }

    // ------------------------------------------------------------------ detail

    const config = document.querySelector('[data-live-protocol-config]');
    if (!config) {
        return;
    }
    const protocolId = Number(config.dataset.liveProtocolId);
    if (!core.isPositiveInteger(protocolId)) {
        return;
    }

    const conflictBanner = document.querySelector('[data-protocol-conflict-banner]');
    const accessBanner = document.querySelector('[data-protocol-access-banner]');
    const reloadButton = document.querySelector('[data-protocol-conflict-reload]');
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
    // Adding or removing an editor row changes the submission just as much as
    // typing does, even though no field fired an `input`.
    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || typeof target.closest !== 'function') {
            return;
        }
        if (
            target.closest(
                '[data-add-row], [data-remove-row], [data-add-assignee], [data-remove-assignee]',
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
        // These act on a status that no longer exists. Ordinary fields stay
        // editable so nothing typed is lost.
        document.querySelectorAll('[data-protocol-workflow-submit]').forEach((button) => {
            button.disabled = true;
        });
    };

    const blocks = {};

    const handleAccessLoss = () => {
        // A 404 here means the protocol was deleted or is no longer readable —
        // not that the session is gone, so the bell and every other page's
        // coordinators keep working.
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
                    // Unsaved input wins: warn instead of replacing, and stop
                    // offering actions built for the state that just changed.
                    showConflictBanner();
                    disableStaleWorkflowActions();
                    return;
                }
                element.innerHTML = payload.html;
                if (window.qualityFragments) {
                    // The protocol editor re-binds itself through the very same
                    // initialiser after the swap.
                    window.qualityFragments.reinitialise(element);
                }
            },
            onDenied: (reason) => (reason === 'auth' ? core.stop() : handleAccessLoss()),
        });
        return { element, coordinator };
    };

    // Only the blocks of the current tab exist; the other tabs are an ordinary
    // server render and are therefore already current when they are opened.
    //
    // `comments` and `attachments` are the *lists*, never the forms next to
    // them: both partials deliberately exclude the textarea and the file
    // picker, so somebody else's comment refreshes the feed without touching
    // what this user is in the middle of typing. That is also why neither is
    // `guardDirty` — there is nothing in them to lose.
    Object.assign(blocks, {
        heading: makeBlock('[data-live-protocol-heading]', 'headingUrl'),
        approval: makeBlock('[data-live-protocol-approval]', 'approvalUrl'),
        content: makeBlock('[data-live-protocol-content]', 'contentUrl', { guardDirty: true }),
        history: makeBlock('[data-live-protocol-history]', 'historyUrl'),
        comments: makeBlock('[data-live-protocol-comments]', 'commentsUrl'),
        attachments: makeBlock('[data-live-protocol-attachments]', 'attachmentsUrl'),
        activities: makeBlock('[data-live-protocol-activities]', 'activitiesUrl'),
    });

    const refresh = (names) => {
        names.forEach((name) => {
            if (blocks[name]) {
                blocks[name].coordinator.schedule(null);
            }
        });
    };

    const refreshAll = () =>
        refresh([
            'heading',
            'approval',
            'content',
            'history',
            'comments',
            'attachments',
            'activities',
        ]);

    const isThisProtocol = (payload) => Number(payload.resource_id) === protocolId;

    core.registerAdapter({
        name: 'protocolDetail',
        revisions: ['protocols'],
        refresh: refreshAll,
        stop: () =>
            Object.keys(blocks).forEach(
                (name) => blocks[name] && blocks[name].coordinator.stop(),
            ),
    });

    // «Связанные мероприятия» follows tasks, not protocols: a task completed
    // by its assignee never moves the `protocols` token. The `tasks` revision
    // already covers every readable task, protocol ones included, so this needs
    // no new aggregate — only its own adapter, so an unrelated task change
    // refetches one block instead of the whole page.
    if (blocks.activities) {
        core.registerAdapter({
            name: 'protocolActivities',
            revisions: ['tasks'],
            refresh: () => refresh(['activities']),
            stop: () => blocks.activities.coordinator.stop(),
        });
    }

    core.onOpen(refreshAll);

    core.subscribe(core.EVENT_TYPES.PROTOCOL_UPDATED, (payload) => {
        if (!isThisProtocol(payload)) {
            return;
        }
        // Somebody else stored a different document than the one on screen —
        // or added a comment or a file, which the collaboration services
        // announce on this same event because they change what a reader sees.
        refresh(['heading', 'content', 'history', 'comments', 'attachments']);
        if (dirty) {
            showConflictBanner();
        }
    });

    [core.EVENT_TYPES.PROTOCOL_STATUS_CHANGED, core.EVENT_TYPES.PROTOCOL_APPROVAL_CHANGED].forEach(
        (eventType) =>
            core.subscribe(eventType, (payload) => {
                if (!isThisProtocol(payload)) {
                    return;
                }
                // A clean page is replaced with current server markup, actions
                // included. A dirty one keeps its input and is told instead.
                refreshAll();
                if (dirty) {
                    showConflictBanner();
                    disableStaleWorkflowActions();
                }
                // The protocol workflow creates and closes approval and
                // decision tasks in the same transaction. Task events already
                // reach their assignees; this is what keeps an open task
                // registry current for a reader who is not one of them. The
                // task module's own behaviour is untouched.
                if (core.taskList) {
                    core.taskList.coordinator.schedule(null);
                }
            }),
    );

    // A task created, completed or reassigned elsewhere changes a row of
    // «Связанные мероприятия». The tasks module owns those events; this only
    // listens, and refetches through the protocol's own permission-checked
    // fragment.
    [
        core.EVENT_TYPES.TASK_CREATED,
        core.EVENT_TYPES.TASK_COMPLETED,
        core.EVENT_TYPES.TASK_UPDATED,
    ].forEach((eventType) => {
        if (!eventType) {
            return;
        }
        core.subscribe(eventType, () => refresh(['activities']));
    });

    core.subscribe(core.EVENT_TYPES.PROTOCOL_DELETED, (payload) => {
        if (!isThisProtocol(payload)) {
            return;
        }
        // The page is looking at a protocol that no longer exists. Nothing is
        // refetched — every fragment would 404 — and nothing typed is thrown
        // away; the banner says the page is frozen.
        handleAccessLoss();
    });

    core.protocolDetail = {
        blocks,
        get isDirty() {
            return dirty;
        },
    };
})();
