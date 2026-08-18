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
const MODULES = ['core.js', 'tabs.js', 'sync.js', 'notifications.js', 'tasks.js', 'acts.js', 'workup.js', 'start.js'];
const SOURCES = MODULES.map((name) => [name, fs.readFileSync(path.join(CLIENT_DIR, name), 'utf8')]);
const DEFAULT_COORDINATION_EPOCH = 'test-session-epoch-000000000001';
const coordinationChannelName = (epoch = DEFAULT_COORDINATION_EPOCH) =>
    `quality-realtime-v1:${epoch}`;
const coordinationLeaseKey = (epoch = DEFAULT_COORDINATION_EPOCH) =>
    `${coordinationChannelName(epoch)}:leader`;
const coordinationMessage = (message, epoch = DEFAULT_COORDINATION_EPOCH) => ({
    ...message,
    schema_version: 1,
    coordination_epoch: epoch,
});
const coordinationLease = (tabId, expiresAt, epoch = DEFAULT_COORDINATION_EPOCH) =>
    JSON.stringify(coordinationMessage({ tab_id: tabId, expires_at: expiresAt }, epoch));

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

test('different session epochs neither share leases nor accept cross-session messages', async () => {
    FakeBroadcastChannel.bus = new Bus();
    FakeEventSource.instances = [];
    const storage = new FakeStorage();
    const oldEpoch = 'old-session-epoch-00000000000001';
    const newEpoch = 'new-session-epoch-00000000000002';

    load({ storage, coordinationEpoch: oldEpoch, resetSources: false });
    storage.setItem(
        coordinationLeaseKey(newEpoch),
        coordinationLease('old-session-tab', Date.now() + 60000, oldEpoch),
    );
    const newSession = load({ storage, coordinationEpoch: newEpoch, resetSources: false });

    assert.equal(newSession.core.tabs.isLeader, true, 'a foreign-epoch lease is ignored');
    assert.ok(storage.getItem(coordinationLeaseKey(oldEpoch)));
    assert.ok(storage.getItem(coordinationLeaseKey(newEpoch)));
    assert.equal(FakeEventSource.instances.length, 2, 'each authenticated session owns its stream');

    const crossSessionSender = new FakeBroadcastChannel(coordinationChannelName(newEpoch));
    crossSessionSender.postMessage(
        coordinationMessage({ kind: 'leader.state', state: 'offline' }, oldEpoch),
    );
    await flush();

    assert.notEqual(newSession.core.state, 'offline', 'a cross-session message is rejected');
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
        coordinationLeaseKey(),
        coordinationLease('gone', Date.now() - 1000),
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

// ------------------------------------------------- bell, tasks and act blocks

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

// ------------------------------------------- expired session and safety-sync

test('a redirected fragment response is never parsed as JSON and stops the client', async () => {
    const env = load({ page: 'tasks' });
    env.setFetchHandler(() => ({ redirected: true }));

    env.source.emitEvent('task.created', taskEvent('task.created', 9, 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.core.state, 'stopped');
    assert.equal(env.live.taskList.textContent, 'исходный список задач', 'DOM left untouched');
});

test('an unexpected Content-Type is never parsed as JSON, leaves the DOM untouched and does not stop the client', async () => {
    const env = load({ page: 'tasks' });
    env.setFetchHandler(() => ({ contentType: 'text/html' }));

    env.source.emitEvent('task.created', taskEvent('task.created', 9, 3));
    env.clock.advance(300);
    await flush();

    assert.notEqual(env.core.state, 'stopped');
    assert.equal(env.live.taskList.textContent, 'исходный список задач');
});

test('a 401 from any fragment coordinator stops the whole client', async () => {
    const env = load();
    env.setFetchHandler(() => ({ status: 401 }));

    env.source.emitEvent('notification.created', createdEvent(11));
    env.clock.advance(300);
    await flush();

    assert.equal(env.core.state, 'stopped');
});

test('an act fragment 401 stops the whole client, not just that act', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler((call) => (call.url.startsWith('/acts/') ? { status: 401 } : snapshot()));

    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.core.state, 'stopped');
});

test('an act fragment 404 still stops only that act, leaving the client running', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler((call) => (call.url.startsWith('/acts/') ? { status: 404 } : snapshot()));

    env.source.emitEvent('act.status_changed', actEvent('act.status_changed', 3));
    env.clock.advance(300);
    await flush();

    assert.notEqual(env.core.state, 'stopped');
    assert.equal(env.live.accessBanner.hidden, false);
});

