'use strict';

/**
 * Smoke test for the split browser client in `static/js/realtime/`.
 *
 * Runs on plain Node with the hand-rolled harness next to it — no npm, no
 * Jest, no jsdom, no build step. Invoked from `realtime/tests/test_js_client.py`
 * so it participates in the normal `manage.py test` run.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const {
    createEnvironment,
    FakeBroadcastChannel,
    FakeStorage,
    FakeEventSource,
    Bus,
    flush,
} = require('./dom_harness');

const CLIENT_DIR = path.join(__dirname, '..', '..', '..', 'static', 'js', 'realtime');
// The very order base.html loads them in.
const MODULES = ['core.js', 'tabs.js', 'sync.js', 'notifications.js', 'tasks.js', 'acts.js', 'start.js'];
const SOURCES = MODULES.map((name) => [name, fs.readFileSync(path.join(CLIENT_DIR, name), 'utf8')]);

function load(options = {}) {
    const env = createEnvironment(options);
    const context = {
        window: env.window,
        document: env.document,
        navigator: { onLine: true },
        fetch: env.window.fetch,
        EventSource: env.window.EventSource,
        AbortController,
        Number,
        JSON,
        Promise,
        Set,
        Map,
        Object,
        Array,
        Math,
        Date,
        CustomEvent: class CustomEvent {
            constructor(type, init) {
                this.type = type;
                this.detail = init && init.detail;
            }
        },
        console,
        setImmediate,
    };
    context.globalThis = context;
    env.navigator = context.navigator;
    vm.createContext(context);
    SOURCES.forEach(([name, source]) => vm.runInContext(source, context, { filename: name }));
    env.context = context;
    env.core = context.window.QualityRealtime;
    // Leader election is intentionally deferred by a small random delay, so the
    // fake clock has to run for a tab to settle into its role.
    env.clock.advance(400);
    return env;
}

/** Two tabs of the same user, sharing one BroadcastChannel bus and storage. */
function loadTabs(count, options = {}) {
    FakeBroadcastChannel.bus = new Bus();
    FakeEventSource.instances = [];
    const storage = new FakeStorage();
    const tabs = [];
    for (let index = 0; index < count; index += 1) {
        tabs.push(load({ ...options, storage, resetSources: false }));
    }
    return tabs;
}

function fragment(items, unreadCount) {
    const html = items
        .map(
            (item) =>
                `<a class="notification-menu__item" href="${item.url}" data-notification-id="${item.id}" data-notification-unread="true">`
                + `<strong data-notification-title>${item.title}</strong>`
                + `<span data-notification-message>${item.message}</span></a>`,
        )
        .join('');
    return {
        unread_count: unreadCount === undefined ? items.length : unreadCount,
        items_html: html,
        generated_at: '2026-08-04T10:00:00+00:00',
        latest_notification_id: items.length ? items[0].id : null,
    };
}

function snapshot(overrides = {}) {
    return {
        schema_version: 1,
        generated_at: '2026-08-04T10:00:00+00:00',
        unread_notifications: 0,
        revisions: {
            notifications: 'n1',
            tasks: 't1',
            acts: 'a1',
            comments: 'c1',
            activities: 'v1',
            ...overrides,
        },
    };
}

function createdEvent(resourceId, eventId) {
    return {
        schema_version: 1,
        event_id: eventId || `event-${resourceId}`,
        event_type: 'notification.created',
        occurred_at: '2026-08-04T10:00:00+00:00',
        resource_type: 'notification',
        resource_id: resourceId,
        data: { act_id: 3, recipient_id: 5, actor_id: null },
    };
}

function readEvent(eventId, scope = 'bell') {
    return {
        schema_version: 1,
        event_id: eventId || 'read-1',
        event_type: 'notification.read',
        occurred_at: '2026-08-04T10:00:00+00:00',
        resource_type: 'user',
        resource_id: 5,
        data: { changed_count: 2, unread_count: 0, scope },
    };
}

function taskEvent(type, resourceId, actId, eventId) {
    return {
        schema_version: 1,
        event_id: eventId || `${type}-${resourceId}`,
        event_type: type,
        occurred_at: '2026-08-04T10:00:00+00:00',
        resource_type: 'task',
        resource_id: resourceId,
        data: { act_id: actId, status_code: 'IN_PROGRESS' },
    };
}

