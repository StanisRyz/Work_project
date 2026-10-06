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
    Element,
    FakeBroadcastChannel,
    FakeStorage,
    FakeEventSource,
    Bus,
    flush,
} = require('./dom_harness');

const CLIENT_DIR = path.join(__dirname, '..', '..', '..', 'static', 'js', 'realtime');
// The very order base.html loads them in.
const MODULES = ['core.js', 'tabs.js', 'sync.js', 'notifications.js', 'tasks.js', 'acts.js', 'workup.js', 'protocols.js', 'boards.js', 'start.js'];
const SOURCES = MODULES.map((name) => [name, fs.readFileSync(path.join(CLIENT_DIR, name), 'utf8')]);
// Dragging is not a real-time module, but what it does after moving the open
// card depends on whether the live board client runs.
const BOARD_DND_SOURCE = fs.readFileSync(path.join(CLIENT_DIR, '..', 'board_dnd.js'), 'utf8');
// The card drawer opens, switches and closes without a reload, and the live
// client has to follow it.
const BOARD_DRAWER_SOURCE = fs.readFileSync(path.join(CLIENT_DIR, '..', 'board_drawer.js'), 'utf8');
// The checklist's tick through `fetch`, and «@» in «Чат».
const BOARD_CHECKLIST_SOURCE = fs.readFileSync(path.join(CLIENT_DIR, '..', 'board_checklist.js'), 'utf8');
const BOARD_MENTIONS_SOURCE = fs.readFileSync(path.join(CLIENT_DIR, '..', 'board_mentions.js'), 'utf8');
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
        URL,
        URLSearchParams,
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

test('a reconnect that finds the work tab unchanged never warns a dirty form', async () => {
    // The stream ends by itself every REALTIME_MAX_CONNECTION_SECONDS; the
    // reconnect refetches the work tab. Same fingerprint, same server state:
    // no banner, no disabled buttons, nothing replaced.
    const env = load({ page: 'act-detail' });
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot({ acts: `a-${call.url}` })
            : { html: '<section data-work></section>', revision: 'work-rev-initial', status_code: 'KO_REVIEW' },
    );

    env.live.textarea.value = 'анализ, который заполняют уже двадцать минут';
    env.document.dispatch('input', { target: env.live.textarea });
    env.source.emit('open');
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.conflictBanner.hidden, true, 'no false conflict');
    assert.equal(env.live.workflowButton.disabled, false);
    assert.equal(env.live.work.textContent, 'исходная работа');
    assert.equal(env.live.textarea.value, 'анализ, который заполняют уже двадцать минут');
});

test('a changed work tab warns a dirty form but keeps its buttons while the status stays', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler(() => ({ html: '<section data-work></section>', revision: 'work-rev-2', status_code: 'KO_REVIEW' }));

    env.document.dispatch('input', { target: env.live.textarea });
    env.source.emitEvent('act.updated', actEvent('act.updated', 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.conflictBanner.hidden, false);
    assert.equal(env.live.workflowButton.disabled, false, 'the status did not move');
    assert.equal(env.live.work.textContent, 'исходная работа');
});

test('a page re-rendered after a rejected submission is never replaced by a refresh', async () => {
    // The bound form holds what the user typed and the errors; the fragment is
    // a clean render. Replacing one with the other is how the typed text used
    // to disappear a second after the page loaded.
    const env = load({ page: 'act-detail', workBound: true });
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/')
            ? snapshot()
            : { html: '<section data-work></section>', revision: 'work-rev-other', status_code: 'KO_REVIEW' },
    );

    env.source.emit('open');
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.work.textContent, 'исходная работа', 'the bound form stays');
    assert.equal(env.live.work.querySelector('[data-work]'), null);
});

test('typing outside the work tab does not hold the work tab back', async () => {
    const env = load({ page: 'act-detail' });
    env.setFetchHandler(() => ({ html: '<section data-work></section>', revision: 'work-rev-2', status_code: 'KO_REVIEW' }));

    env.live.modalTextarea.value = 'комментарий в модальном окне';
    env.document.dispatch('input', { target: env.live.modalTextarea });
    env.source.emitEvent('act.updated', actEvent('act.updated', 3));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.conflictBanner.hidden, true);
    assert.ok(env.live.work.querySelector('[data-work]'), 'a clean work tab is refreshed');
    assert.equal(env.core.actDetail.isDirty, false);
});

