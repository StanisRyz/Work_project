/**
 * Recovery: `/realtime/sync/` snapshots, fallback polling, and the live
 * safety-sync.
 *
 * SSE is best-effort. Whenever the stream connects — first time or after any
 * reconnect — and whenever the client is degraded, this module asks the server
 * for opaque revision tokens and refreshes only the blocks whose token moved.
 * Identical tokens cost nothing: no fragment request is made at all.
 *
 * While the stream stays `live`, a Redis event can still be published and
 * never delivered (a restart between publish and subscribe, for example).
 * The live safety-sync is a deliberately rare periodic `/realtime/sync/` call
 * — owned by the leader tab only when tabs are coordinated — that catches
 * exactly that case. It is not a polling replacement: fallback polling and the
 * safety timer are mutually exclusive, and the safety timer never runs more
 * often than `REALTIME_LIVE_SYNC_SECONDS`.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core) {
        return;
    }

    let lastRevisions = null;
    let lastSnapshot = null;
    let lastSuccessfulSyncAt = null;
    let inFlight = null;
    let pollTimer = null;
    let polling = false;
    let liveSafetyTimer = null;
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
        return core
            .requestJson(core.config.syncUrl, { signal: controller ? controller.signal : undefined })
            .then((result) => {
                if (!result.ok) {
                    if (result.kind === 'auth' || result.kind === 'redirect') {
                        // Session gone, or an endpoint that redirected when it
                        // never should have: stop instead of hammering the server.
                        stopped = true;
                        stopPolling();
                        clearLiveSafetyTimer();
                        core.stop();
                    } else if (result.kind === 'bad-content-type' || result.kind === 'malformed') {
                        core.warnUnexpectedResponse(core.config.syncUrl, result.kind);
                    }
                    // 401/redirect/malformed/network: `lastSuccessfulSyncAt` and
                    // `lastSnapshot` are left exactly as they were.
                    return null;
                }
                if (!core.isValidSyncSnapshot(result.data)) {
                    core.warnUnexpectedResponse(core.config.syncUrl, 'invalid-snapshot');
                    return null;
                }
                lastSuccessfulSyncAt = window.Date.now();
                lastSnapshot = result.data;
                applySnapshot(result.data);
                return result.data;
            })
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

    // -- live safety-sync ----------------------------------------------------
    //
    // Only while `live`, only the tab that actually owns this user's stream
    // (the leader when tabs are coordinated, every tab when they are not), and
    // never more often than the configured interval.

    const ownsLiveSafety = () => !core.tabs || !core.tabs.isCoordinated || core.tabs.isLeader;

    const dueForLiveSafetySync = () =>
        lastSuccessfulSyncAt === null ||
        window.Date.now() - lastSuccessfulSyncAt >= core.config.liveSyncSeconds * 1000;

    const maybeRunLiveSafetySync = () => {
        if (stopped || inFlight) {
            // Never parallel with an already-running sync.
            return;
        }
        if (core.state !== core.STATES.LIVE) {
            return;
        }
        if (!ownsLiveSafety()) {
            return;
        }
        if (!dueForLiveSafetySync()) {
            return;
        }
        runSync();
    };

    function clearLiveSafetyTimer() {
        if (liveSafetyTimer !== null) {
            window.clearInterval(liveSafetyTimer);
            liveSafetyTimer = null;
        }
    }

    const scheduleLiveSafetyTimer = () => {
        clearLiveSafetyTimer();
        if (stopped || core.state !== core.STATES.LIVE || !ownsLiveSafety()) {
            return;
        }
        liveSafetyTimer = window.setInterval(maybeRunLiveSafetySync, core.config.liveSyncSeconds * 1000);
    };

    // -- lifecycle ---------------------------------------------------------

    core.onOpen(() => {
        // A live stream makes polling pointless — and every reconnect resyncs.
        stopPolling();
        runSync();
    });

    core.onState((state) => {
        if (state === core.STATES.DEGRADED || state === core.STATES.OFFLINE) {
            clearLiveSafetyTimer();
            if (state === core.STATES.DEGRADED) {
                startPolling();
                runSync();
            } else {
                stopPolling();
            }
        }
        if (state === core.STATES.LIVE) {
            stopPolling();
            scheduleLiveSafetyTimer();
        }
        if (state === core.STATES.STOPPED) {
            stopped = true;
            stopPolling();
            clearLiveSafetyTimer();
        }
    });

    // Set by tabs.js's leader-election logic so the safety timer only ever
    // runs on the tab that actually owns the stream. Promotion always opens a
    // fresh EventSource first (see `openStream()`), and that connection's own
    // `open` handler already triggers an ordinary resync — so there is
    // nothing extra to run here beyond arming the timer for later. Demotion,
    // however, does not reset the state machine on its own, so clearing the
    // timer explicitly is what stops a demoted tab from ever ticking again.
    core.onLeaderPromoted = () => {
        scheduleLiveSafetyTimer();
    };
    core.onLeaderDemoted = () => {
        clearLiveSafetyTimer();
    };

    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState !== 'visible') {
            return;
        }
        if (polling) {
            // Reschedule at the visible-tab cadence straight away.
            if (pollTimer !== null) {
                window.clearTimeout(pollTimer);
                pollTimer = null;
            }
            runSync().finally(scheduleNextPoll);
        }
        if (core.state === core.STATES.LIVE) {
            // A no-op unless the previous successful sync is actually stale —
            // a short tab-switch must not trigger an extra request.
            maybeRunLiveSafetySync();
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
        get lastSnapshot() {
            return lastSnapshot;
        },
        get lastSuccessfulSyncAt() {
            return lastSuccessfulSyncAt;
        },
    };
})();