function actEvent(type, resourceId, eventId) {
    return {
        schema_version: 1,
        event_id: eventId || `${type}-${resourceId}`,
        event_type: type,
        occurred_at: '2026-08-04T10:00:00+00:00',
        resource_type: 'act',
        resource_id: resourceId,
        data: {
            from_status_code: 'CREATED_OTK',
            to_status_code: 'KO_REVIEW',
            status_code: 'CREATED_OTK',
            author_id: 5,
        },
    };
}

function commentEvent(resourceId, actId, eventId) {
    return {
        schema_version: 1,
        event_id: eventId || `comment-${resourceId}`,
        event_type: 'comment.created',
        occurred_at: '2026-08-04T10:00:00+00:00',
        resource_type: 'comment',
        resource_id: resourceId,
        data: { act_id: actId, author_id: 5 },
    };
}

const settle = async (env, rounds = 3) => {
    for (let index = 0; index < rounds; index += 1) {
        env.clock.advance(300);
        await flush();
    }
};

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ------------------------------------------------------------ core lifecycle

test('a disabled configuration never opens an EventSource', async () => {
    const env = load({ realtimeEnabled: false });

    assert.equal(env.sources.length, 0);
    assert.equal(env.fetchCalls.length, 0);
});

test('a browser without EventSource degrades instead of failing', async () => {
    const env = load({ withEventSource: false });
    env.setFetchHandler(() => snapshot());
    await flush();

    assert.equal(env.core.state, 'degraded');
    assert.ok(env.core.sync.isPolling, 'polling must take over when SSE is impossible');
});

test('the separate modules all share one core and one stream', async () => {
    const env = load({ page: 'act-detail' });

    assert.equal(env.sources.length, 1);
    assert.equal(env.source.url, '/realtime/events/');
    assert.ok(env.core.notifications, 'notifications module registered');
    assert.ok(env.core.actDetail, 'acts module registered');
    assert.ok(env.core.sync, 'sync module registered');
    assert.ok(env.core.tabs, 'tabs module registered');
    assert.ok(env.core.adapters.length >= 2);
});

test('exactly one EventSource is created regardless of the page', async () => {
    for (const page of ['plain', 'tasks', 'acts', 'act-detail']) {
        const env = load({ page });
        assert.equal(env.sources.length, 1, `page ${page} must open one stream`);
    }
});

test('the state machine walks connecting -> live', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());

    assert.equal(env.core.state, 'connecting');
    env.source.emit('open');
    await flush();

    assert.equal(env.core.state, 'live');
});

test('no open within the degraded window switches to degraded and starts polling', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());

    assert.equal(env.core.state, 'connecting');
    env.clock.advance(21000);
    await flush();

    assert.equal(env.core.state, 'degraded');
    assert.ok(env.core.sync.isPolling);
});

test('an open stops polling again', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());
    env.clock.advance(21000);
    await flush();
    assert.ok(env.core.sync.isPolling);

    env.source.emit('open');
    await flush();

    assert.equal(env.core.state, 'live');
    assert.equal(env.core.sync.isPolling, false);
});

test('offline and online move the state and resync', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());

    env.window.dispatch('offline');
    assert.equal(env.core.state, 'offline');
    assert.equal(env.core.sync.isPolling, false, 'no polling while offline');

    env.window.dispatch('online');
    await flush();

    assert.equal(env.core.state, 'connecting');
    assert.ok(env.callsTo('/realtime/sync/').length >= 1, 'coming back online resyncs');
});

// ------------------------------------------------------------------- sync

test('every open triggers a sync', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());

    env.source.emit('open');
    await flush();

    assert.equal(env.callsTo('/realtime/sync/').length, 1);
});

test('identical revisions trigger no fragment requests at all', async () => {
    const env = load({ page: 'tasks' });
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : { results_html: '<p></p>', tab: 'my', task_ids: [] },
    );

    env.source.emit('open');
    // The open schedules a refresh and a sync; the sync's own snapshot then
    // schedules another. Settle both before measuring.
    await settle(env);
    const afterFirst = env.callsTo('/tasks/list-fragment/').length;

    env.core.sync.run();
    await settle(env);

    assert.equal(env.callsTo('/tasks/list-fragment/').length, afterFirst);
});

