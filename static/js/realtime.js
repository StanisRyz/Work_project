/**
 * Real-time browser client (RT-3 notifications + RT-4 tasks and acts).
 *
 * One core owns the single EventSource, the bounded event-id deduplication and
 * the reconnect lifecycle. Feature modules never touch EventSource: they
 * register through the internal bus with `subscribe(eventType, handler)` and
 * `onOpen(handler)`.
 *
 * The SSE stream is a *signal*, never a data source. An event only says that
 * something changed; every string, every URL and every permission decision
 * comes from an ordinary authenticated Django endpoint afterwards.
 *
 * No framework, no bundler, no third-party dependency.
 */
(() => {
    'use strict';

    const REFRESH_DEBOUNCE_MS = 150;
    const SEEN_EVENT_LIMIT = 100;
    const TOAST_TIMEOUT_MS = 8000;
    const MAX_VISIBLE_TOASTS = 3;

    // Only `notification.created` ever produces a toast. Task and act events
    // update blocks silently — the user already gets a toast through the
    // matching internal notification when the business rules define one.
    const EVENT_TYPES = {
        NOTIFICATION_CREATED: 'notification.created',
        NOTIFICATION_READ: 'notification.read',
        TASK_CREATED: 'task.created',
        TASK_UPDATED: 'task.updated',
        TASK_COMPLETED: 'task.completed',
        ACT_UPDATED: 'act.updated',
        ACT_STATUS_CHANGED: 'act.status_changed',
        COMMENT_CREATED: 'comment.created',
    };

    const SUBSCRIBED_EVENTS = Object.values(EVENT_TYPES);

    // ---------------------------------------------------------------------
    // Shared helpers
    // ---------------------------------------------------------------------

    const isPositiveInteger = (value) => Number.isInteger(value) && value > 0;

    const parsePayload = (event) => {
        try {
            const payload = JSON.parse(event.data);
            return payload && typeof payload === 'object' && !Array.isArray(payload)
                ? payload
                : null;
        } catch (error) {
            return null;
        }
    };

    /**
     * One debounced, cancellable refresh pipeline for a single live block.
     *
     * Several events in a burst collapse into one request; only the newest
     * request may write to the DOM, so a late response is discarded; a failed
     * request leaves the current markup and counters untouched.
     */
    const createRefreshCoordinator = ({ url, apply, onDenied, onError }) => {
        let timer = null;
        let generation = 0;
        let inFlight = null;
        let stopped = false;
        const pending = new Set();

        const run = () => {
            timer = null;
            if (stopped) {
                return;
            }
            generation += 1;
            const requestGeneration = generation;
            if (inFlight) {
                inFlight.abort();
            }
            const controller = typeof AbortController === 'function' ? new AbortController() : null;
            inFlight = controller;

            const context = [...pending];
            pending.clear();
            const target = typeof url === 'function' ? url() : url;

            fetch(target, {
                method: 'GET',
                credentials: 'same-origin',
                headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                signal: controller ? controller.signal : undefined,
            })
                .then((response) => {
                    if (response.status === 401 || response.status === 403 || response.status === 404) {
                        stopped = true;
                        if (onDenied) {
                            onDenied(response.status);
                        }
                        return null;
                    }
                    if (!response.ok) {
                        throw new Error(`Unexpected status ${response.status}`);
                    }
                    return response.json();
                })
                .then((payload) => {
                    if (!payload || requestGeneration !== generation) {
                        return;
                    }
                    apply(payload, context);
                })
                .catch(() => {
                    if (onError) {
                        onError();
                    }
                })
                .finally(() => {
                    if (inFlight === controller) {
                        inFlight = null;
                    }
                });
        };

        return {
            schedule(context) {
                if (stopped) {
                    return;
                }
                if (context !== undefined && context !== null) {
                    pending.add(context);
                }
                if (timer !== null) {
                    return;
                }
                timer = window.setTimeout(run, REFRESH_DEBOUNCE_MS);
            },
            stop() {
                stopped = true;
                if (timer !== null) {
                    window.clearTimeout(timer);
                    timer = null;
                }
                if (inFlight) {
                    inFlight.abort();
                    inFlight = null;
                }
            },
            get isStopped() {
                return stopped;
            },
        };
    };

    /** Re-send the page's own query string so tab, filters and sorting survive. */
    const withCurrentQuery = (base) => {
        const query = window.location.search;
        if (!query || query === '?') {
            return base;
        }
        return base + (base.includes('?') ? '&' : '?') + query.slice(1);
    };

    // ---------------------------------------------------------------------
    // Core: bus + EventSource lifecycle
    // ---------------------------------------------------------------------

    const createCore = (config) => {
        const handlers = new Map();
        const openHandlers = [];
        const seenIds = new Set();
        const seenOrder = [];
        let source = null;
        let stopped = false;

        const rememberEvent = (eventId) => {
            if (!eventId) {
                return true;
            }
            if (seenIds.has(eventId)) {
                return false;
            }
            seenIds.add(eventId);
            seenOrder.push(eventId);
            while (seenOrder.length > SEEN_EVENT_LIMIT) {
                seenIds.delete(seenOrder.shift());
            }
            return true;
        };

        const bus = {
            config,
            subscribe(eventType, handler) {
                if (!handlers.has(eventType)) {
                    handlers.set(eventType, []);
                }
                handlers.get(eventType).push(handler);
            },
            onOpen(handler) {
                openHandlers.push(handler);
            },
            stop() {
                stopped = true;
                if (source) {
                    source.close();
                    source = null;
                }
            },
        };

        const dispatch = (eventType, event) => {
            const payload = parsePayload(event);
            if (!payload || payload.event_type !== eventType) {
                return;
            }
            const eventId = event.lastEventId || payload.event_id;
            // `isFirstDelivery` lets a module decide whether to toast; a
            // redelivery may still trigger a safe resync.
            const isFirstDelivery = rememberEvent(eventId);
            (handlers.get(eventType) || []).forEach((handler) => {
                handler(payload, { isFirstDelivery, eventId });
            });
        };

        const start = () => {
            // No user id, target or channel: the endpoint resolves the
            // subscription from the session by itself.
            source = new EventSource(config.eventsUrl, { withCredentials: true });

            source.addEventListener('open', () => {
                // Every open — the first and each browser-driven reconnect —
                // resyncs every live block, so events missed while offline or
                // during a Redis restart cannot leave stale state behind.
                openHandlers.forEach((handler) => handler());
            });

            SUBSCRIBED_EVENTS.forEach((eventType) => {
                source.addEventListener(eventType, (event) => dispatch(eventType, event));
            });

            source.addEventListener('error', () => {
                // EventSource reconnects on its own; a retry loop here would
                // fight it. Nothing is shown to the user and no markup removed.
                if (source && source.readyState === EventSource.CLOSED) {
                    bus.stop();
                }
            });
        };

        return { bus, start, get stopped() { return stopped; } };
    };

    // ---------------------------------------------------------------------
    // Module: notification bell and toasts (RT-3)
    // ---------------------------------------------------------------------

    const registerNotificationsModule = (bus) => {
        const region = document.querySelector('[data-toast-region]');
        const bell = () => window.qualityNotificationMenu || null;

        const trimToasts = () => {
            if (!region) {
                return;
            }
            const toasts = [...region.querySelectorAll('.toast')];
            while (toasts.length > MAX_VISIBLE_TOASTS) {
                toasts.shift().remove();
            }
        };

        const reducedMotion = window.matchMedia
            ? window.matchMedia('(prefers-reduced-motion: reduce)')
            : { matches: false };

        const showToast = ({ title, message, url }) => {
            if (!region) {
                return null;
            }
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.setAttribute('role', 'status');
            toast.tabIndex = -1;

            const heading = document.createElement('strong');
            heading.className = 'toast__title';
            // textContent everywhere: server text is inserted as text, never markup.
            heading.textContent = title;
            toast.append(heading);

            if (message) {
                const body = document.createElement('p');
                body.className = 'toast__message';
                body.textContent = message;
                toast.append(body);
            }

            const actions = document.createElement('div');
            actions.className = 'toast__actions';
            const link = document.createElement('a');
            link.className = 'toast__link';
            // The href always comes from Django-rendered markup.
            link.href = url;
            link.textContent = 'Открыть';
            actions.append(link);
            const close = document.createElement('button');
            close.type = 'button';
            close.className = 'toast__close';
            close.setAttribute('aria-label', 'Закрыть уведомление');
            close.textContent = '×';
            actions.append(close);
            toast.append(actions);

            let timer = null;
            const dismiss = () => {
                if (timer !== null) {
                    window.clearTimeout(timer);
                    timer = null;
                }
                toast.remove();
            };
            const startTimer = () => {
                if (timer === null) {
                    timer = window.setTimeout(dismiss, TOAST_TIMEOUT_MS);
                }
            };
            const pauseTimer = () => {
                if (timer !== null) {
                    window.clearTimeout(timer);
                    timer = null;
                }
            };

            close.addEventListener('click', dismiss);
            toast.addEventListener('mouseenter', pauseTimer);
            toast.addEventListener('mouseleave', startTimer);
            toast.addEventListener('focusin', pauseTimer);
            toast.addEventListener('focusout', startTimer);
            toast.addEventListener('keydown', (event) => {
                if (event.key === 'Escape') {
                    dismiss();
                }
            });

            if (reducedMotion.matches) {
                toast.classList.add('toast--no-motion');
            }
            region.append(toast);
            trimToasts();
            startTimer();
            return toast;
        };

        const showToastForNotification = (notificationId) => {
            const menu = bell();
            const item = menu
                ? menu.itemsContainer.querySelector(`[data-notification-id="${notificationId}"]`)
                : null;
            if (!item) {
                // Beyond the newest five: point at the full list rather than
                // inventing text.
                showToast({
                    title: 'Новое уведомление',
                    message: 'Откройте страницу уведомлений, чтобы прочитать его.',
                    url: bus.config.notificationsUrl,
                });
                return;
            }
            const titleNode = item.querySelector('[data-notification-title]');
            const messageNode = item.querySelector('[data-notification-message]');
            showToast({
                title: titleNode ? titleNode.textContent.trim() : 'Новое уведомление',
                message: messageNode ? messageNode.textContent.trim() : '',
                url: item.getAttribute('href') || bus.config.notificationsUrl,
            });
        };

        const coordinator = createRefreshCoordinator({
            url: bus.config.notificationFragmentUrl,
            apply(payload, toastIds) {
                const menu = bell();
                if (menu) {
                    if (typeof payload.items_html === 'string') {
                        menu.replaceItems(payload.items_html);
                    }
                    if (typeof payload.unread_count === 'number') {
                        menu.updateCounter(payload.unread_count);
                    }
                }
                toastIds.forEach(showToastForNotification);
            },
            onDenied: () => bus.stop(),
        });

        bus.onOpen(() => coordinator.schedule(null));

        bus.subscribe(EVENT_TYPES.NOTIFICATION_CREATED, (payload, meta) => {
            if (payload.resource_type !== 'notification' || !isPositiveInteger(payload.resource_id)) {
                return;
            }
            coordinator.schedule(meta.isFirstDelivery ? payload.resource_id : null);
        });

        bus.subscribe(EVENT_TYPES.NOTIFICATION_READ, (payload) => {
            if (payload.resource_type !== 'user') {
                return;
            }
            // Never a toast, and never derived from notification_ids:
            // `scope=all` deliberately reports only counts.
            coordinator.schedule(null);
        });

        return { showToast, coordinator };
    };

    // ---------------------------------------------------------------------
    // Module: task registry
    // ---------------------------------------------------------------------

    const registerTaskListModule = (bus) => {
        const container = document.querySelector('[data-live-task-list]');
        if (!container || !container.dataset.fragmentUrl) {
            return null;
        }

        const coordinator = createRefreshCoordinator({
            url: () => withCurrentQuery(container.dataset.fragmentUrl),
            apply(payload) {
                if (typeof payload.results_html === 'string') {
                    // Only the results are swapped: the tabs and the filter form
                    // stay untouched, so focus and typing survive.
                    container.innerHTML = payload.results_html;
                }
            },
        });

        bus.onOpen(() => coordinator.schedule(null));
        // Whether a task belongs in this tab and these filters is decided by the
        // Django queryset, never here.
        [EVENT_TYPES.TASK_CREATED, EVENT_TYPES.TASK_UPDATED, EVENT_TYPES.TASK_COMPLETED].forEach(
            (eventType) => bus.subscribe(eventType, () => coordinator.schedule(null)),
        );

        return { coordinator };
    };

    // ---------------------------------------------------------------------
    // Module: act registry
    // ---------------------------------------------------------------------

    const registerActRegistryModule = (bus) => {
        const container = document.querySelector('[data-live-act-registry]');
        if (!container || !container.dataset.fragmentUrl) {
            return null;
        }
        const kpis = container.querySelector('[data-live-act-registry-kpis]');
        const results = container.querySelector('[data-live-act-registry-results]');

        const coordinator = createRefreshCoordinator({
            url: () => withCurrentQuery(container.dataset.fragmentUrl),
            apply(payload) {
                // Scroll position and the filter form are untouched, so only the
                // two read-only blocks change under the user.
                if (kpis && typeof payload.kpis_html === 'string') {
                    kpis.innerHTML = payload.kpis_html;
                }
                if (results && typeof payload.results_html === 'string') {
                    results.innerHTML = payload.results_html;
                }
            },
        });

        bus.onOpen(() => coordinator.schedule(null));
        [EVENT_TYPES.ACT_UPDATED, EVENT_TYPES.ACT_STATUS_CHANGED].forEach((eventType) =>
            bus.subscribe(eventType, () => coordinator.schedule(null)),
        );

        return { coordinator };
    };

    // ---------------------------------------------------------------------
    // Module: open act page
    // ---------------------------------------------------------------------

    const registerActDetailModule = (bus) => {
        const config = document.querySelector('[data-live-act-config]');
        if (!config) {
            return null;
        }
        const actId = Number(config.dataset.liveActId);
        if (!isPositiveInteger(actId)) {
            return null;
        }

        const conflictBanner = document.querySelector('[data-act-conflict-banner]');
        const accessBanner = document.querySelector('[data-act-access-banner]');
        const reloadButton = document.querySelector('[data-act-conflict-reload]');
        if (reloadButton) {
            reloadButton.addEventListener('click', () => window.location.reload());
        }

        // -- dirty-state tracking ------------------------------------------
        //
        // Only a real user gesture marks a form dirty. A programmatic fragment
        // replacement dispatches nothing, so it can never raise a false
        // positive.
        let dirty = false;
        const markDirty = (event) => {
            // A programmatic `innerHTML` swap dispatches no events at all; an
            // explicitly synthetic one is ignored as well.
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
        // Adding or removing a dynamic analysis row is an edit too.
        document.addEventListener('click', (event) => {
            const target = event.target;
            if (!target || typeof target.closest !== 'function') {
                return;
            }
            if (target.closest('[data-add-root-analysis], [data-add-corrective-action], [data-remove-root-analysis], [data-remove-corrective-action]')) {
                markDirty(event);
            }
        });

        const showConflictBanner = () => {
            if (conflictBanner) {
                conflictBanner.hidden = false;
            }
        };

        const disableStaleWorkflowActions = () => {
            // Workflow submits may act on a status that no longer exists.
            // Ordinary fields and textareas stay editable so nothing typed is
            // lost, and the server-side status re-check remains the real guard.
            document
                .querySelectorAll('[data-workflow-submit]')
                .forEach((button) => {
                    button.disabled = true;
                });
        };

        const handleAccessLoss = () => {
            Object.values(blocks).forEach((block) => block && block.coordinator.stop());
            disableStaleWorkflowActions();
            if (accessBanner) {
                accessBanner.hidden = false;
            }
            if (conflictBanner) {
                conflictBanner.hidden = true;
            }
        };

        const makeBlock = (selector, urlKey) => {
            const element = document.querySelector(selector);
            const url = config.dataset[urlKey];
            if (!element || !url) {
                return null;
            }
            const coordinator = createRefreshCoordinator({
                url,
                apply(payload) {
                    if (typeof payload.html === 'string') {
                        element.innerHTML = payload.html;
                    }
                },
                onDenied: handleAccessLoss,
            });
            return { element, coordinator };
        };

        // Only blocks present on the current tab exist; opening another tab is
        // an ordinary server render and therefore already current.
        const blocks = {
            summary: makeBlock('[data-live-act-summary]', 'summaryUrl'),
            history: makeBlock('[data-live-act-history]', 'historyUrl'),
            comments: makeBlock('[data-live-act-comments]', 'commentsUrl'),
            activities: makeBlock('[data-live-act-activities]', 'activitiesUrl'),
        };

        const refresh = (names) => {
            names.forEach((name) => {
                const block = blocks[name];
                if (block) {
                    block.coordinator.schedule(null);
                }
            });
        };

        const isForThisAct = (payload, key) => Number(payload.data && payload.data[key]) === actId;

        bus.onOpen(() => refresh(['summary', 'history', 'comments', 'activities']));

        bus.subscribe(EVENT_TYPES.ACT_UPDATED, (payload) => {
            if (payload.resource_id !== actId) {
                return;
            }
            // Read-only blocks are always safe; the editable work area is not
            // replaced at all, so unsaved input cannot be lost.
            refresh(['summary']);
            if (dirty) {
                showConflictBanner();
            }
        });

        bus.subscribe(EVENT_TYPES.ACT_STATUS_CHANGED, (payload) => {
            if (payload.resource_id !== actId) {
                return;
            }
            refresh(['summary', 'history']);
            if (dirty) {
                showConflictBanner();
                disableStaleWorkflowActions();
            }
        });

        bus.subscribe(EVENT_TYPES.COMMENT_CREATED, (payload) => {
            if (!isForThisAct(payload, 'act_id')) {
                return;
            }
            refresh(['comments']);
        });

        [EVENT_TYPES.TASK_CREATED, EVENT_TYPES.TASK_UPDATED, EVENT_TYPES.TASK_COMPLETED].forEach(
            (eventType) =>
                bus.subscribe(eventType, (payload) => {
                    if (!isForThisAct(payload, 'act_id')) {
                        return;
                    }
                    refresh(['activities']);
                }),
        );

        return {
            blocks,
            get isDirty() {
                return dirty;
            },
        };
    };

    // ---------------------------------------------------------------------
    // Bootstrap
    // ---------------------------------------------------------------------

    const bootstrap = () => {
        const configElement = document.querySelector('[data-realtime-config]');
        if (!configElement || configElement.dataset.realtimeEnabled !== 'true') {
            return;
        }
        if (typeof window.EventSource !== 'function') {
            return;
        }
        // A second include must not open a second stream.
        if (window.__qualityRealtimeStarted) {
            return;
        }

        const config = {
            eventsUrl: configElement.dataset.eventsUrl,
            notificationFragmentUrl: configElement.dataset.notificationFragmentUrl,
            notificationsUrl: configElement.dataset.notificationsUrl || '/notifications/',
        };
        if (!config.eventsUrl || !config.notificationFragmentUrl) {
            return;
        }
        window.__qualityRealtimeStarted = true;

        const core = createCore(config);
        // Every module registers before the stream is opened, so an event that
        // arrives immediately after connecting always finds its handler.
        const modules = {
            notifications: registerNotificationsModule(core.bus),
            taskList: registerTaskListModule(core.bus),
            actRegistry: registerActRegistryModule(core.bus),
            actDetail: registerActDetailModule(core.bus),
        };
        window.__qualityRealtime = { bus: core.bus, modules };
        window.addEventListener('pagehide', () => core.bus.stop());
        core.start();
    };

    // The bell adapter is published by app.js on DOMContentLoaded. This script
    // is deferred, so it executes while readyState is still "interactive" —
    // registering the same listener guarantees every UI adapter exists before
    // the stream is opened and the first event is handled.
    if (document.readyState === 'complete') {
        bootstrap();
    } else {
        document.addEventListener('DOMContentLoaded', bootstrap);
    }
})();