test('a clean work tab takes the new fingerprint, so the next identical refresh is a no-op', async () => {
    const env = load({ page: 'act-detail' });
    let html = '<section data-work></section>';
    env.setFetchHandler(() => ({ html, revision: 'work-rev-2', status_code: 'KO_REVIEW' }));

    env.source.emitEvent('act.updated', actEvent('act.updated', 3, 'first'));
    env.clock.advance(300);
    await flush();
    assert.ok(env.live.work.querySelector('[data-work]'));

    env.document.dispatch('input', { target: env.live.work.querySelector('[data-work]') });
    html = '<section data-other></section>';
    env.source.emitEvent('act.updated', actEvent('act.updated', 3, 'second'));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.conflictBanner.hidden, true, 'same fingerprint: no conflict');
    assert.ok(env.live.work.querySelector('[data-work]'), 'and nothing replaced');
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

// -------------------------------------------------------------------- boards

function boardEvent(boardId, change, eventId) {
    return {
        schema_version: 1,
        event_id: eventId || `board-${boardId}-${change}`,
        event_type: 'board.updated',
        occurred_at: '2026-10-05T10:00:00+00:00',
        resource_type: 'board',
        resource_id: boardId,
        data: { board_id: boardId, card_id: 9, change },
    };
}

function boardFragment({ columns = 'columns-rev-2', panel = 'panel-rev-2', comments, tabs, log, counts, checklist } = {}) {
    const payload = {
        columns_html: `<section data-column-id="31"><ol data-column-list><li data-card-id="9" data-task-id="21" data-fresh-tile>${columns}</li></ol></section>`,
        columns_revision: columns,
        panel_html: `<textarea name="execution_comment" data-fresh-panel></textarea>`,
        card_html: `<section data-board-tab-body="description" data-fresh-card>${panel}</section>`
            + '<section data-board-tab-body="files">файлы</section>',
        panel_revision: panel,
        panel: 'view',
    };
    if (log) {
        payload.log_html = `<ol><li data-fresh-log>${log}</li></ol>`;
        payload.log_revision = log;
    }
    if (counts) {
        payload.chat_count = counts.chat;
        payload.files_count = counts.files;
    }
    if (tabs) {
        payload.tabs_html = `<nav><a data-tab="8" data-fresh-tab>${tabs}</a></nav>`;
        payload.tabs_revision = tabs;
    }
    if (comments) {
        payload.comments_html = `<ol><li data-comment-id="2" data-fresh-comment>${comments}</li></ol>`;
        payload.comments_revision = comments;
    }
    if (checklist) {
        payload.checklist_html = `<ol><li data-checklist-item="1" data-fresh-checklist>${checklist}</li></ol>`;
        payload.checklist_revision = checklist;
    }
    return payload;
}

const boardCalls = (env) => env.fetchCalls.filter((call) => call.url.startsWith('/work/boards/4/7/fragment/'));

test('board.updated for this board refreshes the columns and a clean panel', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler((call) => (call.url.startsWith('/realtime/sync/') ? snapshot() : boardFragment()));

    env.source.emitEvent('board.updated', boardEvent(4, 'card_moved'));
    env.clock.advance(300);
    await flush();

    const calls = boardCalls(env);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/work/boards/4/7/fragment/?card=9', 'the server-built URL, as it is');
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'), 'columns replaced');
    assert.ok(env.live.panel.querySelector('[data-fresh-panel]'), 'a clean panel replaced');
    assert.equal(env.live.conflictBanner.hidden, true);
});

test('board.updated for another board fetches nothing', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment());

    env.source.emitEvent('board.updated', boardEvent(5, 'card_created'));
    env.clock.advance(300);
    await flush();

    assert.equal(boardCalls(env).length, 0);
});

test('the columns wait for the drag to end, then refresh once', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler((call) => (call.url.startsWith('/realtime/sync/') ? snapshot() : boardFragment()));

    env.live.board.setAttribute('data-board-busy', '');
    env.source.emitEvent('board.updated', boardEvent(4, 'card_created', 'first'));
    env.clock.advance(300);
    await flush();
    assert.equal(env.live.columns.textContent, 'исходная плиткавторая плитка', 'nothing replaced mid-drag');
    assert.equal(env.core.boardLive.isDeferred, true);

    env.live.board.attributes.delete('data-board-busy');
    env.document.dispatch('quality:board-idle');
    env.clock.advance(300);
    await flush();

    assert.equal(boardCalls(env).length, 2, 'fetched again after the drop, not applied stale');
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'));
    assert.equal(env.core.boardLive.isDeferred, false);

    env.document.dispatch('quality:board-idle');
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).length, 2, 'an idle with nothing deferred fetches nothing');
});

test('an open tab or column menu holds the structure back until it closes', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler((call) => (call.url.startsWith('/realtime/sync/') ? snapshot() : boardFragment()));
    env.live.columns.innerHTML = '<details data-board-menu open><form><input name="name"></form></details>'
        + '<section data-column-id="31"><ol data-column-list><li>исходная плитка</li></ol></section>';
    const menu = env.live.columns.querySelector('[data-board-menu]');

    env.source.emitEvent('board.updated', boardEvent(4, 'structure_changed', 'menu-open'));
    env.clock.advance(300);
    await flush();
    assert.ok(env.live.columns.querySelector('[data-board-menu]'), 'the open menu is not replaced');
    assert.equal(env.core.boardLive.isDeferred, true);

    // Another menu closing elsewhere, or this one still open, changes nothing.
    env.document.dispatch('toggle', { target: menu });
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).length, 1);

    menu.attributes.delete('open');
    env.document.dispatch('toggle', { target: menu });
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).length, 2, 'fetched again once the menu closed');
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'));
    assert.equal(env.core.boardLive.isDeferred, false);
});

test('the tabs are their own block: replaced when they moved, held while a tab menu is open', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler((call) => (call.url.startsWith('/realtime/sync/') ? snapshot() : boardFragment({ tabs: 'tabs-rev-2' })));

    env.source.emitEvent('board.updated', boardEvent(4, 'structure_changed', 'tabs-1'));
    env.clock.advance(300);
    await flush();
    assert.ok(env.live.tabs.querySelector('[data-fresh-tab]'), 'the tabs were replaced');
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'), 'and so were the columns');

    // A tab menu left open holds both read-only blocks back until it closes.
    env.live.tabs.innerHTML = '<nav><details data-board-menu open><input name="name"></details></nav>';
    const menu = env.live.tabs.querySelector('[data-board-menu]');
    env.setFetchHandler((call) => (call.url.startsWith('/realtime/sync/') ? snapshot() : boardFragment({ columns: 'columns-rev-3', tabs: 'tabs-rev-3' })));
    env.source.emitEvent('board.updated', boardEvent(4, 'structure_changed', 'tabs-2'));
    env.clock.advance(300);
    await flush();
    assert.ok(env.live.tabs.querySelector('[data-board-menu]'), 'the open menu stays');
    assert.equal(env.core.boardLive.isDeferred, true);

    menu.attributes.delete('open');
    env.document.dispatch('toggle', { target: menu });
    env.clock.advance(300);
    await flush();
    assert.ok(env.live.tabs.querySelector('[data-fresh-tab]'), 'replaced once the menu closed');
    assert.equal(env.core.boardLive.isDeferred, false);
});