test('a changed token refreshes only its own block', async () => {
    const env = load({ page: 'act-detail' });
    let revisions = snapshot();
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/') ? revisions : { html: '<p data-x></p>' },
    );

    env.source.emit('open');
    env.clock.advance(300);
    await flush();
    const before = env.callsTo('/notifications/header-fragment/').length;

    revisions = snapshot({ notifications: 'n2' });
    env.core.sync.run();
    env.clock.advance(300);
    await flush();

    assert.equal(env.callsTo('/notifications/header-fragment/').length, before + 1);
});

test('the first sync updates blocks without a toast', async () => {
    const env = load();
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : fragment([{ id: 7, title: 'Есть', message: 'Текст', url: '/acts/3/' }]),
    );

    env.source.emit('open');
    env.clock.advance(300);
    await flush();

    assert.equal(env.menu.unreadCount, 1);
    assert.equal(env.toasts.length, 0);
});

test('a hidden tab polls on the longer interval', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());
    env.document.visibilityState = 'hidden';
    env.clock.advance(21000);
    await flush();
    const afterDegrade = env.callsTo('/realtime/sync/').length;

    env.clock.advance(35000);
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, afterDegrade, 'still waiting on the hidden interval');

    env.clock.advance(90000);
    await flush();
    assert.ok(env.callsTo('/realtime/sync/').length > afterDegrade);
});

test('a 401 from sync stops the client', async () => {
    const env = load();
    env.setFetchHandler(() => ({ status: 401 }));

    env.source.emit('open');
    await flush();

    assert.equal(env.core.state, 'stopped');
    const calls = env.fetchCalls.length;
    env.clock.advance(120000);
    await flush();
    assert.equal(env.fetchCalls.length, calls, 'no further requests after 401');
});

test('no more than one sync request runs at a time', async () => {
    const env = load();
    env.setFetchHandler(() => 'manual');

    env.core.sync.run();
    env.core.sync.run();
    env.core.sync.run();
    await flush();

    assert.equal(env.callsTo('/realtime/sync/').length, 1);
});

// ------------------------------------------------------------------- tabs

test('only the leader tab opens an EventSource', async () => {
    const [leader, follower] = loadTabs(2);

    assert.equal(leader.core.tabs.isLeader, true);
    assert.equal(follower.core.tabs.isLeader, false);
    assert.equal(FakeEventSource.instances.length, 1, 'one stream for both tabs');
});

test('a follower receives events over BroadcastChannel', async () => {
    const [leader, follower] = loadTabs(2, { page: 'tasks' });
    follower.setFetchHandler(() => ({ results_html: '<p data-followed></p>', tab: 'my', task_ids: [] }));
    leader.setFetchHandler(() => ({ results_html: '<p data-led></p>', tab: 'my', task_ids: [] }));

    FakeEventSource.instances[0].emitEvent('task.created', taskEvent('task.created', 9, 3));
    leader.clock.advance(300);
    follower.clock.advance(300);
    await flush();

    assert.ok(follower.live.taskList.querySelector('[data-followed]'), 'follower refreshed its own fragment');
    assert.ok(leader.live.taskList.querySelector('[data-led]'));
});

test('an expired lease lets another tab take over', async () => {
    const [leader, follower] = loadTabs(2);

    assert.equal(leader.core.tabs.isLeader, true);
    leader.core.stop();
    follower.window.localStorage.setItem(
        'quality-realtime-leader-v1',
        JSON.stringify({ tab_id: 'gone', expires_at: Date.now() - 1000 }),
    );

    follower.core.tabs.renewOrElect();
    follower.clock.advance(500);
    await flush();

    assert.equal(follower.core.tabs.isLeader, true, 'a new leader must be elected');
});

