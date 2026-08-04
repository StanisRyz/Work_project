/**
 * Recovery: `/realtime/sync/` snapshots and fallback polling.
 *
 * SSE is best-effort. Whenever the stream connects — first time or after any
 * reconnect — and whenever the client is degraded, this module asks the server
 * for opaque revision tokens and refreshes only the blocks whose token moved.
 * Identical tokens cost nothing: no fragment request is made at all.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core) {
        return;
    }

    let lastRevisions = null;
    let inFlight = null;
    let pollTimer = null;
    let polling = false;
    let stopped = false;

    const jitter = (seconds) => {
        // A little spread so many tabs do not poll in lockstep.
        const base = seconds * 1000;
        return base + Math.floor(Math.random() * Math.min(5000, base * 0.2));
    };

    const currentInterval = () => {
        const hidden = document.visibilityState === 'hidden';
        return jitter(hidden ? core.config.syncHiddenPollSeconds : core.config.syncPollSeconds);
    };

    const changedKeys = (revisions) => {
        if (!revisions || typeof revisions !== 'object') {
            return [];
        }
        if (!lastRevisions) {
            return Object.keys(revisions);
        }
        return Object.keys(revisions).filter((key) => revisions[key] !== lastRevisions[key]);
    };

    const applySnapshot = (snapshot, options = {}) => {
        if (!snapshot || typeof snapshot !== 'object') {
            return [];
        }
        const isFirst = lastRevisions === null;
        const changed = changedKeys(snapshot.revisions);
        lastRevisions = { ...(snapshot.revisions || {}) };

        if (!changed.length) {
            // Nothing moved: deliberately no fragment requests.
            return [];
        }
        core.adapters.forEach((adapter) => {
            if (!adapter.revisions || !adapter.refresh) {
                return;
            }
            const touched = adapter.revisions.some((key) => changed.includes(key));
            if (touched) {
                // Never a toast: a snapshot describes existing state, not news.
                adapter.refresh({ fromSync: true, isFirstSync: isFirst });
            }
        });
        if (!options.fromLeader && core.onLeaderSync) {
            core.onLeaderSync(snapshot);
        }
        return changed;
    };

    const runSync = () => {
        if (stopped || inFlight) {
            // Never more than one sync request at a time.
            return Promise.resolve(null);
        }
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        inFlight = controller || true;
        return fetch(core.config.syncUrl, {
            method: 'GET',
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            signal: controller ? controller.signal : undefined,
        })
            .then((response) => {
                if (response.status === 401 || response.status === 403) {
                    // Session gone: stop instead of hammering the server.
                    stopped = true;
                    stopPolling();
                    core.stop();
                    return null;
                }
                if (!response.ok) {
                    throw new Error(`Unexpected status ${response.status}`);
                }
                return response.json();
            })
            .then((snapshot) => {
                if (!snapshot) {
                    return null;
                }
                applySnapshot(snapshot);
                return snapshot;
            })
            .catch(() => null)
            .finally(() => {
                inFlight = null;
            });
    };

    const scheduleNextPoll = () => {
        if (!polling || stopped) {
            return;
        }
        pollTimer = window.setTimeout(() => {
            pollTimer = null;
            // An offline browser cannot reach the server; wait for `online`.
            if (navigator && navigator.onLine === false) {
                scheduleNextPoll();
                return;
            }
            runSync().finally(scheduleNextPoll);
        }, currentInterval());
    };

    const startPolling = () => {
        if (polling || stopped) {
            return;
        }
        polling = true;
        scheduleNextPoll();
    };

    function stopPolling() {
        polling = false;
        if (pollTimer !== null) {
            window.clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    // -- lifecycle ---------------------------------------------------------

    core.onOpen(() => {
        // A live stream makes polling pointless — and every reconnect resyncs.
        stopPolling();
        runSync();
    });

    core.onState((state) => {
        if (state === core.STATES.DEGRADED || state === core.STATES.OFFLINE) {
            if (state === core.STATES.DEGRADED) {
                startPolling();
                runSync();
            } else {
                stopPolling();
            }
        }
        if (state === core.STATES.LIVE) {
            stopPolling();
        }
        if (state === core.STATES.STOPPED) {
            stopped = true;
            stopPolling();
        }
    });

    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && polling) {
            // Reschedule at the visible-tab cadence straight away.
            if (pollTimer !== null) {
                window.clearTimeout(pollTimer);
                pollTimer = null;
            }
            runSync().finally(scheduleNextPoll);
        }
    });

    window.addEventListener('offline', () => {
        core.setState(core.STATES.OFFLINE);
    });
    window.addEventListener('online', () => {
        if (stopped) {
            return;
        }
        core.setState(core.STATES.CONNECTING);
        runSync();
        if (core.tabs && core.tabs.isLeader) {
            core.closeStream();
            core.openStream();
        }
    });

    core.applySyncSnapshot = applySnapshot;
    core.sync = {
        run: runSync,
        applySnapshot,
        startPolling,
        stopPolling,
        get isPolling() {
            return polling;
        },
        get revisions() {
            return lastRevisions;
        },
    };
})();