test('typing in the panel keeps the text and raises the conflict banner', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment());

    env.live.execution.value = 'Сделано, акт приложен';
    env.document.dispatch('input', { target: env.live.execution });
    env.source.emitEvent('board.updated', boardEvent(4, 'card_updated'));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.panel.querySelector('[data-fresh-panel]'), null, 'the panel is kept');
    assert.equal(env.live.execution.value, 'Сделано, акт приложен');
    assert.equal(env.live.conflictBanner.hidden, false);
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'), 'the columns still refresh');
});

test('an unchanged panel fingerprint neither replaces nor warns', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment({ columns: 'columns-rev-initial', panel: 'panel-rev-initial' }));

    env.document.dispatch('input', { target: env.live.execution });
    env.source.emit('open');
    env.clock.advance(300);
    await flush();

    assert.equal(boardCalls(env).length, 1);
    assert.equal(env.live.conflictBanner.hidden, true, 'no false conflict');
    assert.equal(env.live.columns.textContent, 'исходная плиткавторая плитка', 'unchanged columns stay');
});

test('a page holding a refused form starts dirty', async () => {
    const env = load({ page: 'board', boardPanelHoldsInput: true });
    env.setFetchHandler(() => boardFragment());

    env.source.emitEvent('board.updated', boardEvent(4, 'card_updated'));
    env.clock.advance(300);
    await flush();

    assert.equal(env.live.panel.querySelector('[data-fresh-panel]'), null);
    assert.equal(env.live.conflictBanner.hidden, false);
});

test('typing outside the panel does not hold it back', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment());

    env.document.dispatch('input', { target: env.live.modalTextarea });
    env.source.emitEvent('board.updated', boardEvent(4, 'card_updated'));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.panel.querySelector('[data-fresh-panel]'));
    assert.equal(env.core.boardLive.isDirty, false);
});

test('a task event refreshes the board only for a task it shows', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment());

    env.source.emitEvent('task.completed', taskEvent('task.completed', 77, null, 'other'));
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).length, 0);

    env.source.emitEvent('task.completed', taskEvent('task.completed', 21, null, 'ours'));
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).length, 1);
});

test('the boards sync token moving refreshes the board', async () => {
    const env = load({ page: 'board' });
    let token = 'b1';
    env.setFetchHandler((call) =>
        call.url.startsWith('/realtime/sync/') ? snapshot({ boards: token }) : boardFragment(),
    );
    env.source.emit('open');
    env.clock.advance(300);
    await flush();
    const afterOpen = boardCalls(env).length;

    token = 'b2';
    env.core.sync.run();
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).length, afterOpen + 1);
});

test('a message on the open card replaces the list and leaves the dirty panel and the message form alone', async () => {
    const env = load({ page: 'board' });
    // Only the list and the tile's counter moved: the panel fingerprint did not.
    env.setFetchHandler(() =>
        boardFragment({ columns: 'columns-rev-2', panel: 'panel-rev-initial', comments: 'comments-rev-2' }),
    );

    env.live.execution.value = 'Половина результата';
    env.document.dispatch('input', { target: env.live.execution });
    env.live.commentText.value = 'Своё сообщение, ещё не отправлено';
    env.document.dispatch('input', { target: env.live.commentText });
    const boardEventPayload = boardEvent(4, 'comment_added');
    env.source.emitEvent('board.updated', boardEventPayload);
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.comments.querySelector('[data-fresh-comment]'), 'the list is replaced');
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'), 'the counter on the tile too');
    assert.equal(env.live.panel.querySelector('[data-fresh-panel]'), null, 'the panel is untouched');
    assert.equal(env.live.execution.value, 'Половина результата');
    assert.equal(env.live.commentText.value, 'Своё сообщение, ещё не отправлено');
    assert.equal(env.live.conflictBanner.hidden, true, 'a message is no conflict');
});

test('a message on another card refreshes only the columns', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() =>
        boardFragment({ columns: 'columns-rev-2', panel: 'panel-rev-initial', comments: 'comments-rev-initial' }),
    );

    env.source.emitEvent('board.updated', { ...boardEvent(4, 'comment_added', 'other-card'), data: { board_id: 4, card_id: 77, change: 'comment_added' } });
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'));
    assert.equal(env.live.comments.querySelector('[data-fresh-comment]'), null, 'the list stays');
    assert.equal(env.live.panel.querySelector('[data-fresh-panel]'), null);
});

test('a reader at the bottom of the list follows the new message, one above it stays', async () => {
    const env = load({ page: 'board' });
    let revision = 'comments-rev-2';
    env.setFetchHandler(() => boardFragment({ panel: 'panel-rev-initial', comments: revision }));
    const comments = env.live.comments;

    comments.scrollHeight = 500;
    comments.clientHeight = 100;
    comments.scrollTop = 400;
    env.source.emitEvent('board.updated', boardEvent(4, 'comment_added', 'at-bottom'));
    env.clock.advance(300);
    await flush();
    assert.equal(comments.scrollTop, 500, 'scrolled to the newest message');

    comments.scrollHeight = 700;
    comments.scrollTop = 120;
    revision = 'comments-rev-3';
    env.source.emitEvent('board.updated', boardEvent(4, 'comment_added', 'reading-above'));
    env.clock.advance(300);
    await flush();
    assert.ok(comments.querySelector('[data-fresh-comment]'));
    assert.equal(comments.scrollTop, 120, 'left where the reader was');
});

function loadDnd(env) {
    vm.runInContext(BOARD_DND_SOURCE, env.context, { filename: 'board_dnd.js' });
    return env.context.window.qualityBoardDnd;
}