// -- live safety-sync -------------------------------------------------------

test('the live safety-sync fires on the configured interval while live', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());
    env.source.emit('open');
    await flush();
    assert.equal(env.core.state, 'live');
    const afterOpen = env.callsTo('/realtime/sync/').length;

    env.clock.advance(299 * 1000);
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, afterOpen, 'not due yet');

    env.clock.advance(2000);
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, afterOpen + 1, 'fires once the interval elapses');
});

test('the live safety-sync never runs twice within one interval', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());
    env.source.emit('open');
    await flush();
    const afterOpen = env.callsTo('/realtime/sync/').length;

    env.clock.advance(300 * 1000);
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, afterOpen + 1);

    env.clock.advance(100 * 1000);
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, afterOpen + 1, 'still within the same interval');
});

test('the live safety-sync never runs while a sync is already in flight', async () => {
    const env = load();
    env.setFetchHandler(() => 'manual');
    env.source.emit('open');
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, 1, 'the ordinary open-sync is pending and never resolves');

    env.clock.advance(300 * 1000);
    await flush();

    assert.equal(env.callsTo('/realtime/sync/').length, 1, 'no second request while the first is still in flight');
});

test('degraded->live turns off fallback polling and arms the safety timer instead', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());

    env.clock.advance(21000);
    await flush();
    assert.equal(env.core.state, 'degraded');
    assert.ok(env.core.sync.isPolling);

    env.source.emit('open');
    await flush();
    assert.equal(env.core.state, 'live');
    assert.equal(env.core.sync.isPolling, false);

    const callsAfterOpen = env.callsTo('/realtime/sync/').length;
    env.clock.advance(300 * 1000);
    await flush();
    assert.ok(env.callsTo('/realtime/sync/').length > callsAfterOpen, 'the safety timer is now armed and fires');
});

test('live->degraded clears the safety timer and resumes fallback polling', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());
    env.source.emit('open');
    await flush();
    assert.equal(env.core.state, 'live');

    env.source.emit('error');
    env.clock.advance(21000);
    await flush();
    assert.equal(env.core.state, 'degraded');
    assert.ok(env.core.sync.isPolling, 'fallback polling resumed');
});

test('a live safety-sync runs after visible only when the previous sync is stale', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());
    env.source.emit('open');
    await flush();
    const afterOpen = env.callsTo('/realtime/sync/').length;

    env.document.visibilityState = 'visible';
    env.document.dispatch('visibilitychange');
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, afterOpen, 'a fresh sync is not stale yet');

    env.clock.advance(300 * 1000 + 1000);
    env.document.dispatch('visibilitychange');
    await flush();
    assert.equal(env.callsTo('/realtime/sync/').length, afterOpen + 1, 'a stale sync triggers one on becoming visible');
});