test('without BroadcastChannel every tab runs standalone', async () => {
    FakeBroadcastChannel.bus = new Bus();
    FakeEventSource.instances = [];
    const storage = new FakeStorage();
    const first = load({ broadcast: false, storage, resetSources: false });
    const second = load({ broadcast: false, storage, resetSources: false });

    assert.equal(FakeEventSource.instances.length, 2, 'each tab opens its own stream');
    assert.equal(first.core.tabs.isCoordinated, false);
    assert.equal(second.core.tabs.isCoordinated, false);
});

test('a broken localStorage never breaks the page', async () => {
    const env = load({ storage: new FakeStorage({ broken: true }) });

    assert.ok(env.core, 'the client still initialises');
    assert.equal(env.sources.length, 1, 'and still streams');
});

test('a duplicate event does not toast twice in one tab', async () => {
    const [leader, follower] = loadTabs(2);
    const handler = (call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : fragment([{ id: 11, title: 'Одно', message: 'Текст', url: '/acts/3/' }]);
    leader.setFetchHandler(handler);
    follower.setFetchHandler(handler);

    const source = FakeEventSource.instances[0];
    source.emitEvent('notification.created', createdEvent(11, 'same-id'));
    source.emitEvent('notification.created', createdEvent(11, 'same-id'));
    leader.clock.advance(300);
    follower.clock.advance(300);
    await flush();

    assert.equal(leader.toasts.length, 1);
    assert.equal(follower.toasts.length, 1, 'each tab shows it once, not twice');
});

// ------------------------------------------------------------- feature modules

test('task.created refreshes the task list fragment', async () => {
    const env = load({ page: 'tasks' });
    env.setFetchHandler(() => ({ results_html: '<table data-task-row="9"></table>', tab: 'my', task_ids: [9] }));

    env.source.emitEvent('task.created', taskEvent('task.created', 9, 3));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.taskList.querySelector('[data-task-row="9"]'));
    assert.equal(env.toasts.length, 0, 'task events must never toast');
});

test('task.completed refreshes the active list too', async () => {
    const env = load({ page: 'tasks' });
    env.setFetchHandler(() => ({ results_html: '<p data-empty></p>', tab: 'my', task_ids: [] }));

    env.source.emitEvent('task.completed', taskEvent('task.completed', 9, 3));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.taskList.querySelector('[data-empty]'));
    assert.equal(env.toasts.length, 0);
});

test('the task fragment request keeps the current query string', async () => {
    const env = load({ page: 'tasks' });
    env.window.location.search = '?tab=archive&sort=nearest';
    env.setFetchHandler(() => ({ results_html: '<p></p>', tab: 'archive', task_ids: [] }));

    env.source.emitEvent('task.updated', taskEvent('task.updated', 9, 3));
    env.clock.advance(300);
    await flush();

    assert.equal(
        env.callsTo('/tasks/list-fragment/')[0].url,
        '/tasks/list-fragment/?tab=archive&sort=nearest',
    );
});

test('a new act updates the registry silently', async () => {
    const env = load({ page: 'acts' });
    env.setFetchHandler(() => ({
        kpis_html: '<article data-kpi></article>',
        results_html: '<tr data-act-row="4"></tr>',
        act_ids: [4],
    }));

    env.source.emitEvent('act.created', actEvent('act.created', 4));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.actKpis.querySelector('[data-kpi]'));
    assert.ok(env.live.actResults.querySelector('[data-act-row="4"]'));
    assert.equal(env.toasts.length, 0, 'act.created must never toast');
});

test('act.status_changed refreshes summary, work and history', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler((call) => ({
        html: call.url.includes('history')
            ? '<p data-history></p>'
            : call.url.includes('work')
              ? '<section data-work></section>'
              : '<span data-badge></span>',
        status_code: 'KO_REVIEW',
    }));

    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 3));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.summary.querySelector('[data-badge]'));
    assert.ok(env.live.work.querySelector('[data-work]'), 'a clean work tab is replaced');
    assert.ok(env.live.history.querySelector('[data-history]'));
});

test('a replaced work fragment re-initialises the dynamic forms', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler(() => ({ html: '<section data-work></section>', status_code: 'KO_REVIEW' }));
    const before = env.window.qualityFragments.reinitialiseCalls;

    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 3));
    env.clock.advance(300);
    await flush();

    assert.ok(env.window.qualityFragments.reinitialiseCalls > before);
});