test('moving the open card leaves the panel to the live client', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment());
    const dnd = loadDnd(env);

    assert.equal(dnd.openCardMoved('9'), 'live');
    assert.deepEqual(env.window.location.replaced, [], 'no navigation');
    const message = env.document.querySelector('[data-board-message]');
    assert.ok(!message || message.hidden !== false, 'no «обновите страницу»');
});

test('with the live client stopped, moving the open card loads the board address', async () => {
    const env = load({ page: 'board' });
    const dnd = loadDnd(env);
    env.core.stop();

    assert.equal(dnd.openCardMoved('9'), 'replace');
    assert.deepEqual(env.window.location.replaced, ['/work/boards/4/7/?card=9']);
});

test('without real-time, moving the open card loads the board address', async () => {
    const env = load({ page: 'board', realtimeEnabled: false });
    const dnd = loadDnd(env);

    assert.equal(dnd.openCardMoved('9'), 'replace');
    assert.deepEqual(env.window.location.replaced, ['/work/boards/4/7/?card=9']);
});

test('without real-time, unsaved input gets the message instead of a navigation', async () => {
    const env = load({ page: 'board', realtimeEnabled: false });
    env.window.qualityUnsavedGuard = { isDirty: true };
    const dnd = loadDnd(env);

    assert.equal(dnd.openCardMoved('9'), 'message');
    assert.deepEqual(env.window.location.replaced, []);
});

test('without real-time the board script does nothing', async () => {
    const env = load({ page: 'board', realtimeEnabled: false });
    assert.equal(env.core, null);
    assert.equal(env.fetchCalls.length, 0);
});

// ------------------------------------------------------------ board drawer

function loadDrawer(env) {
    vm.runInContext(BOARD_DRAWER_SOURCE, env.context, { filename: 'board_drawer.js' });
    return env.context.window.qualityBoardDrawer;
}

/** `boards:fragment` for card 12, the second tile, opened from the board. */
function drawerPayload(tab = 'description') {
    const query = `?card=12&tab=${tab}&mine=1`;
    const links = ['description', 'chat', 'files', 'log'].map((name) => (
        `<a class="board-drawer__tab${name === tab ? ' is-active' : ''}" `
        + `href="/work/boards/4/7/?card=12&tab=${name}&mine=1" data-board-tab-link="${name}" `
        + `data-board-tab-fragment-url="/work/boards/4/7/fragment/?card=12&tab=${name}&mine=1">${name}</a>`
    )).join('');
    return {
        ...boardFragment({
            columns: 'columns-rev-open', panel: 'panel-rev-open', comments: 'comments-rev-open', log: 'log-rev-open',
        }),
        panel: 'view',
        card_id: 12,
        task_id: 24,
        tab,
        page_url: `/work/boards/4/7/${query}`,
        fragment_url: `/work/boards/4/7/fragment/${query}`,
        reset_url: '/work/boards/4/7/?card=12',
        drawer_html: '<div class="board-drawer__frame">'
            + '<div data-live-board-panel data-task-id="24"><a href="/work/boards/4/7/?mine=1" data-board-drawer-close>×</a>'
            + '<textarea name="execution_comment" data-opened-panel></textarea></div>'
            + `<nav>${links}</nav>`
            + '<div data-live-board-card><section data-board-tab-body="description">описание 12</section>'
            + '<section data-board-tab-body="files">файлы 12</section></div>'
            + '<section data-board-tab-body="chat"><div data-live-board-comments><ol><li>сообщение 12</li></ol></div>'
            + '<textarea name="text" data-opened-chat-form></textarea></section>'
            + '<section data-board-tab-body="log"><div data-live-board-log><ol><li>запись 12</li></ol></div></section>'
            + '</div>',
    };
}

const tileOf = (env, cardId) => env.live.columns.querySelector(`[data-card-id="${cardId}"]`).querySelector('.board-tile');

/** A plain left click on `target`; returns whether the script took it over. */
function click(env, target) {
    let prevented = false;
    env.document.dispatch('click', {
        target, button: 0, defaultPrevented: false, preventDefault: () => { prevented = true; },
    });
    return prevented;
}

function drawerHandler(env, { tab } = {}) {
    env.setFetchHandler((call) => {
        if (call.url.startsWith('/realtime/sync/')) {
            return snapshot();
        }
        if (call.url.includes('card=12')) {
            return drawerPayload(tab || new URLSearchParams(call.url.split('?')[1]).get('tab') || 'description');
        }
        return boardFragment();
    });
}

test('a tile opens its card in the drawer through the fragment and pushes the address', async () => {
    const env = load({ page: 'board' });
    drawerHandler(env);
    loadDrawer(env);

    assert.equal(click(env, tileOf(env, 12)), true, 'the link is taken over, not followed');
    await flush();

    assert.equal(env.callsTo('/work/boards/4/7/fragment/')[0].url, '/work/boards/4/7/fragment/?card=12&mine=1',
        'the tile\'s own server-built query');
    assert.ok(env.live.drawer.querySelector('[data-opened-panel]'), 'drawer_html inserted');
    assert.equal(env.live.drawer.hidden, false);
    assert.equal(env.live.drawer.getAttribute('data-board-tab'), 'description');
    assert.deepEqual(env.window.history.pushed, ['/work/boards/4/7/?card=12&tab=description&mine=1']);
    assert.deepEqual(env.window.location.assigned, [], 'no navigation, no reload');
    assert.equal(env.live.board.dataset.boardFragmentUrl, '/work/boards/4/7/fragment/?card=12&tab=description&mine=1');
    assert.equal(env.live.board.dataset.boardPageUrl, '/work/boards/4/7/?card=12&tab=description&mine=1');
    assert.equal(env.live.board.dataset.panelRevision, 'panel-rev-open');
    assert.equal(env.live.columns.dataset.currentCard, '12');
    assert.ok(tileOf(env, 12).classList.contains('board-tile--open'));
    assert.ok(!tileOf(env, 9).classList.contains('board-tile--open'));

    // The live client follows: its next request is the new card's address.
    env.clock.advance(300);
    await flush();
    const calls = boardCalls(env);
    assert.equal(calls[calls.length - 1].url, '/work/boards/4/7/fragment/?card=12&tab=description&mine=1');
});

