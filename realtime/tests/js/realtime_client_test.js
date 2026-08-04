'use strict';

/**
 * Smoke test for `static/js/realtime.js`.
 *
 * Runs on plain Node with the hand-rolled harness next to it — no npm, no
 * Jest, no jsdom, no build step. Invoked from `realtime/tests/test_js_client.py`
 * so it participates in the normal `manage.py test` run.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createEnvironment, flush } = require('./dom_harness');

const CLIENT_PATH = path.join(__dirname, '..', '..', '..', 'static', 'js', 'realtime.js');
const CLIENT_SOURCE = fs.readFileSync(CLIENT_PATH, 'utf8');

function load(options) {
    const env = createEnvironment(options);
    const context = {
        window: env.window,
        document: env.document,
        fetch: env.window.fetch,
        EventSource: env.window.EventSource,
        AbortController,
        Number,
        JSON,
        Promise,
        Set,
        Array,
        Math,
        console,
        setImmediate,
    };
    context.globalThis = context;
    vm.createContext(context);
    vm.runInContext(CLIENT_SOURCE, context);
    return env;
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

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// --------------------------------------------------------------------------

test('a disabled configuration never opens an EventSource', async () => {
    const env = load({ realtimeEnabled: false });

    assert.equal(env.sources.length, 0);
    assert.equal(env.fetchCalls.length, 0);
});

test('a browser without EventSource support is left alone', async () => {
    const env = load({ withEventSource: false });

    assert.equal(env.fetchCalls.length, 0);
});

test('the stream is opened without any user id, target or channel', async () => {
    const env = load();

    assert.equal(env.sources.length, 1);
    assert.equal(env.source.url, '/realtime/events/');
    assert.equal(env.source.options.withCredentials, true);
    assert.ok(!env.source.url.includes('user'));
    assert.ok(!env.source.url.includes('channel'));
});

test('open refreshes the fragment and shows no toast', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([{ id: 7, title: 'Заголовок', message: 'Текст', url: '/acts/3/' }]));

    env.source.emit('open');
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 1);
    assert.equal(env.fetchCalls[0].url, '/notifications/header-fragment/');
    assert.equal(env.fetchCalls[0].options.credentials, 'same-origin');
    assert.equal(env.menu.unreadCount, 1);
    assert.equal(env.toasts.length, 0, 'existing notifications must stay silent on open');
});

test('every reconnect resyncs the fragment again', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([]));

    env.source.emit('open');
    env.clock.advance(200);
    await flush();
    env.source.emit('error');
    env.source.emit('open');
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 2);
    assert.equal(env.toasts.length, 0);
});

test('notification.created refreshes and shows one toast built from server markup', async () => {
    const env = load();
    env.setFetchHandler(() =>
        fragment([{ id: 11, title: 'Акт АОК-2026-001 передан в КО', message: 'Требуется решение', url: '/acts/3/' }]),
    );

    env.source.emitEvent('notification.created', createdEvent(11));
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 1);
    assert.equal(env.menu.unreadCount, 1);
    assert.equal(env.toasts.length, 1);
    const toast = env.toasts[0];
    assert.equal(toast.querySelector('.toast__title').textContent, 'Акт АОК-2026-001 передан в КО');
    assert.equal(toast.querySelector('.toast__message').textContent, 'Требуется решение');
    assert.equal(toast.querySelector('.toast__link').getAttribute('href'), '/acts/3/');
    assert.equal(toast.getAttribute('role'), 'status');
});

test('the toast never uses text from the SSE payload', async () => {
    const env = load();
    const payload = createdEvent(11);
    payload.data.title = 'ПОДДЕЛЬНЫЙ ЗАГОЛОВОК';
    payload.data.message = '<img src=x onerror=alert(1)>';
    env.setFetchHandler(() => fragment([{ id: 11, title: 'Настоящий', message: 'Из Django', url: '/acts/3/' }]));

    env.source.emitEvent('notification.created', payload);
    env.clock.advance(200);
    await flush();

    const toast = env.toasts[0];
    assert.equal(toast.querySelector('.toast__title').textContent, 'Настоящий');
    assert.equal(toast.querySelector('.toast__message').textContent, 'Из Django');
    assert.ok(!toast.textContent.includes('ПОДДЕЛЬНЫЙ'));
    assert.ok(!toast.textContent.includes('onerror'));
});

test('a notification outside the newest five falls back to a generic toast', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([{ id: 1, title: 'Другое', message: 'Другое', url: '/acts/1/' }], 9));

    env.source.emitEvent('notification.created', createdEvent(999));
    env.clock.advance(200);
    await flush();

    const toast = env.toasts[0];
    assert.equal(toast.querySelector('.toast__title').textContent, 'Новое уведомление');
    assert.equal(toast.querySelector('.toast__link').getAttribute('href'), '/notifications/');
});

test('a redelivered event id does not produce a second toast', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([{ id: 11, title: 'Раз', message: 'Текст', url: '/acts/3/' }]));

    env.source.emitEvent('notification.created', createdEvent(11, 'same-id'));
    env.clock.advance(200);
    await flush();
    env.source.emitEvent('notification.created', createdEvent(11, 'same-id'));
    env.clock.advance(200);
    await flush();

    assert.equal(env.toasts.length, 1);
    // A duplicate still triggers a safe resync.
    assert.equal(env.fetchCalls.length, 2);
});

test('a burst of events collapses into a single fragment request', async () => {
    const env = load();
    env.setFetchHandler(() =>
        fragment([
            { id: 3, title: 'Третье', message: 'C', url: '/acts/3/' },
            { id: 2, title: 'Второе', message: 'B', url: '/acts/2/' },
            { id: 1, title: 'Первое', message: 'A', url: '/acts/1/' },
        ]),
    );

    env.source.emitEvent('notification.created', createdEvent(1, 'e1'));
    env.source.emitEvent('notification.created', createdEvent(2, 'e2'));
    env.source.emitEvent('notification.created', createdEvent(3, 'e3'));
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 1, 'debounce must merge the burst');
    assert.equal(env.toasts.length, 3);
});

test('at most three toasts stay visible', async () => {
    const env = load();
    const items = [1, 2, 3, 4, 5].map((id) => ({ id, title: `T${id}`, message: `M${id}`, url: `/acts/${id}/` }));
    env.setFetchHandler(() => fragment(items));

    items.forEach((item) => env.source.emitEvent('notification.created', createdEvent(item.id, `e${item.id}`)));
    env.clock.advance(200);
    await flush();

    assert.equal(env.toasts.length, 3);
    assert.equal(env.toasts[0].querySelector('.toast__title').textContent, 'T3');
});

test('a stale response never overwrites newer state', async () => {
    const env = load();
    let call = 0;
    env.setFetchHandler(() => {
        call += 1;
        return 'manual';
    });

    env.source.emitEvent('notification.created', createdEvent(1, 'first'));
    env.clock.advance(200);
    await flush();
    env.source.emitEvent('notification.created', createdEvent(2, 'second'));
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 2);
    // The newer request answers first, then the older one arrives late.
    env.fetchCalls[1].resolve({ ok: true, status: 200, json: async () => fragment([], 5) });
    await flush();
    env.fetchCalls[0].resolve({ ok: true, status: 200, json: async () => fragment([], 99) });
    await flush();

    assert.equal(env.menu.unreadCount, 5, 'the late response must be discarded');
    assert.equal(call, 2);
});

test('a failed request keeps the current markup and counter', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([{ id: 7, title: 'Есть', message: 'Текст', url: '/acts/3/' }]));
    env.source.emit('open');
    env.clock.advance(200);
    await flush();
    const replacedBefore = env.menu.replacedHtml.length;

    env.setFetchHandler((call) => {
        call.reject(new Error('network down'));
        return 'manual';
    });
    env.source.emitEvent('notification.created', createdEvent(8, 'boom'));
    env.clock.advance(200);
    await flush();

    assert.equal(env.menu.unreadCount, 1, 'the counter must not be reset');
    assert.equal(env.menu.replacedHtml.length, replacedBefore, 'markup must not be blanked');
    assert.equal(env.toasts.length, 0);
});

test('notification.read refreshes without a toast', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([], 0));

    env.source.emitEvent('notification.read', readEvent('read-1'));
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 1);
    assert.equal(env.menu.unreadCount, 0);
    assert.equal(env.toasts.length, 0);
});

test('notification.read with scope=all is handled without notification_ids', async () => {
    const env = load();
    const payload = readEvent('read-all', 'all');
    delete payload.data.notification_ids;
    env.setFetchHandler(() => fragment([], 0));

    env.source.emitEvent('notification.read', payload);
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 1);
    assert.equal(env.menu.unreadCount, 0);
});

test('malformed and foreign events are ignored without errors', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([]));

    env.source.emitEvent('notification.created', '{not json');
    env.source.emitEvent('notification.created', { event_type: 'notification.created', resource_type: 'act', resource_id: 3 });
    env.source.emitEvent('notification.created', { event_type: 'notification.created', resource_type: 'notification', resource_id: -1 });
    env.source.emitEvent('notification.read', { event_type: 'notification.read', resource_type: 'notification', resource_id: 1 });
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 0);
    assert.equal(env.toasts.length, 0);
});

test('other event types are ignored', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([]));

    env.source.emitEvent('task.created', { event_type: 'task.created', resource_type: 'task', resource_id: 4 });
    env.source.emitEvent('act.status_changed', { event_type: 'act.status_changed', resource_type: 'act', resource_id: 3 });
    env.clock.advance(200);
    await flush();

    assert.equal(env.fetchCalls.length, 0);
});

test('a toast auto-dismisses and pauses while hovered', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([{ id: 11, title: 'Живёт', message: 'Текст', url: '/acts/3/' }]));

    env.source.emitEvent('notification.created', createdEvent(11));
    env.clock.advance(200);
    await flush();
    const toast = env.toasts[0];

    toast.dispatch('mouseenter');
    env.clock.advance(20000);
    assert.equal(env.toasts.length, 1, 'hover must pause the auto-dismiss');

    toast.dispatch('mouseleave');
    env.clock.advance(20000);
    assert.equal(env.toasts.length, 0, 'the toast must close after the timeout');
});

test('a toast closes on its button and on Escape', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([{ id: 11, title: 'Закрыть', message: 'Текст', url: '/acts/3/' }]));

    env.source.emitEvent('notification.created', createdEvent(11, 'close-1'));
    env.clock.advance(200);
    await flush();
    const close = env.toasts[0].querySelector('.toast__close');
    assert.equal(close.getAttribute('aria-label'), 'Закрыть уведомление');
    close.dispatch('click');
    assert.equal(env.toasts.length, 0);

    env.source.emitEvent('notification.created', createdEvent(12, 'close-2'));
    env.clock.advance(200);
    await flush();
    env.toasts[0].dispatch('keydown', { key: 'Escape' });
    assert.equal(env.toasts.length, 0);
});

test('a 401 stops the client instead of looping', async () => {
    const env = load();
    env.setFetchHandler(() => ({ status: 401 }));

    env.source.emit('open');
    env.clock.advance(200);
    await flush();
    assert.equal(env.fetchCalls.length, 1);
    assert.equal(env.source.closed, true, 'the stream must be closed after a 401');

    env.source.emitEvent('notification.created', createdEvent(11, 'after-401'));
    env.clock.advance(500);
    await flush();

    assert.equal(env.fetchCalls.length, 1, 'no further requests after a 401');
});

test('an error event alone does not close the stream or clear the UI', async () => {
    const env = load();
    env.setFetchHandler(() => fragment([{ id: 7, title: 'Есть', message: 'Текст', url: '/acts/3/' }]));
    env.source.emit('open');
    env.clock.advance(200);
    await flush();

    env.source.emit('error');

    assert.equal(env.source.closed, false, 'the browser reconnects on its own');
    assert.equal(env.menu.unreadCount, 1);
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