test('comment.created refreshes only the comments list', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler(() => ({ html: '<article data-comment></article>' }));

    env.source.emitEvent('comment.created', commentEvent(5, 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.callsTo('/acts/3/comments-fragment/').length, 1);
    assert.equal(env.callsTo('/acts/3/live-summary-fragment/').length, 0);
    assert.ok(env.live.comments.querySelector('[data-comment]'));
});

test('task events refresh the related activities of the open act', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler(() => ({ html: '<tr data-related-task="9"></tr>' }));

    env.source.emitEvent('task.completed', taskEvent('task.completed', 9, 3));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.activities.querySelector('[data-related-task="9"]'));
});

test('an event for another act is ignored', async () => {
    const env = load({ page: 'act-detail', actId: 3 });
    env.setFetchHandler(() => ({ html: '<p></p>' }));

    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 99));
    env.source.emitEvent('comment.created', commentEvent(5, 99));
    env.clock.advance(300);
    await flush();

    assert.equal(env.fetchCalls.length, 0);
});

test('a burst of events collapses into one fragment request', async () => {
    const env = load({ page: 'tasks' });
    env.setFetchHandler(() => ({ results_html: '<p></p>', tab: 'my', task_ids: [] }));

    env.source.emitEvent('task.created', taskEvent('task.created', 1, 3, 't1'));
    env.source.emitEvent('task.updated', taskEvent('task.updated', 2, 3, 't2'));
    env.source.emitEvent('task.completed', taskEvent('task.completed', 3, 3, 't3'));
    env.clock.advance(300);
    await flush();

    assert.equal(env.callsTo('/tasks/list-fragment/').length, 1);
});

test('a stale response never overwrites newer markup', async () => {
    const env = load({ page: 'tasks' });
    env.setFetchHandler(() => 'manual');

    env.source.emitEvent('task.created', taskEvent('task.created', 1, 3, 'first'));
    env.clock.advance(300);
    await flush();
    env.source.emitEvent('task.created', taskEvent('task.created', 2, 3, 'second'));
    env.clock.advance(300);
    await flush();

    const calls = env.callsTo('/tasks/list-fragment/');
    assert.equal(calls.length, 2);
    calls[1].resolve({ ok: true, status: 200, json: async () => ({ results_html: '<p data-new></p>' }) });
    await flush();
    calls[0].resolve({ ok: true, status: 200, json: async () => ({ results_html: '<p data-old></p>' }) });
    await flush();

    assert.ok(env.live.taskList.querySelector('[data-new]'));
    assert.equal(env.live.taskList.querySelector('[data-old]'), null);
});

test('a failed fragment request leaves the current markup intact', async () => {
    const env = load({ page: 'acts' });
    env.setFetchHandler((call) => {
        call.reject(new Error('network down'));
        return 'manual';
    });

    env.source.emitEvent('act.updated', actEvent('act.updated', 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.actKpis.textContent, 'исходные KPI');
    assert.equal(env.live.actResults.textContent, 'исходные акты');
});

// ------------------------------------------------------------- dirty forms

test('a dirty work form is never replaced and raises a conflict banner', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler(() => ({ html: '<section data-work></section>', status_code: 'KO_REVIEW' }));

    env.live.textarea.value = 'мой незаконченный комментарий';
    env.document.dispatch('input', { target: env.live.textarea });
    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.conflictBanner.hidden, false);
    assert.equal(env.live.textarea.value, 'мой незаконченный комментарий', 'typed text survives');
    assert.equal(env.live.work.textContent, 'исходная работа', 'the work tab is left alone');
    assert.equal(env.live.workflowButton.disabled, true, 'stale workflow submits are disabled');
});

test('act.updated on a dirty form warns without disabling workflow buttons', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler(() => ({ html: '<span data-badge></span>' }));

    env.document.dispatch('change', { target: env.live.textarea });
    env.source.emitEvent('act.updated', actEvent('act.updated', 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.conflictBanner.hidden, false);
    assert.equal(env.live.workflowButton.disabled, false);
});