test('«назад» and «вперёд» close and open the drawer the same way', async () => {
    const env = load({ page: 'board' });
    drawerHandler(env);
    loadDrawer(env);

    env.window.location.search = '';
    env.window.dispatch('popstate');
    assert.equal(env.live.drawer.hidden, true, 'back to the board: closed');
    assert.equal(env.live.drawer.innerHTML, '');
    assert.ok(!env.live.layout.classList.contains('board-layout--with-panel'));
    assert.equal(env.live.board.dataset.boardFragmentUrl, '/work/boards/4/7/fragment/');
    assert.equal(env.live.columns.dataset.currentCard, '');
    assert.deepEqual(env.window.history.pushed, [], 'popstate pushes nothing');

    env.window.location.search = '?card=12&tab=chat';
    env.window.dispatch('popstate');
    await flush();
    assert.equal(env.callsTo('/work/boards/4/7/fragment/').pop().url, '/work/boards/4/7/fragment/?card=12&tab=chat');
    assert.equal(env.live.drawer.hidden, false);
    assert.equal(env.live.drawer.getAttribute('data-board-tab'), 'chat');
    assert.deepEqual(env.window.history.pushed, []);
});

test('«×» and Esc close a clean drawer in place; unsaved input makes them ordinary links', async () => {
    const env = load({ page: 'board' });
    drawerHandler(env);
    loadDrawer(env);

    const closer = env.live.panel.querySelector('[data-board-drawer-close]');
    env.window.qualityUnsavedGuard = { isDirty: true };
    assert.equal(click(env, closer), false, 'followed: the browser asks before leaving');
    assert.equal(env.live.drawer.hidden, false);

    env.window.qualityUnsavedGuard = { isDirty: false };
    assert.equal(click(env, closer), true);
    assert.equal(env.live.drawer.hidden, true);
    assert.deepEqual(env.window.history.pushed, ['/work/boards/4/7/']);

    // Esc, with a menu open above the drawer, belongs to the menu.
    click(env, tileOf(env, 12));
    await flush();
    const menu = new Element('details');
    menu.setAttribute('data-board-menu', '');
    menu.setAttribute('open', '');
    env.live.board.append(menu);
    env.document.dispatch('keydown', { key: 'Escape', target: env.document.body });
    assert.equal(env.live.drawer.hidden, false);
    menu.remove();
    env.document.dispatch('keydown', { key: 'Escape', target: env.live.commentText });
    assert.equal(env.live.drawer.hidden, false, 'Esc while typing is the field\'s');
    env.document.dispatch('keydown', { key: 'Escape', target: env.document.body });
    assert.equal(env.live.drawer.hidden, true);
});

test('with unsaved input a tile is an ordinary link: nothing fetched, nothing replaced', async () => {
    const env = load({ page: 'board' });
    drawerHandler(env);
    loadDrawer(env);

    env.live.execution.value = 'Результат, ещё не отправлен';
    env.document.dispatch('input', { target: env.live.execution });
    assert.equal(click(env, tileOf(env, 12)), false, 'the live client\'s own dirty flag counts');
    await flush();
    assert.equal(env.callsTo('/work/boards/4/7/fragment/').length, 0);
    assert.equal(env.live.execution.value, 'Результат, ещё не отправлен');
    assert.deepEqual(env.window.history.pushed, []);

    const clean = load({ page: 'board', realtimeEnabled: false });
    drawerHandler(clean);
    loadDrawer(clean);
    clean.window.qualityUnsavedGuard = { isDirty: true };
    assert.equal(click(clean, tileOf(clean, 12)), false, 'and so does the leave-page guard');
});

test('a tab switches in place, the address follows, and a live replacement keeps it', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler((call) => (call.url.startsWith('/realtime/sync/')
        ? snapshot()
        : boardFragment({ panel: 'panel-rev-2', comments: 'comments-rev-2', log: 'log-rev-2' })));
    loadDrawer(env);

    const chat = env.live.drawer.querySelector('[data-board-tab-link="chat"]');
    assert.equal(click(env, chat), true);
    assert.equal(env.live.drawer.getAttribute('data-board-tab'), 'chat');
    assert.ok(chat.classList.contains('is-active'));
    assert.ok(!env.live.drawer.querySelector('[data-board-tab-link="description"]').classList.contains('is-active'));
    assert.deepEqual(env.window.history.replaced, ['/work/boards/4/7/?card=9&tab=chat']);
    assert.equal(env.live.board.dataset.boardFragmentUrl, '/work/boards/4/7/fragment/?card=9&tab=chat');
    assert.equal(env.callsTo('/work/boards/4/7/fragment/').length, 0, 'switching fetches nothing');

    env.source.emitEvent('board.updated', boardEvent(4, 'card_updated'));
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).pop().url, '/work/boards/4/7/fragment/?card=9&tab=chat');
    assert.ok(env.live.panel.querySelector('[data-fresh-panel]'), 'the heading replaced');
    assert.ok(env.live.card.querySelector('[data-fresh-card]'), 'and «Описание»/«Файлы» with it');
    assert.ok(env.live.comments.querySelector('[data-fresh-comment]'));
    assert.ok(env.live.log.querySelector('[data-fresh-log]'));
    assert.equal(env.live.drawer.getAttribute('data-board-tab'), 'chat', 'the tab survived the replacement');
    assert.ok(chat.classList.contains('is-active'));
});

