/**
 * Task registry.
 *
 * Whether a task belongs in the current tab and filters is decided entirely by
 * the Django queryset behind the fragment endpoint — never here.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('tasks')) {
        // A repeated include would register a second coordinator for the same
        // container and double every fragment request.
        return;
    }

    const container = document.querySelector('[data-live-task-list]');
    if (!container || !container.dataset.fragmentUrl) {
        return;
    }

    const coordinator = core.createRefreshCoordinator({
        url: () => core.withCurrentQuery(container.dataset.fragmentUrl),
        apply(payload) {
            if (typeof payload.results_html !== 'string') {
                return;
            }
            // Only the results are swapped: the tabs and the filter form stay
            // untouched, so focus, typing and scroll position survive.
            container.innerHTML = payload.results_html;
            if (window.qualityFragments) {
                window.qualityFragments.reinitialise(container);
            }
        },
        // A lost session on this endpoint means every block is stale, not
        // just the task list: stop the whole client rather than leaving the
        // rest of the page (bell, other coordinators, tabs) running dead.
        onDenied: () => core.stop(),
    });

    core.registerAdapter({
        name: 'taskList',
        revisions: ['tasks'],
        refresh: () => coordinator.schedule(null),
        stop: () => coordinator.stop(),
    });

    core.onOpen(() => coordinator.schedule(null));

    // Task events never toast: the user already gets one through the matching
    // internal notification when the workflow defines it.
    [
        core.EVENT_TYPES.TASK_CREATED,
        core.EVENT_TYPES.TASK_UPDATED,
        core.EVENT_TYPES.TASK_COMPLETED,
    ].forEach((eventType) => core.subscribe(eventType, () => coordinator.schedule(null)));

    core.taskList = { coordinator, container };
})();
