/**
 * Real-time core: the single EventSource, the event bus and the state machine.
 *
 * Everything else in `static/js/realtime/` is a feature module that registers
 * on this bus. There is exactly one EventSource per browser tab — and, when
 * BroadcastChannel is available, exactly one per user across all their tabs
 * (see tabs.js).
 *
 * The SSE stream is best-effort: it says *that* something changed, never what.
 * Recovery from anything missed is `/realtime/sync/` (see sync.js).
 *
 * No framework, no bundler, no third-party dependency.
 */
(() => {
    'use strict';

    if (window.QualityRealtime) {
        // A repeated include must not build a second core.
        return;
    }

    const SEEN_EVENT_LIMIT = 100;
    const REFRESH_DEBOUNCE_MS = 150;

    const EVENT_TYPES = {
        NOTIFICATION_CREATED: 'notification.created',
        NOTIFICATION_READ: 'notification.read',
        TASK_CREATED: 'task.created',
        TASK_UPDATED: 'task.updated',
        TASK_COMPLETED: 'task.completed',
        ACT_CREATED: 'act.created',
        ACT_UPDATED: 'act.updated',
        ACT_STATUS_CHANGED: 'act.status_changed',
        COMMENT_CREATED: 'comment.created',
    };

    const SUBSCRIBED_EVENTS = Object.keys(EVENT_TYPES).map((key) => EVENT_TYPES[key]);

    // Connection states. `degraded` means "the stream is not delivering, poll
    // instead"; `stopped` is terminal and only reached on 401/403 or logout.
    const STATES = {
        IDLE: 'idle',
        CONNECTING: 'connecting',
        LIVE: 'live',
        DEGRADED: 'degraded',
        OFFLINE: 'offline',
        STOPPED: 'stopped',
    };

    const isPositiveInteger = (value) => Number.isInteger(value) && value > 0;

    const readConfig = () => {
        const element = document.querySelector('[data-realtime-config]');
        if (!element || element.dataset.realtimeEnabled !== 'true') {
            return null;
        }
        const number = (name, fallback) => {
            const value = Number(element.dataset[name]);
            return Number.isFinite(value) && value > 0 ? value : fallback;
        };
        return {
            eventsUrl: element.dataset.eventsUrl,
            syncUrl: element.dataset.syncUrl,
            notificationFragmentUrl: element.dataset.notificationFragmentUrl,
            notificationsUrl: element.dataset.notificationsUrl || '/notifications/',
            degradedAfterSeconds: number('degradedAfterSeconds', 20),
            syncPollSeconds: number('syncPollSeconds', 30),
            syncHiddenPollSeconds: number('syncHiddenPollSeconds', 90),
            leaderLeaseSeconds: number('leaderLeaseSeconds', 12),
            leaderHeartbeatSeconds: number('leaderHeartbeatSeconds', 4),
        };
    };

    /**
     * One debounced, cancellable refresh pipeline for a single live block.
     *
     * A burst of events collapses into one request; only the newest request may
     * write to the DOM; a failed request leaves the current markup alone.
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

    const parsePayload = (raw) => {
        try {
            const payload = JSON.parse(raw);
            return payload && typeof payload === 'object' && !Array.isArray(payload)
                ? payload
                : null;
        } catch (error) {
            return null;
        }
    };

    // -----------------------------------------------------------------------

    const config = readConfig();
    const handlers = new Map();
    const openHandlers = [];
    const stateHandlers = [];
    const adapters = [];
    const seenIds = new Set();
    const seenOrder = [];

    let source = null;
    let state = STATES.IDLE;
    let degradeTimer = null;
    let started = false;

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

    const setState = (next) => {
        if (state === next || state === STATES.STOPPED) {
            return;
        }
        state = next;
        stateHandlers.forEach((handler) => handler(next));
    };

    const clearDegradeTimer = () => {
        if (degradeTimer !== null) {
            window.clearTimeout(degradeTimer);
            degradeTimer = null;
        }
    };

    const armDegradeTimer = () => {
        clearDegradeTimer();
        // No successful `open` within this window means the stream is not
        // working; the client says so and lets sync.js take over by polling.
        degradeTimer = window.setTimeout(() => {
            if (state === STATES.CONNECTING) {
                setState(STATES.DEGRADED);
            }
        }, config.degradedAfterSeconds * 1000);
    };

    /**
     * Hand one already-parsed event to every module.
     *
     * Used both by the EventSource listeners and by follower tabs replaying an
     * event the leader forwarded over BroadcastChannel.
     */
    const dispatch = (eventType, payload, options = {}) => {
        if (!payload || payload.event_type !== eventType) {
            return false;
        }
        const eventId = options.eventId || payload.event_id;
        const isFirstDelivery = rememberEvent(eventId);
        (handlers.get(eventType) || []).forEach((handler) => {
            handler(payload, { isFirstDelivery, eventId });
        });
        return isFirstDelivery;
    };

    const api = {
        STATES,
        EVENT_TYPES,
        SUBSCRIBED_EVENTS,
        config,
        createRefreshCoordinator,
        withCurrentQuery,
        isPositiveInteger,

        subscribe(eventType, handler) {
            if (!handlers.has(eventType)) {
                handlers.set(eventType, []);
            }
            handlers.get(eventType).push(handler);
        },
        onOpen(handler) {
            openHandlers.push(handler);
        },
        onState(handler) {
            stateHandlers.push(handler);
        },
        /** Register a block that must be refreshed on connect and on sync. */
        registerAdapter(adapter) {
            adapters.push(adapter);
            return adapter;
        },
        get adapters() {
            return adapters;
        },
        dispatch,
        get state() {
            return state;
        },
        setState,

        /** Called by tabs.js when this tab becomes the leader. */
        openStream() {
            if (source || state === STATES.STOPPED) {
                return;
            }
            if (typeof window.EventSource !== 'function') {
                // No EventSource at all: degraded from the start, polling only.
                setState(STATES.DEGRADED);
                return;
            }
            setState(STATES.CONNECTING);
            armDegradeTimer();
            // No user id, target or channel: the endpoint resolves the
            // subscription from the session by itself.
            source = new EventSource(config.eventsUrl, { withCredentials: true });

            source.addEventListener('open', () => {
                clearDegradeTimer();
                setState(STATES.LIVE);
                openHandlers.forEach((handler) => handler());
            });

            SUBSCRIBED_EVENTS.forEach((eventType) => {
                source.addEventListener(eventType, (event) => {
                    const payload = parsePayload(event.data);
                    const accepted = dispatch(eventType, payload, {
                        eventId: event.lastEventId || (payload && payload.event_id),
                    });
                    if (accepted && api.onLeaderEvent) {
                        // Forward to follower tabs, if there are any.
                        api.onLeaderEvent(eventType, payload);
                    }
                });
            });

            source.addEventListener('error', () => {
                if (source && source.readyState === EventSource.CLOSED) {
                    // The browser gave up (logout, for example).
                    source = null;
                    setState(STATES.DEGRADED);
                    return;
                }
                // A normal reconnect attempt: EventSource retries on its own and
                // nothing is shown to the user. Only a long silence degrades.
                if (state === STATES.LIVE) {
                    setState(STATES.CONNECTING);
                    armDegradeTimer();
                }
            });
        },
        closeStream() {
            clearDegradeTimer();
            if (source) {
                source.close();
                source = null;
            }
        },
        get hasStream() {
            return source !== null;
        },
        stop() {
            api.closeStream();
            setState(STATES.STOPPED);
            adapters.forEach((adapter) => adapter.stop && adapter.stop());
        },
        markStarted() {
            if (started) {
                return false;
            }
            started = true;
            return true;
        },
        parsePayload,
    };

    window.QualityRealtime = config ? api : null;
})();