test('a message and a log entry replace their blocks and counters, never the typed message', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment({
        columns: 'columns-rev-initial', panel: 'panel-rev-initial', comments: 'comments-rev-2', log: 'log-rev-2',
        counts: { chat: 5, files: 3 },
    }));
    loadDrawer(env);

    env.live.execution.value = 'Половина результата';
    env.document.dispatch('input', { target: env.live.execution });
    env.live.commentText.value = 'Пишу ответ';
    env.document.dispatch('input', { target: env.live.commentText });
    env.source.emitEvent('board.updated', boardEvent(4, 'comment_added'));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.comments.querySelector('[data-fresh-comment]'), 'the chat replaced');
    assert.ok(env.live.log.querySelector('[data-fresh-log]'), 'the log replaced');
    assert.equal(env.live.board.querySelector('[data-board-tab-count="chat"]').textContent, '5');
    assert.equal(env.live.board.querySelector('[data-board-tab-count="files"]').textContent, '1',
        'files follow their own block, which did not move');
    assert.equal(env.live.commentText.value, 'Пишу ответ', 'the message being typed is never redrawn');
    assert.equal(env.live.execution.value, 'Половина результата');
    assert.equal(env.live.panel.querySelector('[data-fresh-panel]'), null);
    assert.equal(env.live.conflictBanner.hidden, true, 'a message or an entry is no conflict');
    assert.equal(env.live.board.dataset.logRevision, 'log-rev-2');
});

test('an answer for an address the page no longer shows is dropped and fetched again', async () => {
    const env = load({ page: 'board' });
    let manual = true;
    env.setFetchHandler((call) => {
        if (call.url.startsWith('/realtime/sync/')) {
            return snapshot();
        }
        if (manual) {
            manual = false;
            return 'manual';
        }
        return boardFragment({ panel: 'panel-rev-3' });
    });
    loadDrawer(env);

    env.source.emitEvent('board.updated', boardEvent(4, 'card_updated'));
    env.clock.advance(300);
    await flush();
    const stale = boardCalls(env)[0];
    click(env, env.live.drawer.querySelector('[data-board-tab-link="log"]'));
    stale.resolve({
        ok: true, status: 200, redirected: false, headers: { get: () => 'application/json' },
        json: async () => boardFragment({ panel: 'panel-rev-stale' }),
    });
    await flush();
    assert.equal(env.live.board.dataset.panelRevision, 'panel-rev-initial', 'the stale answer is not applied');
    env.clock.advance(300);
    await flush();
    assert.equal(boardCalls(env).pop().url, '/work/boards/4/7/fragment/?card=9&tab=log');
    assert.equal(env.live.board.dataset.panelRevision, 'panel-rev-3');
});

test('without real-time the drawer still opens, and a moved open card is drawn again in it', async () => {
    const env = load({ page: 'board', realtimeEnabled: false });
    drawerHandler(env);
    const drawer = loadDrawer(env);
    const dnd = loadDnd(env);

    assert.equal(click(env, tileOf(env, 12)), true);
    await flush();
    assert.ok(env.live.drawer.querySelector('[data-opened-panel]'));
    assert.equal(drawer.isOpen, true);

    assert.equal(dnd.openCardMoved('12'), 'drawer');
    await flush();
    assert.deepEqual(env.window.location.replaced, [], 'no navigation');
    assert.equal(env.callsTo('/work/boards/4/7/fragment/').pop().url, '/work/boards/4/7/fragment/?card=12&tab=description&mine=1');
});

test('another card opens on the tab the drawer shows; «Описание» adds nothing', async () => {
    const env = load({ page: 'board' });
    drawerHandler(env);
    loadDrawer(env);

    // Card 9 is open on «Описание»: the tile's own address, as before.
    click(env, tileOf(env, 12));
    await flush();
    assert.equal(env.callsTo('/work/boards/4/7/fragment/').pop().url, '/work/boards/4/7/fragment/?card=12&mine=1');

    // On «Лог», the next card opens on «Лог» too.
    click(env, env.live.drawer.querySelector('[data-board-tab-link="log"]'));
    assert.equal(env.live.drawer.getAttribute('data-board-tab'), 'log');
    click(env, tileOf(env, 9));
    await flush();
    const opened = env.callsTo('/work/boards/4/7/fragment/').pop().url;
    assert.equal(new URL(opened, 'http://quality.test').searchParams.get('tab'), 'log');
    assert.equal(new URL(opened, 'http://quality.test').searchParams.get('card'), '9');
});

test('«Карточка ZAP-9» copies the link to the card, no dialog, no navigation', async () => {
    const env = load({ page: 'board' });
    drawerHandler(env);
    loadDrawer(env);
    const written = [];
    env.context.navigator.clipboard = { writeText: (text) => { written.push(text); return Promise.resolve(); } };
    env.window.isSecureContext = true;
    const link = new Element('a');
    link.setAttribute('href', '/work/boards/4/?card=9');
    link.setAttribute('data-board-card-link', 'ZAP-9');
    env.live.panel.append(link);

    assert.equal(click(env, link), true, 'the click is the copy, not a navigation');
    await flush();
    assert.deepEqual(written, ['http://quality.test/work/boards/4/?card=9']);
    const message = env.live.board.querySelector('[data-board-message]');
    assert.equal(message.hidden, false);
    assert.equal(message.textContent, 'Ссылка на карточку ZAP-9 скопирована.');
    assert.deepEqual(env.window.location.assigned, []);
    assert.equal(env.live.drawer.hidden, false, 'the drawer stays open');
});

// ------------------------------------------------------ the card's «Чек-лист»