test('live safety-sync only runs on the leader tab, not a coordinated follower', async () => {
    // The follower joins only after the leader already has a cached snapshot,
    // so its startup handshake resolves immediately and leaves no pending
    // fallback timer that could confuse the later large clock advance below.
    const [leader] = loadTabs(1);
    leader.setFetchHandler(() => snapshot());
    FakeEventSource.instances[0].emit('open');
    leader.clock.advance(300);
    await flush();
    assert.equal(leader.core.state, 'live');

    const follower = load({ storage: leader.window.localStorage, resetSources: false });
    follower.setFetchHandler(() => snapshot());
    await flush();

    const leaderBefore = leader.callsTo('/realtime/sync/').length;
    const followerBefore = follower.callsTo('/realtime/sync/').length;

    leader.clock.advance(300 * 1000);
    follower.clock.advance(300 * 1000);
    await flush();

    assert.ok(leader.callsTo('/realtime/sync/').length > leaderBefore, 'the leader ran its safety-sync');
    assert.equal(follower.callsTo('/realtime/sync/').length, followerBefore, 'the follower must not run its own');
});

test('demoting a leader clears its safety timer', async () => {
    const [leader, follower] = loadTabs(2);
    leader.setFetchHandler(() => snapshot());
    FakeEventSource.instances[0].emit('open');
    leader.clock.advance(300);
    await flush();
    assert.equal(leader.core.state, 'live');

    // Somebody else grabs the lease: the leader steps down on its next tick.
    follower.window.localStorage.setItem(
        coordinationLeaseKey(),
        coordinationLease('somebody-else', Date.now() + 60000),
    );
    leader.core.tabs.renewOrElect();
    assert.equal(leader.core.tabs.isLeader, false);

    const before = leader.callsTo('/realtime/sync/').length;
    leader.clock.advance(300 * 1000);
    await flush();
    assert.equal(leader.callsTo('/realtime/sync/').length, before, 'a demoted tab never ticks its old timer again');
});

// -- follower handshake ------------------------------------------------------

test('a new follower requests a snapshot and applies the leader-answered response', async () => {
    const [leader] = loadTabs(1);
    leader.setFetchHandler(() => snapshot({ notifications: 'n-existing' }));
    FakeEventSource.instances[0].emit('open');
    leader.clock.advance(300);
    await flush();
    assert.ok(leader.core.sync.lastSnapshot, 'the leader now has a cached snapshot to answer with');

    const follower = load({ storage: leader.window.localStorage, resetSources: false });
    await flush();

    assert.equal(
        follower.core.sync.revisions && follower.core.sync.revisions.notifications,
        'n-existing',
        'the handshake response was applied without a request of its own',
    );
    assert.equal(follower.callsTo('/realtime/sync/').length, 0, 'no fallback fetch was needed');
    assert.equal(follower.sources.length, 1, 'still just the leader\'s stream — the follower opened none of its own');
});

test('the leader answers a sync.request individually for every requesting tab', async () => {
    const [leader] = loadTabs(1);
    leader.setFetchHandler(() => snapshot({ notifications: 'n-shared' }));
    FakeEventSource.instances[0].emit('open');
    leader.clock.advance(300);
    await flush();

    const followerA = load({ storage: leader.window.localStorage, resetSources: false });
    const followerB = load({ storage: leader.window.localStorage, resetSources: false });
    await flush();

    assert.equal(followerA.core.sync.revisions.notifications, 'n-shared');
    assert.equal(followerB.core.sync.revisions.notifications, 'n-shared');
});

test('a mistargeted or malformed sync.response is ignored', async () => {
    const [, follower] = loadTabs(2);
    follower.setFetchHandler(() => snapshot({ notifications: 'own-sync' }));

    const spy = new FakeBroadcastChannel(coordinationChannelName());
    // Wrong target_tab_id.
    spy.postMessage(coordinationMessage({
        kind: 'sync.response',
        request_id: 'does-not-matter',
        target_tab_id: 'somebody-else',
        snapshot: snapshot(),
    }));
    // Wrong request_id, and an unknown transport field inside the snapshot.
    spy.postMessage(coordinationMessage({
        kind: 'sync.response',
        request_id: 'wrong-id',
        target_tab_id: follower.core.tabs.tabId,
        snapshot: { ...snapshot(), extra_field: 'nope' },
    }));
    await flush();

    assert.equal(follower.core.sync.revisions, null, 'neither forged message was applied');
});

