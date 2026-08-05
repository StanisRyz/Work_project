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
 *
 * **Recovery has exactly one owner per user.** Every periodic server request
 * in this module — fallback polling and the safety-sync alike — is gated on
 * `ownsRecovery()`. A follower learns the leader's connection state over
 * BroadcastChannel so its degraded indicator stays correct, but it must never
 * turn that shared state into its own request loop: N tabs would otherwise
 * multiply one user's recovery traffic by N. A follower's only server request
 * is the single handshake fallback in tabs.js.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('sync')) {
        // A repeated include would arm a second set of timers and register a
        // second copy of every listener below.
        return;
    }

    let lastRevisions = null;
    let lastSnapshot = null;
    let lastSuccessfulSyncAt = null;
    let inFlight = null;
    let inFlightController = null;
    let pollTimer = null;
    let polling = false;
    let liveSafetyTimer = null;
    let stopped = false;

    /**
     * Does this tab own recovery for the user?
     *
     * The leader when tabs are coordinated; every tab when they are not
     * (no BroadcastChannel or no localStorage means each tab holds its own
     * EventSource and must recover for itself). A coordinated follower never
     * owns recovery — its leader does it once on everybody's behalf.
     */
    const ownsRecovery = () => !core.tabs || !core.tabs.isCoordinated || core.tabs.isLeader;

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
        inFlightController = controller;
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
                inFlightController = null;
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
        if (!ownsRecovery()) {
            // A follower shares the leader's degraded state for the indicator,
            // but must not turn it into its own request loop.
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
    // Only while `live`, only the tab that owns recovery, and never more often
    // than the configured interval.

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
        if (!ownsRecovery()) {
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
        if (stopped || core.state !== core.STATES.LIVE || !ownsRecovery()) {
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
                // Both are no-ops on a follower: `startPolling` refuses, and a
                // follower's snapshots arrive from the leader instead.
                startPolling();
                if (ownsRecovery()) {
                    runSync();
                }
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
            if (inFlightController) {
                // Cancel the request in flight instead of letting it land on a
                // client that has already shut down.
                inFlightController.abort();
                inFlightController = null;
                inFlight = null;
            }
        }
    });

    // Set by tabs.js's leader-election logic so every periodic request only
    // ever runs on the tab that owns recovery. A promoted tab takes over the
    // whole job: the fresh EventSource it just opened resyncs through its own
    // `open` handler, and if the stream is already known to be degraded it
    // must also pick up the fallback polling the previous leader was doing.
    core.onLeaderPromoted = () => {
        scheduleLiveSafetyTimer();
        if (core.state === core.STATES.DEGRADED) {
            startPolling();
        }
    };
    // Demotion does not move the state machine on its own, so a stepped-down
    // tab has to be told explicitly to stop doing recovery work.
    core.onLeaderDemoted = () => {
        clearLiveSafetyTimer();
        stopPolling();
    };

    document.addEventListener('visibilitychange', () => {
        if (stopped || document.visibilityState !== 'visible') {
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
        get ownsRecovery() {
            return ownsRecovery();
        },
    };
})();