test('a tick by somebody else replaces the checklist, never the dirty panel or the field being typed', async () => {
    const env = load({ page: 'board' });
    // The checklist and the tile moved; the guarded panel did not.
    env.setFetchHandler(() => boardFragment({ panel: 'panel-rev-initial', checklist: 'checklist-rev-2' }));

    env.live.execution.value = 'Правка результата';
    env.document.dispatch('input', { target: env.live.execution });
    env.live.checklistText.value = 'Новый пункт, ещё не добавлен';
    env.source.emitEvent('board.updated', boardEvent(4, 'checklist_changed'));
    env.clock.advance(300);
    await flush();

    assert.ok(env.live.checklist.querySelector('[data-fresh-checklist]'), 'the list is replaced');
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'), 'and the tile with its «☑ 3/5»');
    assert.equal(env.live.panel.querySelector('[data-fresh-panel]'), null, 'the panel is untouched');
    assert.equal(env.live.execution.value, 'Правка результата');
    assert.equal(env.live.checklistText.value, 'Новый пункт, ещё не добавлен', 'the add field is in no block');
    assert.equal(env.live.conflictBanner.hidden, true, 'a tick is no conflict');
    assert.equal(env.live.board.dataset.checklistRevision, 'checklist-rev-2');
});

test('an unchanged checklist fingerprint replaces nothing', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment({ checklist: 'checklist-rev-initial' }));

    env.source.emitEvent('board.updated', boardEvent(4, 'card_moved'));
    env.clock.advance(300);
    await flush();
    assert.equal(env.live.checklist.querySelector('[data-fresh-checklist]'), null);
});

test('an item being renamed holds the checklist back until its form is gone', async () => {
    const env = load({ page: 'board' });
    env.setFetchHandler(() => boardFragment({ panel: 'panel-rev-initial', checklist: 'checklist-rev-2' }));
    env.live.checklist.innerHTML = '<ol><li data-checklist-item="1"><form data-checklist-edit><input name="text"></form></li></ol>';

    env.source.emitEvent('board.updated', boardEvent(4, 'checklist_changed', 'renaming'));
    env.clock.advance(300);
    await flush();
    assert.ok(env.live.checklist.querySelector('[data-checklist-edit]'), 'the open form is not replaced');
    assert.equal(env.core.boardLive.isDeferred, true);
    assert.ok(env.live.columns.querySelector('[data-fresh-tile]'), 'the columns still refresh');

    env.live.checklist.innerHTML = '<ol><li data-checklist-item="1">исходный пункт</li></ol>';
    env.document.dispatch('quality:board-idle');
    env.clock.advance(300);
    await flush();
    assert.ok(env.live.checklist.querySelector('[data-fresh-checklist]'), 'fetched again and replaced');
    assert.equal(env.core.boardLive.isDeferred, false);
});

function loadChecklist(env) {
    vm.runInContext(BOARD_CHECKLIST_SOURCE, env.context, { filename: 'board_checklist.js' });
    return env.context.window.qualityBoardChecklist;
}

/** One item of the block as `checklist.html` draws it, and card 9's tile. */
function checklistItem(env, { done = false } = {}) {
    env.live.checklist.innerHTML = '<div><h3>Чек-лист <span data-checklist-count>1/3</span></h3>'
        + `<ol><li class="board-checklist__item${done ? ' is-done' : ''}" data-checklist-item="5">`
        + '<form action="/work/boards/4/cards/9/checklist/5/toggle/?mine=1" method="post" data-checklist-toggle>'
        + '<input name="csrfmiddlewaretoken" value="token-1">'
        + `<input name="done" value="${done ? '0' : '1'}" data-checklist-done>`
        + `<button type="submit" aria-pressed="${done ? 'true' : 'false'}"></button>`
        + '</form><span>Металл</span></li></ol></div>';
    env.live.columns.setAttribute('data-current-card', '9');
    env.live.columns.innerHTML = '<section data-column-id="31"><ol data-column-list>'
        + '<li data-card-id="9" data-task-id="21"><a class="board-tile"><span data-tile-checklist>☑ 1/3</span></a></li>'
        + '</ol></section>';
    const form = env.live.checklist.querySelector('[data-checklist-toggle]');
    return { form, item: env.live.checklist.querySelector('[data-checklist-item]') };
}

function submit(env, form) {
    let prevented = false;
    env.document.dispatch('submit', { target: form, preventDefault: () => { prevented = true; } });
    return prevented;
}

test('a tick posts through fetch at once and takes the server\'s numbers', async () => {
    const env = load({ page: 'board' });
    loadChecklist(env);
    const { form, item } = checklistItem(env);
    let answer = null;
    env.setFetchHandler((call) => {
        if (call.url.includes('/checklist/')) {
            answer = call;
            return { ok: true, item_id: 5, is_done: true, done: 2, total: 3 };
        }
        return snapshot();
    });

    assert.equal(submit(env, form), true, 'no navigation');
    assert.equal(item.classList.contains('is-done'), true, 'ticked at once');
    await flush();
    assert.equal(answer.url, '/work/boards/4/cards/9/checklist/5/toggle/?mine=1', 'the form\'s own address');
    assert.equal(answer.options.method, 'POST');
    assert.equal(answer.options.headers['X-Requested-With'], 'fetch');
    assert.equal(answer.options.headers['X-CSRFToken'], 'token-1');
    assert.equal(answer.options.body, 'done=1');
    assert.equal(item.classList.contains('is-done'), true);
    assert.equal(form.querySelector('button').getAttribute('aria-pressed'), 'true');
    assert.equal(form.querySelector('[data-checklist-done]').value, '0', 'the next click takes it off');
    assert.equal(env.live.checklist.querySelector('[data-checklist-count]').textContent, '2/3');
    assert.equal(env.live.columns.querySelector('[data-tile-checklist]').textContent, '☑ 2/3');
});

