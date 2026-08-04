/**
 * Real-time notification client (RT-3).
 *
 * The SSE stream is a *signal*, never a data source: an event only tells this
 * client that something changed. Every piece of text and markup shown to the
 * user is fetched afterwards from an ordinary authenticated Django endpoint,
 * so permissions are re-checked server-side and no SSE payload is ever
 * inserted into the DOM.
 *
 * No framework, no bundler, no third-party dependency.
 */
(() => {
    'use strict';

    const config = document.querySelector('[data-realtime-config]');
    if (!config || config.dataset.realtimeEnabled !== 'true') {
        return;
    }
    if (typeof window.EventSource !== 'function') {
        return;
    }
    // A second <script> include must not open a second stream.
    if (window.__qualityRealtimeStarted) {
        return;
    }
    window.__qualityRealtimeStarted = true;

    const eventsUrl = config.dataset.eventsUrl;
    const fragmentUrl = config.dataset.notificationFragmentUrl;
    const notificationsUrl = config.dataset.notificationsUrl || '/notifications/';
    if (!eventsUrl || !fragmentUrl) {
        return;
    }

    const REFRESH_DEBOUNCE_MS = 150;
    const SEEN_EVENT_LIMIT = 100;
    const TOAST_TIMEOUT_MS = 8000;
    const MAX_VISIBLE_TOASTS = 3;

    const reducedMotion = window.matchMedia
        ? window.matchMedia('(prefers-reduced-motion: reduce)')
        : { matches: false };

    // ---------------------------------------------------------------------
    // Event de-duplication
    // ---------------------------------------------------------------------

    // A bounded FIFO: a redelivered event must not produce a second toast, but
    // the set must not grow without limit either.
    const seenIds = new Set();
    const seenOrder = [];

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

    // ---------------------------------------------------------------------
    // Bell DOM
    // ---------------------------------------------------------------------

    const bell = () => window.qualityNotificationMenu || null;

    const applyFragment = (payload) => {
        const menu = bell();
        if (!menu) {
            return;
        }
        if (typeof payload.items_html === 'string') {
            menu.replaceItems(payload.items_html);
        }
        if (typeof payload.unread_count === 'number') {
            menu.updateCounter(payload.unread_count);
        }
    };

    // ---------------------------------------------------------------------
    // Refresh coordinator
    // ---------------------------------------------------------------------

    let debounceTimer = null;
    let generation = 0;
    let inFlight = null;
    let stopped = false;
    const pendingToastIds = new Set();

    const runRefresh = () => {
        debounceTimer = null;
        if (stopped) {
            return;
        }
        // Only the newest request may write to the DOM: an older response that
        // arrives late is dropped rather than overwriting fresher state.
        generation += 1;
        const requestGeneration = generation;
        if (inFlight) {
            inFlight.abort();
        }
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        inFlight = controller;

        const toastIds = [...pendingToastIds];
        pendingToastIds.clear();

        fetch(fragmentUrl, {
            method: 'GET',
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            signal: controller ? controller.signal : undefined,
        })
            .then((response) => {
                if (response.status === 401 || response.status === 403) {
                    // Logged out or session expired: stop cleanly instead of
                    // retrying forever. A normal logout keeps working as before.
                    stop();
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
                applyFragment(payload);
                toastIds.forEach(showToastForNotification);
            })
            .catch(() => {
                // Keep the current markup and counter: a failed refresh must
                // never blank the menu or invent a count.
            })
            .finally(() => {
                if (inFlight === controller) {
                    inFlight = null;
                }
            });
    };

    const scheduleRefresh = (notificationId) => {
        if (stopped) {
            return;
        }
        if (notificationId) {
            pendingToastIds.add(notificationId);
        }
        if (debounceTimer !== null) {
            return;
        }
        // Several events in a burst collapse into one fragment request.
        debounceTimer = window.setTimeout(runRefresh, REFRESH_DEBOUNCE_MS);
    };

    // ---------------------------------------------------------------------
    // Toasts
    // ---------------------------------------------------------------------

    const region = document.querySelector('[data-toast-region]');

    const trimToasts = () => {
        if (!region) {
            return;
        }
        const toasts = [...region.querySelectorAll('.toast')];
        while (toasts.length > MAX_VISIBLE_TOASTS) {
            const oldest = toasts.shift();
            oldest.remove();
        }
    };

    const showToast = ({ title, message, url }) => {
        if (!region) {
            return;
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
        // The href always comes from Django-rendered markup, never from an SSE payload.
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
        // Reading or interacting must not race the auto-dismiss.
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
            // Beyond the five most recent, so the server markup has no entry for
            // it: point at the full list instead of inventing text.
            showToast({
                title: 'Новое уведомление',
                message: 'Откройте страницу уведомлений, чтобы прочитать его.',
                url: notificationsUrl,
            });
            return;
        }

        const titleNode = item.querySelector('[data-notification-title]');
        const messageNode = item.querySelector('[data-notification-message]');
        showToast({
            title: titleNode ? titleNode.textContent.trim() : 'Новое уведомление',
            message: messageNode ? messageNode.textContent.trim() : '',
            url: item.getAttribute('href') || notificationsUrl,
        });
    };

    // ---------------------------------------------------------------------
    // Event handling
    // ---------------------------------------------------------------------

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

    const isPositiveInteger = (value) => Number.isInteger(value) && value > 0;

    const handleNotificationCreated = (event) => {
        const payload = parsePayload(event);
        if (
            !payload
            || payload.event_type !== 'notification.created'
            || payload.resource_type !== 'notification'
            || !isPositiveInteger(payload.resource_id)
        ) {
            return;
        }
        const eventId = event.lastEventId || payload.event_id;
        if (!rememberEvent(eventId)) {
            // Already handled: no second toast, but a cheap resync is still safe.
            scheduleRefresh(null);
            return;
        }
        scheduleRefresh(payload.resource_id);
    };

    const handleNotificationRead = (event) => {
        const payload = parsePayload(event);
        if (
            !payload
            || payload.event_type !== 'notification.read'
            || payload.resource_type !== 'user'
        ) {
            return;
        }
        rememberEvent(event.lastEventId || payload.event_id);
        // Never a toast, and never derived from notification_ids: `scope=all`
        // deliberately reports only counts, so the fragment is the only truth.
        scheduleRefresh(null);
    };

    // ---------------------------------------------------------------------
    // Stream lifecycle
    // ---------------------------------------------------------------------

    let source = null;
    let openedOnce = false;

    function stop() {
        stopped = true;
        if (debounceTimer !== null) {
            window.clearTimeout(debounceTimer);
            debounceTimer = null;
        }
        if (inFlight) {
            inFlight.abort();
            inFlight = null;
        }
        if (source) {
            source.close();
            source = null;
        }
    }

    const start = () => {
        // No user id, target or channel: the endpoint resolves the subscription
        // from the session by itself.
        source = new EventSource(eventsUrl, { withCredentials: true });

        source.addEventListener('open', () => {
            if (!openedOnce) {
                openedOnce = true;
            }
            // Every open — the first one and every browser-driven reconnect —
            // resyncs, so events missed while offline or during a Redis restart
            // cannot leave a stale counter behind. Never toasts: the queue is
            // empty here, so existing notifications stay silent.
            scheduleRefresh(null);
        });

        source.addEventListener('notification.created', handleNotificationCreated);
        source.addEventListener('notification.read', handleNotificationRead);
        // Any other event type is ignored on purpose — no listener, no error.

        source.addEventListener('error', () => {
            // EventSource reconnects on its own; adding a retry loop here would
            // fight it. Nothing is shown to the user and no markup is removed.
            if (source && source.readyState === EventSource.CLOSED) {
                // The browser gave up (for example after a logout): stay quiet.
                stop();
            }
        });
    };

    window.addEventListener('pagehide', stop);
    start();
})();