test('a programmatic refresh never marks the form dirty', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : { html: '<section data-work></section>', status_code: 'KO_REVIEW' },
    );

    env.source.emit('open');
    env.clock.advance(300);
    await flush();
    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 3, 'later'));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.conflictBanner.hidden, true);
    assert.equal(env.live.workflowButton.disabled, false);
    assert.ok(env.live.work.querySelector('[data-work]'), 'a clean form is refreshed');
});

test('losing access stops updates and shows the access banner', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler((call) => (call.url.startsWith('/acts/') ? { status: 404 } : snapshot()));

    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.accessBanner.hidden, false);
    assert.equal(env.live.workflowButton.disabled, true);
    const calls = env.callsTo('/acts/3/comments-fragment/').length;

    env.source.emitEvent('comment.created', commentEvent(5, 3));
    env.clock.advance(500);
    await flush();

    assert.equal(env.callsTo('/acts/3/comments-fragment/').length, calls);
});

// -------------------------------------------------------------- RT-3 / RT-4

test('notification.created still shows one toast from server markup', async () => {
    const env = load();
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : fragment([{ id: 11, title: 'Акт передан в КО', message: 'Требуется решение', url: '/acts/3/' }]),
    );

    env.source.emitEvent('notification.created', createdEvent(11));
    env.clock.advance(300);
    await flush();

    assert.equal(env.toasts.length, 1);
    assert.equal(env.toasts[0].querySelector('.toast__title').textContent, 'Акт передан в КО');
    assert.equal(env.toasts[0].querySelector('.toast__link').getAttribute('href'), '/acts/3/');
});

test('the toast never uses text from the SSE payload', async () => {
    const env = load();
    const payload = createdEvent(11);
    payload.data.title = 'ПОДДЕЛЬНЫЙ';
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : fragment([{ id: 11, title: 'Настоящий', message: 'Из Django', url: '/acts/3/' }]),
    );

    env.source.emitEvent('notification.created', payload);
    env.clock.advance(300);
    await flush();

    assert.equal(env.toasts[0].querySelector('.toast__title').textContent, 'Настоящий');
    assert.ok(!env.toasts[0].textContent.includes('ПОДДЕЛЬНЫЙ'));
});

test('notification.read still synchronises without a toast', async () => {
    const env = load();
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/') ? snapshot() : fragment([], 0),
    );

    env.source.emitEvent('notification.read', readEvent('rt3-read'));
    env.clock.advance(300);
    await flush();

    assert.equal(env.menu.unreadCount, 0);
    assert.equal(env.toasts.length, 0);
});

test('a redelivered notification does not produce a second toast', async () => {
    const env = load();
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : fragment([{ id: 11, title: 'Раз', message: 'Текст', url: '/acts/3/' }]),
    );

    env.source.emitEvent('notification.created', createdEvent(11, 'same'));
    env.clock.advance(300);
    await flush();
    env.source.emitEvent('notification.created', createdEvent(11, 'same'));
    env.clock.advance(300);
    await flush();

    assert.equal(env.toasts.length, 1);
});

test('a toast closes on its button and on Escape', async () => {
    const env = load();
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : fragment([{ id: 11, title: 'Закрыть', message: 'Текст', url: '/acts/3/' }]),
    );

    env.source.emitEvent('notification.created', createdEvent(11, 'close-1'));
    env.clock.advance(300);
    await flush();
    env.toasts[0].querySelector('.toast__close').dispatch('click');
    assert.equal(env.toasts.length, 0);

    env.source.emitEvent('notification.created', createdEvent(12, 'close-2'));
    env.clock.advance(300);
    await flush();
    env.toasts[0].dispatch('keydown', { key: 'Escape' });
    assert.equal(env.toasts.length, 0);
});

// --------------------------------------------------------------------------

(async () => {
    let failures = 0;
    for (const [name, fn] of tests) {
        try {
            await fn();
            process.stdout.write(`  ok   ${name}\n`);
        } catch (error) {
            failures += 1;
            process.stdout.write(`  FAIL ${name}\n         ${error.message}\n`);
        }
    }
    process.stdout.write(`\n${tests.length - failures}/${tests.length} passed\n`);
    process.exit(failures ? 1 : 0);
})();
