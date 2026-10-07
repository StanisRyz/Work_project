'use strict';

/**
 * A minimal DOM / timer / EventSource / fetch harness.
 *
 * Deliberately hand-rolled: the project adds no npm, no Jest, no jsdom and no build
 * step, so the browser client is exercised against the smallest possible stub
 * that still behaves like the parts of the platform it actually uses.
 */

// --------------------------------------------------------------------------
// Selectors: `.class`, `[attr]`, `[attr="value"]`, `tag`, a tag or
// attributes compounded: `details[data-board-menu][open]`, `a.board-tile`,
// and a comma-separated list of those
// --------------------------------------------------------------------------

function matches(element, selector) {
    if (selector.indexOf(',') !== -1) {
        return selector.split(',').some((part) => matches(element, part));
    }
    const text = selector.trim();
    const tagClass = /^([a-zA-Z][\w-]*)\.([\w-]+)$/.exec(text);
    if (tagClass) {
        return element.tagName === tagClass[1].toUpperCase() && element.classList.contains(tagClass[2]);
    }
    const compound = /^([a-zA-Z][\w-]*)?((?:\[[^\]]+\])+)$/.exec(text);
    if (compound && (compound[1] || compound[2].indexOf('][') !== -1)) {
        if (compound[1] && element.tagName !== compound[1].toUpperCase()) {
            return false;
        }
        return compound[2].match(/\[[^\]]+\]/g).every((part) => matches(element, part));
    }
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

    removeAttribute(name) {
        this.attributes.delete(name);
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

    contains(node) {
        let current = node;
        while (current) {
            if (current === this) {
                return true;
            }
            current = current.parent;
        }
        return false;
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

    matches(selector) {
        return matches(this, selector);
    }

    closest(selector) {
        let current = this;
        while (current instanceof Element) {
            if (matches(current, selector)) {
                return current;
            }
            current = current.parent;
        }
        return null;
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
    workRevision = 'work-rev-initial',
    workBound = false,
    actStatus = 'KO_REVIEW',
    boardPanelHoldsInput = false,
    storage: storageOption = new FakeStorage(),
    broadcast = true,
    resetSources = true,
    coordinationEpoch = 'test-session-epoch-000000000001',
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
    config.setAttribute('data-coordination-epoch', coordinationEpoch);
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
    if (page === 'board') {
        // The board page as `boards/detail.html` draws it: the root with the
        // server-built addresses and the fingerprints, the columns with one
        // tile, and the card drawer — its heading (the guarded panel, with
        // «Завершить»'s result field), the tab strip, «Описание» (the guarded
        // block's second container, the checklist, the facts — the third — and
        // «Подписчики»), «Чат» with its modes, its list and its form with the
        // file input, and «Лог» — and the conflict banner.
        const board = new Element('div');
        board.setAttribute('data-board', '');
        board.setAttribute('data-board-id', '4');
        board.setAttribute('data-board-url', '/work/boards/4/7/');
        board.setAttribute('data-board-page-url', '/work/boards/4/7/?card=9');
        board.setAttribute('data-board-fragment-url', '/work/boards/4/7/fragment/?card=9');
        board.setAttribute('data-board-fragment-base', '/work/boards/4/7/fragment/');
        board.setAttribute('data-board-close-url', '/work/boards/4/7/');
        board.setAttribute('data-board-close-fragment-url', '/work/boards/4/7/fragment/');
        board.setAttribute('data-board-panel', 'view');
        board.setAttribute('data-tabs-revision', 'tabs-rev-initial');
        board.setAttribute('data-columns-revision', 'columns-rev-initial');
        board.setAttribute('data-panel-revision', 'panel-rev-initial');
        board.setAttribute('data-comments-revision', 'comments-rev-initial');
        board.setAttribute('data-log-revision', 'log-rev-initial');
        board.setAttribute('data-checklist-revision', 'checklist-rev-initial');
        board.setAttribute('data-followers-revision', 'followers-rev-initial');
        board.setAttribute('data-panel-holds-input', boardPanelHoldsInput ? 'true' : 'false');
        // The sub-board tabs: a read-only live block of their own.
        const tabs = new Element('div');
        tabs.setAttribute('data-live-board-tabs', '');
        tabs.innerHTML = '<nav><a data-tab="7">исходная вкладка</a></nav>';
        const layout = new Element('div');
        layout.setAttribute('data-board-layout', '');
        layout.setAttribute('class', 'board-layout board-layout--with-panel');
        const columns = new Element('div');
        columns.setAttribute('data-live-board-columns', '');
        columns.setAttribute('data-current-card', '9');
        columns.innerHTML = '<section data-column-id="31"><ol data-column-list>'
            + '<li data-card-id="9" data-task-id="21" data-card-movable>'
            + '<a class="board-tile board-tile--open" href="/work/boards/4/7/?card=9">исходная плитка</a></li>'
            + '<li data-card-id="12" data-task-id="24" data-card-movable>'
            + '<a class="board-tile" href="/work/boards/4/7/?card=12&mine=1">вторая плитка</a></li>'
            + '</ol></section>';
        const drawer = new Element('aside');
        drawer.setAttribute('data-board-drawer', '');
        drawer.setAttribute('data-board-tab', 'description');
        const panel = new Element('div');
        panel.setAttribute('data-live-board-panel', '');
        panel.setAttribute('data-task-id', '21');
        const close = new Element('a');
        close.setAttribute('href', '/work/boards/4/7/');
        close.setAttribute('data-board-drawer-close', '');
        const execution = new Element('textarea');
        execution.setAttribute('name', 'execution_comment');
        execution.value = '';
        panel.append(close, execution);
        const strip = new Element('nav');
        strip.innerHTML = ['description', 'chat', 'log'].map((name) => (
            `<a class="board-drawer__tab${name === 'description' ? ' is-active' : ''}" `
            + `href="/work/boards/4/7/?card=9&tab=${name}" data-board-tab-link="${name}" `
            + `data-board-tab-fragment-url="/work/boards/4/7/fragment/?card=9&tab=${name}">${name}`
            + `${name === 'chat' ? `<span data-board-tab-count="${name}">1</span>` : ''}</a>`
        )).join('');
        const card = new Element('div');
        card.setAttribute('data-live-board-card', '');
        card.innerHTML = '<section data-board-tab-body="description">исходное описание</section>';
        // The facts and the tools: the guarded block's third container, with
        // «Переместить в…»'s select — and «Подписчики», a read-only block.
        const facts = new Element('div');
        facts.setAttribute('data-live-board-facts', '');
        facts.innerHTML = '<section data-board-tab-body="description"><dl>исходные факты</dl></section>';
        const move = new Element('select');
        move.setAttribute('name', 'column_id');
        facts.append(move);
        const followers = new Element('div');
        followers.setAttribute('data-live-board-followers', '');
        followers.innerHTML = '<dl>исходные подписчики</dl>';
        // «Описание»'s pane: the guarded bodies above, then the card's
        // «Чек-лист» — a live block of its own — and its «Добавить пункт»,
        // in no block, as `boards/includes/drawer.html` draws them.
        const pane = new Element('div');
        pane.setAttribute('class', 'board-drawer__pane');
        const checklist = new Element('div');
        checklist.setAttribute('data-live-board-checklist', '');
        checklist.innerHTML = '<ol><li data-checklist-item="1">исходный пункт</li></ol>';
        const checklistText = new Element('input');
        checklistText.setAttribute('name', 'text');
        checklistText.value = '';
        pane.append(card, checklist, checklistText, facts, followers);
        // «Чат»: «Все сообщения | Только файлы · N», the read-only list and,
        // below it, its form with the file input and the chosen files —
        // outside every live block, as `boards/includes/drawer.html` draws them.
        const chat = new Element('section');
        chat.setAttribute('data-board-tab-body', 'chat');
        chat.setAttribute('data-board-chat-mode', 'messages');
        const modes = new Element('nav');
        modes.innerHTML = ['messages', 'files'].map((name) => (
            `<a class="board-chat__mode${name === 'messages' ? ' is-active' : ''}" `
            + `href="/work/boards/4/7/?card=9&tab=chat${name === 'files' ? '&chat=files' : ''}" data-board-chat-mode-link="${name}" `
            + `data-board-chat-mode-fragment-url="/work/boards/4/7/fragment/?card=9&tab=chat${name === 'files' ? '&chat=files' : ''}">${name}`
            + `${name === 'files' ? '<span data-board-chat-files-count>1</span>' : ''}</a>`
        )).join('');
        chat.append(modes);
        const comments = new Element('div');
        comments.setAttribute('data-live-board-comments', '');
        comments.innerHTML = '<ol><li data-comment-id="1">исходное сообщение</li></ol>';
        comments.scrollTop = 0;
        comments.scrollHeight = 0;
        comments.clientHeight = 0;
        const chatForm = new Element('form');
        chatForm.setAttribute('class', 'board-chat__form');
        chatForm.setAttribute('data-chat-files', '');
        const commentText = new Element('textarea');
        commentText.setAttribute('name', 'text');
        commentText.value = '';
        const chosen = new Element('ul');
        chosen.setAttribute('data-chat-files-list', '');
        chosen.hidden = true;
        const fileInput = new Element('input');
        fileInput.setAttribute('type', 'file');
        fileInput.setAttribute('name', 'files');
        fileInput.setAttribute('data-chat-files-input', '');
        fileInput.files = [];
        chatForm.append(commentText, chosen, fileInput);
        chat.append(comments, chatForm);
        const logBody = new Element('section');
        logBody.setAttribute('data-board-tab-body', 'log');
        const log = new Element('div');
        log.setAttribute('data-live-board-log', '');
        log.innerHTML = '<ol><li>исходная запись</li></ol>';
        logBody.append(log);
        drawer.append(panel, strip, pane, chat, logBody);
        layout.append(columns, drawer);
        const message = new Element('div');
        message.setAttribute('data-board-message', '');
        message.hidden = true;
        board.append(tabs, message, layout);
        const conflict = new Element('div');
        conflict.setAttribute('data-board-conflict-banner', '');
        conflict.hidden = true;
        const modalTextarea = new Element('textarea');
        modalTextarea.setAttribute('name', 'comment');
        root.append(conflict, board, modalTextarea);
        Object.assign(live, {
            board,
            tabs,
            layout,
            columns,
            drawer,
            panel,
            card,
            facts,
            move,
            followers,
            execution,
            chat,
            comments,
            commentText,
            chatForm,
            chosen,
            fileInput,
            log,
            checklist,
            checklistText,
            conflictBanner: conflict,
            modalTextarea,
        });
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
        actConfig.setAttribute('data-live-act-status', actStatus);
        actConfig.setAttribute('data-work-revision', workRevision);
        actConfig.setAttribute('data-work-bound', workBound ? 'true' : 'false');
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
        const workText = new Element('span');
        workText.textContent = 'исходная работа';
        work.append(workText);

        const conflict = new Element('div');
        conflict.setAttribute('data-act-conflict-banner', '');
        conflict.hidden = true;
        const access = new Element('div');
        access.setAttribute('data-act-access-banner', '');
        access.hidden = true;

        // A working form the live refresh must never touch — inside the work
        // tab, exactly where the Django template renders it.
        const textarea = new Element('textarea');
        textarea.setAttribute('name', 'text');
        textarea.value = '';
        const workflowButton = new Element('button');
        workflowButton.setAttribute('data-workflow-submit', '');
        workflowButton.disabled = false;
        work.append(textarea, workflowButton);

        // A field outside the work tab — the confirmation modal's comment, the
        // bug report — which is not part of the form a refresh could discard.
        const modalTextarea = new Element('textarea');
        modalTextarea.setAttribute('name', 'comment');
        modalTextarea.value = '';

        root.append(summary, work, history, comments, activities, conflict, access, modalTextarea);
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
            modalTextarea,
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
        // What a script's own `CustomEvent` goes through.
        dispatchEvent(event) {
            (documentListeners.get(event.type) || []).forEach((handler) => handler(event));
            return true;
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
            resolve({
                ok: true,
                status: 200,
                redirected: false,
                headers: { get: () => 'application/json' },
                json: async () => outcome,
            });
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
        location: {
            href: 'http://quality.test/',
            search: '',
            reload: () => {},
            replaced: [],
            replace(url) {
                this.replaced.push(url);
            },
            assigned: [],
            assign(url) {
                this.assigned.push(url);
            },
        },
        // The history entries a page pushed or replaced, newest last.
        history: {
            state: null,
            pushed: [],
            replaced: [],
            pushState(state, _title, url) {
                this.state = state;
                this.pushed.push(url);
            },
            replaceState(state, _title, url) {
                this.state = state;
                this.replaced.push(url);
            },
        },
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