test('no leader response within the timeout triggers exactly one sync of its own', async () => {
    const [, follower] = loadTabs(2);
    // The leader never syncs, so it can never answer the follower's request.
    follower.setFetchHandler(() => snapshot({ notifications: 'own' }));

    assert.equal(follower.callsTo('/realtime/sync/').length, 0);
    follower.clock.advance(1500);
    await flush();

    assert.equal(follower.callsTo('/realtime/sync/').length, 1, 'exactly one fallback sync');
    assert.equal(follower.sources.length, 1, 'still just the leader\'s stream — the follower opened none of its own');
    assert.equal(follower.core.sync.isPolling, false, 'no persistent polling was started either');

    follower.clock.advance(120000);
    await flush();
    assert.equal(follower.callsTo('/realtime/sync/').length, 1, 'still just the one fallback sync, not repeated polling');
});

test('leader.state mirrors the connection state onto a follower without opening a stream', async () => {
    const [leader, follower] = loadTabs(2);
    leader.setFetchHandler(() => snapshot());
    follower.setFetchHandler(() => snapshot());

    assert.notEqual(follower.core.state, 'live');
    FakeEventSource.instances[0].emit('open');
    leader.clock.advance(300);
    follower.clock.advance(300);
    await flush();

    assert.equal(leader.core.state, 'live');
    assert.equal(follower.core.state, 'live', 'the leader.state broadcast mirrored live onto the follower');
    assert.equal(FakeEventSource.instances.length, 1, 'the follower never opened a stream of its own');
});

test('a leader closing still lets another tab take over and become live', async () => {
    const [leader, follower] = loadTabs(2);
    follower.setFetchHandler(() => snapshot());
    leader.core.stop();

    follower.window.localStorage.setItem(
        coordinationLeaseKey(),
        coordinationLease('gone', Date.now() - 1000),
    );
    follower.core.tabs.renewOrElect();
    follower.clock.advance(500);
    await flush();

    assert.equal(follower.core.tabs.isLeader, true);
    assert.equal(FakeEventSource.instances.length, 2, 'the new leader opened its own stream');

    FakeEventSource.instances[1].emit('open');
    follower.clock.advance(300);
    await flush();

    assert.equal(follower.core.state, 'live');
    assert.ok(follower.callsTo('/realtime/sync/').length >= 1, 'promotion led to a sync once the fresh stream opened');
});

// -------------------------------------------------------- recovery ownership

test('a degraded leader polls, and its follower shows the indicator without polling', async () => {
    const [leader, follower] = loadTabs(2);
    leader.setFetchHandler(() => snapshot());
    follower.setFetchHandler(() => snapshot());

    // The leader never connects, so it degrades and takes over recovery.
    leader.clock.advance(21000);
    follower.clock.advance(21000);
    await flush();

    assert.equal(leader.core.state, 'degraded');
    assert.equal(leader.core.sync.isPolling, true, 'the recovery owner polls');
    assert.equal(follower.core.state, 'degraded', 'the follower mirrors the state for its indicator');
    assert.equal(follower.core.sync.isPolling, false, 'but must never start its own poll loop');

    const followerBefore = follower.callsTo('/realtime/sync/').length;
    follower.clock.advance(180000);
    await flush();
    assert.equal(
        follower.callsTo('/realtime/sync/').length,
        followerBefore,
        'a follower issues no periodic recovery requests at all',
    );
});

test('ownsRecovery is true for a leader and for a standalone tab, false for a follower', async () => {
    const [leader, follower] = loadTabs(2);
    assert.equal(leader.core.sync.ownsRecovery, true);
    assert.equal(follower.core.sync.ownsRecovery, false);

    FakeBroadcastChannel.bus = new Bus();
    FakeEventSource.instances = [];
    const standalone = load({ broadcast: false, resetSources: false });
    assert.equal(standalone.core.tabs.isCoordinated, false);
    assert.equal(standalone.core.sync.ownsRecovery, true, 'an uncoordinated tab recovers for itself');
});