test('a refused tick is put back and the message shown', async () => {
    const env = load({ page: 'board' });
    loadChecklist(env);
    const { form, item } = checklistItem(env, { done: true });
    env.setFetchHandler((call) => (call.url.includes('/checklist/') ? { status: 400 } : snapshot()));

    submit(env, form);
    assert.equal(item.classList.contains('is-done'), false, 'taken off at once');
    await flush();
    assert.equal(item.classList.contains('is-done'), true, 'and put back');
    assert.equal(form.querySelector('button').getAttribute('aria-pressed'), 'true');
    assert.equal(form.querySelector('[data-checklist-done]').value, '0');
    const message = env.live.board.querySelector('[data-board-message]');
    assert.equal(message.hidden, false);
    assert.equal(message.textContent, 'Не удалось отметить пункт. Попробуйте ещё раз.');
    assert.equal(env.live.checklist.querySelector('[data-checklist-count]').textContent, '1/3', 'counts untouched');
});

// ----------------------------------------------------------------- «@» in «Чат»

function loadMentions(env) {
    vm.runInContext(BOARD_MENTIONS_SOURCE, env.context, { filename: 'board_mentions.js' });
    return env.context.window.qualityBoardMentions;
}

/** The chat's form as `drawer.html` draws it, with two readers to mention. */
function mentionForm(env) {
    const form = new Element('form');
    form.setAttribute('data-board-mentions', '');
    const field = new Element('div');
    field.setAttribute('class', 'board-mentions__field');
    const textarea = new Element('textarea');
    textarea.setAttribute('name', 'text');
    textarea.setAttribute('data-mention-input', '');
    textarea.value = '';
    field.append(textarea);
    const fallback = new Element('details');
    fallback.setAttribute('data-mention-fallback', '');
    [['3', 'Ирина Петрова'], ['4', 'Игорь Смирнов']].forEach(([id, name]) => {
        const box = new Element('input');
        box.setAttribute('type', 'checkbox');
        box.setAttribute('name', 'mention');
        box.setAttribute('data-mention-name', name);
        box.value = id;
        box.checked = false;
        fallback.append(box);
    });
    form.append(field, fallback);
    env.live.drawer.append(form);
    return { form, textarea, fallback };
}

const typeIn = (env, textarea, value) => {
    textarea.value = value;
    env.document.dispatch('input', { target: textarea });
};

const press = (env, textarea, key) => {
    let prevented = false;
    env.document.dispatch('keydown', {
        target: textarea, key, preventDefault: () => { prevented = true; }, stopPropagation: () => {},
    });
    return prevented;
};

test('«@» opens the readers, narrows them, and Esc closes the list', async () => {
    const env = load({ page: 'board' });
    const { form, textarea, fallback } = mentionForm(env);
    const mentions = loadMentions(env);

    assert.equal(fallback.hidden, true, 'the checkboxes become the source, hidden');
    assert.ok(fallback.querySelectorAll('[data-mention-name]').every((box) => box.disabled), 'and post nothing');

    typeIn(env, textarea, 'Коллеги, ');
    assert.equal(form.querySelector('[data-mention-list]'), null, 'no «@», no list');

    typeIn(env, textarea, 'Коллеги, @');
    let options = form.querySelectorAll('[data-mention-option]');
    assert.deepEqual(options.map((option) => option.textContent), ['Ирина Петрова', 'Игорь Смирнов']);

    typeIn(env, textarea, 'Коллеги, @ири');
    options = form.querySelectorAll('[data-mention-option]');
    assert.deepEqual(options.map((option) => option.textContent), ['Ирина Петрова'], 'filtered by what is typed');

    typeIn(env, textarea, 'Коллеги, @кто-то');
    assert.equal(form.querySelector('[data-mention-list]'), null, 'nobody matches, nothing open');

    typeIn(env, textarea, 'Коллеги, @И');
    assert.equal(mentions.isOpen, true);
    assert.equal(press(env, textarea, 'Escape'), true);
    assert.equal(mentions.isOpen, false);
    assert.equal(form.querySelector('[data-mention-list]'), null);
    assert.equal(textarea.value, 'Коллеги, @И', 'the text stays as typed');
});

test('choosing writes the name and a hidden mention; a deleted name is not sent', async () => {
    const env = load({ page: 'board' });
    const { form, textarea } = mentionForm(env);
    loadMentions(env);

    typeIn(env, textarea, 'Посмотрите, @И');
    assert.equal(press(env, textarea, 'ArrowDown'), true);
    assert.equal(form.querySelector('.is-active').textContent, 'Игорь Смирнов');
    assert.equal(press(env, textarea, 'Enter'), true, 'Enter chooses, it adds no line');
    assert.equal(textarea.value, 'Посмотрите, @Игорь Смирнов ');
    let hidden = form.querySelectorAll('[data-mention-hidden]');
    assert.deepEqual(hidden.map((input) => [input.getAttribute('name'), input.getAttribute('value')]), [['mention', '4']]);
    assert.equal(form.querySelector('[data-mention-list]'), null);

    // A click chooses too, and the same person twice is one field.
    typeIn(env, textarea, 'Посмотрите, @Игорь Смирнов и @ир');
    const option = form.querySelector('[data-mention-option="3"]');
    env.document.dispatch('click', { target: option, preventDefault: () => {} });
    assert.equal(textarea.value, 'Посмотрите, @Игорь Смирнов и @Ирина Петрова ');
    typeIn(env, textarea, `${textarea.value}@Иг`);
    press(env, textarea, 'Tab');
    hidden = form.querySelectorAll('[data-mention-hidden]');
    assert.deepEqual(hidden.map((input) => input.getAttribute('value')), ['4', '3']);

    // Sending after deleting a name: that mention goes.
    textarea.value = 'Посмотрите, @Ирина Петрова';
    env.document.dispatch('submit', { target: form, preventDefault: () => {} });
    hidden = form.querySelectorAll('[data-mention-hidden]');
    assert.deepEqual(hidden.map((input) => input.getAttribute('value')), ['3']);
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
