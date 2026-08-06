'use strict';

/**
 * A minimal DOM / timer / EventSource / fetch harness.
 *
 * Deliberately hand-rolled: the project adds no npm, no Jest, no jsdom and no build
 * step, so the browser client is exercised against the smallest possible stub
 * that still behaves like the parts of the platform it actually uses.
 */

// --------------------------------------------------------------------------
// Selectors: `.class`, `[attr]`, `[attr="value"]`, `tag`
// --------------------------------------------------------------------------

function matches(element, selector) {
    const text = selector.trim();
    if (text.startsWith('.')) {
        return element.classList.contains(text.slice(1));
    }
    if (text.startsWith('[')) {
        const inner = text.slice(1, -1);
        const equals = inner.indexOf('=');
        if (equals === -1) {
            return element.getAttribute(inner) !== null;
        }
        const name = inner.slice(0, equals);
        const value = inner.slice(equals + 1).replace(/^["']|["']$/g, '');
        return element.getAttribute(name) === value;
    }
    return element.tagName === text.toUpperCase();
}

function camel(name) {
    return name.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

function dashed(name) {
    return name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'col', 'meta', 'link']);

class ClassList {
    constructor(element) {
        this.element = element;
    }

    get _values() {
        return (this.element.getAttribute('class') || '').split(/\s+/).filter(Boolean);
    }

    add(name) {
        const values = this._values;
        if (!values.includes(name)) {
            values.push(name);
            this.element.setAttribute('class', values.join(' '));
        }
    }

    remove(name) {
        this.element.setAttribute('class', this._values.filter((v) => v !== name).join(' '));
    }

    contains(name) {
        return this._values.includes(name);
    }

    toggle(name, force) {
        const shouldAdd = force === undefined ? !this.contains(name) : force;
        if (shouldAdd) {
            this.add(name);
        } else {
            this.remove(name);
        }
    }
}

class Element {
    constructor(tagName) {
        this.tagName = String(tagName).toUpperCase();
        this.attributes = new Map();
        this.children = [];
        this.parent = null;
        this.listeners = new Map();
        this._text = '';
        this.classList = new ClassList(this);
        this.dataset = new Proxy(
            {},
            {
                get: (_target, key) => this.getAttribute(`data-${dashed(String(key))}`) ?? undefined,
                set: (_target, key, value) => {
                    this.setAttribute(`data-${dashed(String(key))}`, String(value));
                    return true;
                },
                has: (_target, key) => this.getAttribute(`data-${dashed(String(key))}`) !== null,
            },
        );
    }

    get className() {
        return this.getAttribute('class') || '';
    }

    set className(value) {
        this.setAttribute('class', value);
    }

    get href() {
        return this.getAttribute('href') || '';
    }

    set href(value) {
        this.setAttribute('href', value);
    }

    get hidden() {
        return this.getAttribute('hidden') !== null;
    }

    set hidden(value) {
        if (value) {
            this.setAttribute('hidden', '');
        } else {
            this.attributes.delete('hidden');
        }
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    get textContent() {
        if (this.children.length) {
            return this.children.map((child) => child.textContent).join('');
        }
        return this._text;
    }

    set textContent(value) {
        this.children = [];
        this._text = String(value);
    }

    get innerHTML() {
        return this._html || '';
    }

    /**
     * Parse the server HTML a live refresh swaps in.
     *
     * Only what the fixtures actually contain — nested tags with plain
     * attributes and text — which is all the client needs to look up elements
     * by `data-*` selector.
     */
    set innerHTML(value) {
        this._html = String(value);
        this.children = [];
        this._text = '';
        const stack = [this];
        const token = /<\/?([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
        let match = token.exec(this._html);
        while (match) {
            const [raw, tagName, rawAttributes, selfClosing, text] = match;
            const parent = stack[stack.length - 1];
            if (text !== undefined) {
                if (text.trim()) {
                    parent._text += text;
                }
            } else if (raw.startsWith('</')) {
                if (stack.length > 1) {
                    stack.pop();
                }
            } else {
                const element = new Element(tagName);
                const attribute = /([\w:-]+)(?:="([^"]*)")?/g;
                let found = attribute.exec(rawAttributes || '');
                while (found) {
                    element.setAttribute(found[1], found[2] === undefined ? '' : found[2]);
                    found = attribute.exec(rawAttributes || '');
                }
                parent.append(element);
                if (!selfClosing && !VOID_TAGS.has(tagName.toLowerCase())) {
                    stack.push(element);
                }
            }
            match = token.exec(this._html);
        }
    }

    append(...nodes) {
        nodes.forEach((node) => {
            node.parent = this;
            this.children.push(node);
        });
    }

    remove() {
        if (!this.parent) {
            return;
        }
        this.parent.children = this.parent.children.filter((child) => child !== this);
        this.parent = null;
    }

    _descendants() {
        const found = [];
        this.children.forEach((child) => {
            found.push(child, ...child._descendants());
        });
        return found;
    }

    querySelector(selector) {
        return this._descendants().find((node) => matches(node, selector)) || null;
    }

    querySelectorAll(selector) {
        return this._descendants().filter((node) => matches(node, selector));
    }

    addEventListener(type, handler) {
        if (!this.listeners.has(type)) {
            this.listeners.set(type, []);
        }
        this.listeners.get(type).push(handler);
    }

    dispatch(type, event = {}) {
        (this.listeners.get(type) || []).forEach((handler) => handler({ type, ...event }));
    }
}

// --------------------------------------------------------------------------
// Fake clock
// --------------------------------------------------------------------------

class Clock {
    constructor() {
        this.now = 0;
        this.timers = new Map();
        this.nextId = 1;
    }

    setTimeout(callback, delay) {
        const id = this.nextId++;
        this.timers.set(id, { callback, due: this.now + (delay || 0) });
        return id;
    }

    clearTimeout(id) {
        this.timers.delete(id);
    }

    setInterval(callback, delay) {
        const id = this.nextId++;
        this.timers.set(id, { callback, due: this.now + (delay || 0), every: delay || 0 });
        return id;
    }

    advance(ms) {
        this.now += ms;
        const due = [...this.timers.entries()]
            .filter(([, timer]) => timer.due <= this.now)
            .sort((a, b) => a[1].due - b[1].due);
        due.forEach(([id, timer]) => {
            if (timer.every) {
                // Repeating timer: re-arm before running the callback.
                this.timers.set(id, { ...timer, due: this.now + timer.every });
            } else {
                this.timers.delete(id);
            }
            timer.callback();
        });
    }

    get pending() {
        return this.timers.size;
    }
}

// --------------------------------------------------------------------------
// Fake EventSource
// --------------------------------------------------------------------------

class FakeEventSource {
    constructor(url, options) {
        this.url = url;
        this.options = options;
        this.readyState = FakeEventSource.CONNECTING;
        this.listeners = new Map();
        this.closed = false;
        FakeEventSource.instances.push(this);
    }

    addEventListener(type, handler) {
        if (!this.listeners.has(type)) {
            this.listeners.set(type, []);
        }
        this.listeners.get(type).push(handler);
    }

    close() {
        this.closed = true;
        this.readyState = FakeEventSource.CLOSED;
    }

    emit(type, event = {}) {
        (this.listeners.get(type) || []).forEach((handler) => handler({ type, ...event }));
    }

    emitEvent(type, payload, lastEventId) {
        this.emit(type, {
            data: typeof payload === 'string' ? payload : JSON.stringify(payload),
            lastEventId: lastEventId || (payload && payload.event_id) || '',
        });
    }
}

FakeEventSource.CONNECTING = 0;
FakeEventSource.OPEN = 1;
FakeEventSource.CLOSED = 2;
FakeEventSource.instances = [];


// --------------------------------------------------------------------------
// Fake localStorage and BroadcastChannel, shared between the tabs of one test
// --------------------------------------------------------------------------

class FakeStorage {
    constructor({ broken = false } = {}) {
        this.data = new Map();
        this.broken = broken;
    }

    getItem(key) {
        if (this.broken) {
            throw new Error('storage unavailable');
        }
        return this.data.has(key) ? this.data.get(key) : null;
    }

    setItem(key, value) {
        if (this.broken) {
            throw new Error('storage unavailable');
        }
        this.data.set(key, String(value));
    }

    removeItem(key) {
        if (this.broken) {
            throw new Error('storage unavailable');
        }
        this.data.delete(key);
    }
}

class Bus {
    constructor() {
        this.channels = [];
    }
}

class FakeBroadcastChannel {
    constructor(name) {
        this.name = name;
        this.listeners = [];
        this.closed = false;
        const bus = FakeBroadcastChannel.bus;
        if (bus) {
            bus.channels.push(this);
        }
    }

    addEventListener(type, handler) {
        if (type === 'message') {
            this.listeners.push(handler);
        }
    }

    postMessage(data) {
        if (this.closed) {
            return;
        }
        const bus = FakeBroadcastChannel.bus;
        if (!bus) {
            return;
        }
        // A real BroadcastChannel never echoes back to its own sender.
        bus.channels
            .filter((channel) => channel !== this && !channel.closed)
            .forEach((channel) =>
                channel.listeners.forEach((handler) => handler({ data: JSON.parse(JSON.stringify(data)) })),
            );
    }

    close() {
        this.closed = true;
    }
}

FakeBroadcastChannel.bus = null;

// --------------------------------------------------------------------------
// Environment assembly
// --------------------------------------------------------------------------

function createEnvironment({
    realtimeEnabled = true,
    withEventSource = true,
    page = 'plain',
    actId = 3,
    storage: storageOption = new FakeStorage(),
    broadcast = true,
    resetSources = true,
} = {}) {
    const clock = new Clock();
    const storage = storageOption;
    if (resetSources) {
        FakeEventSource.instances = [];
    }

    const root = new Element('body');

    const config = new Element('div');
    config.setAttribute('data-realtime-config', '');
    config.setAttribute('data-realtime-enabled', realtimeEnabled ? 'true' : 'false');
    config.setAttribute('data-events-url', '/realtime/events/');
    config.setAttribute('data-notification-fragment-url', '/notifications/header-fragment/');
    config.setAttribute('data-notifications-url', '/notifications/');
    config.setAttribute('data-sync-url', '/realtime/sync/');
    config.setAttribute('data-degraded-after-seconds', '20');
    config.setAttribute('data-sync-poll-seconds', '30');
    config.setAttribute('data-sync-hidden-poll-seconds', '90');
    config.setAttribute('data-leader-lease-seconds', '12');
    config.setAttribute('data-leader-heartbeat-seconds', '4');
    config.setAttribute('data-live-sync-seconds', '300');
    root.append(config);

    const region = new Element('div');
    region.setAttribute('data-toast-region', '');
    root.append(region);

    // Page-specific live containers, mirroring what the Django templates render.
    const live = {};
    if (page === 'tasks') {
        const list = new Element('div');
        list.setAttribute('data-live-task-list', '');
        list.setAttribute('data-fragment-url', '/tasks/list-fragment/');
        list.textContent = 'исходный список задач';
        root.append(list);
        live.taskList = list;
    }
    if (page === 'acts') {
        const registry = new Element('div');
        registry.setAttribute('data-live-act-registry', '');
        registry.setAttribute('data-fragment-url', '/acts/list-fragment/');
        const kpis = new Element('section');
        kpis.setAttribute('data-live-act-registry-kpis', '');
        kpis.textContent = 'исходные KPI';
        const results = new Element('div');
        results.setAttribute('data-live-act-registry-results', '');
        results.textContent = 'исходные акты';
        registry.append(kpis, results);
        root.append(registry);
        live.actRegistry = registry;
        live.actKpis = kpis;
        live.actResults = results;
    }
    if (page === 'act-detail') {
        const actConfig = new Element('div');
        actConfig.setAttribute('data-live-act-config', '');
        actConfig.setAttribute('data-live-act-id', String(actId));
        actConfig.setAttribute('data-summary-url', `/acts/${actId}/live-summary-fragment/`);
        actConfig.setAttribute('data-work-url', `/acts/${actId}/work-fragment/`);
        actConfig.setAttribute('data-history-url', `/acts/${actId}/history-fragment/`);
        actConfig.setAttribute('data-comments-url', `/acts/${actId}/comments-fragment/`);
        actConfig.setAttribute('data-activities-url', `/acts/${actId}/activities-fragment/`);
        root.append(actConfig);

        const summary = new Element('div');
        summary.setAttribute('data-live-act-summary', '');
        summary.textContent = 'исходная сводка';
        const comments = new Element('div');
        comments.setAttribute('data-live-act-comments', '');
        comments.textContent = 'исходные комментарии';
        const activities = new Element('section');
        activities.setAttribute('data-live-act-activities', '');
        activities.textContent = 'исходные мероприятия';
        const history = new Element('div');
        history.setAttribute('data-live-act-history', '');
        history.textContent = 'исходная история';
        const work = new Element('div');
        work.setAttribute('data-live-act-work', '');
        work.textContent = 'исходная работа';

        const conflict = new Element('div');
        conflict.setAttribute('data-act-conflict-banner', '');
        conflict.hidden = true;
        const access = new Element('div');
        access.setAttribute('data-act-access-banner', '');
        access.hidden = true;

        // A working form the live refresh must never touch.
        const textarea = new Element('textarea');
        textarea.setAttribute('name', 'text');
        textarea.value = '';
        const workflowButton = new Element('button');
        workflowButton.setAttribute('data-workflow-submit', '');
        workflowButton.disabled = false;

        root.append(summary, work, history, comments, activities, conflict, access, textarea, workflowButton);
        Object.assign(live, {
            actConfig,
            summary,
            work,
            history,
            comments,
            activities,
            conflictBanner: conflict,
            accessBanner: access,
            textarea,
            workflowButton,
        });
    }

    const documentListeners = new Map();
    const document = {
        body: root,
        readyState: 'complete',
        createElement: (tag) => new Element(tag),
        querySelector: (selector) => (matches(root, selector) ? root : root.querySelector(selector)),
        querySelectorAll: (selector) => root.querySelectorAll(selector),
        addEventListener(type, handler) {
            if (!documentListeners.has(type)) {
                documentListeners.set(type, []);
            }
            documentListeners.get(type).push(handler);
        },
        dispatch(type, event = {}) {
            (documentListeners.get(type) || []).forEach((handler) => handler({ type, ...event }));
        },
    };

    // The bell double: `replaceItems` stores server HTML as elements the client
    // can look up, exactly like the real menu built from the Django partial.
    const itemsContainer = new Element('div');
    const menu = {
        element: new Element('details'),
        itemsContainer,
        unreadCount: null,
        replacedHtml: [],
        updateCounter(count) {
            this.unreadCount = count;
        },
        replaceItems(html) {
            this.replacedHtml.push(html);
            itemsContainer.children = [];
            // A tiny parser for the shape the Django partial produces.
            const pattern = /<a[^>]*href="([^"]*)"[^>]*data-notification-id="(\d+)"[^>]*>\s*<strong[^>]*>([^<]*)<\/strong>\s*<span[^>]*>([^<]*)<\/span>/g;
            let match = pattern.exec(html);
            while (match) {
                const item = new Element('a');
                item.setAttribute('href', match[1]);
                item.setAttribute('data-notification-id', match[2]);
                const title = new Element('strong');
                title.setAttribute('data-notification-title', '');
                title.textContent = match[3];
                const message = new Element('span');
                message.setAttribute('data-notification-message', '');
                message.textContent = match[4];
                item.append(title, message);
                itemsContainer.append(item);
                match = pattern.exec(html);
            }
        },
    };

    const fetchCalls = [];
    let fetchHandler = () => ({ unread_count: 0, items_html: '', generated_at: '', latest_notification_id: null });

    const fetchStub = (url, options = {}) => {
        const call = { url, options, resolve: null, reject: null };
        fetchCalls.push(call);
        return new Promise((resolve, reject) => {
            call.resolve = resolve;
            call.reject = reject;
            const outcome = fetchHandler(call, fetchCalls.length);
            if (outcome === 'manual') {
                return;
            }
            if (outcome && outcome.redirected) {
                // A technical endpoint redirected: a real fetch would still
                // report `ok: true` for the final (HTML) response, so the
                // client must catch this from `redirected` alone.
                resolve({
                    ok: true,
                    status: outcome.status || 200,
                    redirected: true,
                    headers: { get: () => outcome.contentType || 'text/html' },
                    json: async () => ({}),
                });
                return;
            }
            if (outcome && outcome.contentType && outcome.contentType !== 'application/json') {
                resolve({
                    ok: true,
                    status: outcome.status || 200,
                    redirected: false,
                    headers: { get: () => outcome.contentType },
                    json: async () => {
                        throw new Error('not json');
                    },
                });
                return;
            }
            if (outcome && outcome.status && outcome.status !== 200) {
                resolve({ ok: false, status: outcome.status, redirected: false, json: async () => ({}) });
                return;
            }
            if (options.signal && options.signal.aborted) {
                reject(new Error('aborted'));
                return;
            }
            resolve({ ok: true, status: 200, redirected: false, json: async () => outcome });
        });
    };

    const windowListeners = new Map();
    const window = {
        EventSource: withEventSource ? FakeEventSource : undefined,
        setTimeout: (callback, delay) => clock.setTimeout(callback, delay),
        clearTimeout: (id) => clock.clearTimeout(id),
        setInterval: (callback, delay) => clock.setInterval(callback, delay),
        clearInterval: (id) => clock.clearTimeout(id),
        // Deliberately separate from the real global `Date` that `tabs.js`'s
        // leader lease still uses: only code that explicitly reads
        // `window.Date.now()` (the live safety-sync staleness check) moves in
        // lockstep with the fake clock that `advance()` controls.
        Date: { now: () => clock.now },
        matchMedia: () => ({ matches: false }),
        addEventListener(type, handler) {
            if (!windowListeners.has(type)) {
                windowListeners.set(type, []);
            }
            windowListeners.get(type).push(handler);
        },
        dispatch(type, event = {}) {
            (windowListeners.get(type) || []).forEach((handler) => handler({ type, ...event }));
        },
        qualityNotificationMenu: menu,
        qualityFragments: {
            register: () => {},
            reinitialise() {
                this.reinitialiseCalls += 1;
            },
            reinitialiseCalls: 0,
            claim: () => true,
        },
        fetch: fetchStub,
        location: { search: '', reload: () => {} },
        localStorage: storage,
        BroadcastChannel: broadcast ? FakeBroadcastChannel : undefined,
    };

    return {
        clock,
        document,
        window,
        region,
        menu,
        config,
        live,
        fetchCalls,
        FakeEventSource,
        setFetchHandler(handler) {
            fetchHandler = handler;
        },
        callsTo(url) {
            return fetchCalls.filter((call) => call.url.split('?')[0] === url);
        },
        get source() {
            return FakeEventSource.instances[FakeEventSource.instances.length - 1] || null;
        },
        get sources() {
            return FakeEventSource.instances;
        },
        get toasts() {
            return region.querySelectorAll('.toast');
        },
    };
}

const flush = async (times = 6) => {
    for (let index = 0; index < times; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
};

module.exports = {
    createEnvironment,
    Element,
    FakeEventSource,
    FakeBroadcastChannel,
    FakeStorage,
    Bus,
    flush,
    matches,
};