test('a promoted tab takes over polling from the leader that went away', async () => {
    const [leader, follower] = loadTabs(2);
    leader.setFetchHandler(() => snapshot());
    follower.setFetchHandler(() => snapshot());

    leader.clock.advance(21000);
    follower.clock.advance(21000);
    await flush();
    assert.equal(follower.core.sync.isPolling, false);

    // The leader disappears and its lease expires.
    leader.core.stop();
    follower.window.localStorage.setItem(
        coordinationLeaseKey(),
        coordinationLease('gone', Date.now() - 1000),
    );
    follower.core.tabs.renewOrElect();
    follower.clock.advance(500);
    await flush();

    assert.equal(follower.core.tabs.isLeader, true, 'the follower was promoted');
    assert.equal(follower.core.sync.ownsRecovery, true, 'and now owns recovery');
    // Promotion opens a fresh EventSource, so the new leader starts in
    // `connecting` rather than inheriting `degraded`. If that stream also
    // fails to deliver, the ordinary degrade timeout hands it the polling.
    assert.equal(follower.core.state, 'connecting');

    follower.clock.advance(21000);
    await flush();
    assert.equal(follower.core.state, 'degraded');
    assert.equal(follower.core.sync.isPolling, true, 'the new leader took over recovery polling');
});

test('a demoted leader stops polling as well as its safety timer', async () => {
    const [leader, follower] = loadTabs(2);
    leader.setFetchHandler(() => snapshot());
    leader.clock.advance(21000);
    await flush();
    assert.equal(leader.core.sync.isPolling, true);

    follower.window.localStorage.setItem(
        coordinationLeaseKey(),
        coordinationLease('somebody-else', Date.now() + 60000),
    );
    leader.core.tabs.renewOrElect();

    assert.equal(leader.core.tabs.isLeader, false);
    assert.equal(leader.core.sync.isPolling, false, 'a demoted tab must hand recovery back');
});

test('stopping releases every timer, request and channel', async () => {
    const [leader, follower] = loadTabs(2);
    leader.setFetchHandler(() => snapshot());
    follower.setFetchHandler(() => snapshot());
    FakeEventSource.instances[0].emit('open');
    leader.clock.advance(300);
    await flush();

    const source = FakeEventSource.instances[0];
    leader.core.stop();

    assert.equal(leader.core.state, 'stopped');
    assert.equal(source.closed, true, 'the EventSource is closed');
    assert.equal(leader.core.sync.isPolling, false, 'polling is stopped');

    // Nothing may tick afterwards, however far the clock is advanced.
    const calls = leader.fetchCalls.length;
    leader.clock.advance(600 * 1000);
    await flush();
    assert.equal(leader.fetchCalls.length, calls, 'no timer survived the stop');

    // And the leader must not keep broadcasting onto the shared channel.
    const followerStateBefore = follower.core.state;
    leader.core.setState('live');
    await flush();
    assert.equal(follower.core.state, followerStateBefore, 'a stopped tab broadcasts nothing');
});

test('re-running the client scripts never doubles timers or listeners', async () => {
    const env = load();
    env.setFetchHandler(() => snapshot());
    env.source.emit('open');
    await flush();
    const afterOpen = env.callsTo('/realtime/sync/').length;

    // A second include of every module, exactly as a duplicated script tag.
    SOURCES.forEach(([name, source]) => vm.runInContext(source, env.context, { filename: name }));
    env.clock.advance(400);
    await flush();

    assert.equal(env.sources.length, 1, 'still exactly one EventSource');
    env.clock.advance(300 * 1000);
    await flush();
    assert.equal(
        env.callsTo('/realtime/sync/').length,
        afterOpen + 1,
        'one safety-sync fired, not two',
    );
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
