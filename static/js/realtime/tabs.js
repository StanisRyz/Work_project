/**
 * Multi-tab coordination: one SSE connection per authenticated session.
 *
 * A leader tab holds the EventSource and the fallback polling; it forwards
 * normalized events and sync snapshots to the other tabs over BroadcastChannel.
 * Followers feed those straight into the same core bus, so every tab updates
 * its own fragments without opening a second stream.
 *
 * A follower also asks the leader for its current snapshot on start
 * (`sync.request` / `sync.response`) instead of waiting for the next event or
 * periodic sync to happen to arrive, and learns the leader's connection state
 * (`leader.state`) so its own degraded indicator stays correct without ever
 * opening a stream of its own.
 *
 * The lease lives in localStorage. Two leaders for a moment is acceptable — the
 * core's event-id deduplication is what prevents a duplicate toast.
 *
 * When BroadcastChannel or localStorage is unavailable, every tab simply works
 * on its own. Missing browser APIs must never disable real-time.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('tabs')) {
        // A repeated include must not replace `core.tabs`: the running
        // instance holds this tab's leader state, and a fresh one would
        // report `isLeader === false` while the stream is still open here.
        return;
    }

    const COORDINATION_SCHEMA_VERSION = 1;
    const coordinationEpoch = core.config.coordinationEpoch;
    const namespace = coordinationEpoch ? `quality-realtime-v1:${coordinationEpoch}` : '';
    const CHANNEL_NAME = namespace || null;
    const LEASE_KEY = namespace ? `${namespace}:leader` : null;
    const MESSAGE_EVENT = 'event';
    const MESSAGE_SYNC = 'sync';
    const MESSAGE_SYNC_REQUEST = 'sync.request';
    const MESSAGE_SYNC_RESPONSE = 'sync.response';
    const MESSAGE_LEADER_STATE = 'leader.state';

    // How long a follower waits for the leader to answer a `sync.request`
    // before falling back to exactly one `/realtime/sync/` of its own.
    const HANDSHAKE_TIMEOUT_MS = 1500;

    // `leader.state` only ever needs to drive a follower's degraded
    // indicator (task requirement: live/degraded/offline). A leader's
    // `stopped` is deliberately not forwarded: each tab discovers a lost
    // session independently through its own requests, so one tab's shutdown
    // can never short-circuit another tab that might still be fine.
    const FORWARDABLE_STATES = [
        core.STATES.CONNECTING,
        core.STATES.LIVE,
        core.STATES.DEGRADED,
        core.STATES.OFFLINE,
    ];

    const tabId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

    const storage = (() => {
        try {
            const probe = `${namespace || 'quality-realtime-standalone'}:probe:${tabId}`;
            window.localStorage.setItem(probe, '1');
            window.localStorage.removeItem(probe);
            return window.localStorage;
        } catch (error) {
            // Private mode, disabled storage, quota — never fatal.
            return null;
        }
    })();

    const channel = (() => {
        try {
            return CHANNEL_NAME && typeof window.BroadcastChannel === 'function'
                ? new window.BroadcastChannel(CHANNEL_NAME)
                : null;
        } catch (error) {
            return null;
        }
    })();

    const readLease = () => {
        if (!storage || !LEASE_KEY) {
            return null;
        }
        try {
            const raw = storage.getItem(LEASE_KEY);
            if (!raw) {
                return null;
            }
            const lease = JSON.parse(raw);
            if (!lease || typeof lease !== 'object' || Array.isArray(lease)) {
                return null;
            }
            if (
                lease.schema_version !== COORDINATION_SCHEMA_VERSION
                || lease.coordination_epoch !== coordinationEpoch
                || typeof lease.tab_id !== 'string'
                || !lease.tab_id
                || typeof lease.expires_at !== 'number'
                || !Number.isFinite(lease.expires_at)
            ) {
                return null;
            }
            return lease;
        } catch (error) {
            return null;
        }
    };

    const writeLease = (lease) => {
        if (!storage || !LEASE_KEY) {
            return;
        }
        try {
            storage.setItem(
                LEASE_KEY,
                JSON.stringify({
                    ...lease,
                    schema_version: COORDINATION_SCHEMA_VERSION,
                    coordination_epoch: coordinationEpoch,
                }),
            );
        } catch (error) {
            // A failed write only means this tab may lose the election.
        }
    };

    const clearLease = () => {
        if (!storage || !LEASE_KEY) {
            return;
        }
        try {
            const lease = readLease();
            if (lease && lease.tab_id === tabId) {
                storage.removeItem(LEASE_KEY);
            }
        } catch (error) {
            // Nothing to do: the lease expires on its own.
        }
    };

    let isLeader = false;
    let heartbeatTimer = null;
    let pendingRequest = null; // { requestId, timeoutId }
    let messageListener = null;
    let channelClosed = false;

    const leaseMs = core.config.leaderLeaseSeconds * 1000;
    const heartbeatMs = core.config.leaderHeartbeatSeconds * 1000;

    const clearPendingRequest = () => {
        if (pendingRequest && pendingRequest.timeoutId !== null) {
            window.clearTimeout(pendingRequest.timeoutId);
        }
        pendingRequest = null;
    };

    const becomeLeader = () => {
        if (isLeader) {
            return;
        }
        isLeader = true;
        // No longer relevant: we are about to get our own stream and sync.
        clearPendingRequest();
        writeLease({ tab_id: tabId, expires_at: Date.now() + leaseMs });
        core.openStream();
        if (core.onLeaderPromoted) {
            core.onLeaderPromoted();
        }
    };

    const renewOrElect = () => {
        if (core.state === core.STATES.STOPPED) {
            return;
        }
        const lease = readLease();
        const now = Date.now();

        if (isLeader) {
            if (lease && lease.tab_id !== tabId && Number(lease.expires_at) > now) {
                // Somebody else won a race: step down cleanly.
                isLeader = false;
                core.closeStream();
                if (core.onLeaderDemoted) {
                    core.onLeaderDemoted();
                }
                return;
            }
            writeLease({ tab_id: tabId, expires_at: now + leaseMs });
            return;
        }

        if (!lease || Number(lease.expires_at) <= now) {
            // A small random delay makes a simultaneous grab less likely; a
            // brief double leader is harmless because events are deduplicated.
            window.setTimeout(() => {
                const current = readLease();
                if (!current || Number(current.expires_at) <= Date.now()) {
                    becomeLeader();
                }
            }, Math.floor(Math.random() * 250));
        }
    };

    // -- follower → leader snapshot handshake -------------------------------

    const requestSnapshotFromLeader = () => {
        if (!channel) {
            return;
        }
        const requestId = `${tabId}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
        const timeoutId = window.setTimeout(() => {
            if (!pendingRequest || pendingRequest.requestId !== requestId) {
                return;
            }
            pendingRequest = null;
            // The leader never answered: perform exactly one sync of our own
            // instead of guessing. Never open a stream and never start
            // persistent polling — the leader is still assumed active.
            if (core.state !== core.STATES.STOPPED && core.sync) {
                core.sync.run();
            }
        }, HANDSHAKE_TIMEOUT_MS);
        pendingRequest = { requestId, timeoutId };
        post({ kind: MESSAGE_SYNC_REQUEST, request_id: requestId, tab_id: tabId });
    };

    const handleSyncRequest = (data) => {
        if (!isLeader) {
            return;
        }
        if (typeof data.request_id !== 'string' || !data.request_id) {
            return;
        }
        if (typeof data.tab_id !== 'string' || !data.tab_id) {
            return;
        }
        const snapshot = core.sync && core.sync.lastSnapshot;
        if (snapshot) {
            // Exactly what `/realtime/sync/` would hand this user anyway: no
            // user id, no channel name, no HTML, no business object content.
            post({
                kind: MESSAGE_SYNC_RESPONSE,
                request_id: data.request_id,
                target_tab_id: data.tab_id,
                snapshot,
            });
        }
        // Always report connection state, even with no snapshot yet, so the
        // follower's degraded indicator stays correct from the start.
        post({ kind: MESSAGE_LEADER_STATE, state: core.state });
    };

    const handleSyncResponse = (data) => {
        if (!pendingRequest || data.request_id !== pendingRequest.requestId) {
            return;
        }
        if (data.target_tab_id !== tabId) {
            return;
        }
        if (!core.isValidSyncSnapshot(data.snapshot)) {
            // Malformed or carrying unknown fields: ignore and let the
            // handshake timeout fall back to a one-shot sync of our own.
            return;
        }
        clearPendingRequest();
        core.applySyncSnapshot(data.snapshot, { fromLeader: true });
    };

    const handleLeaderState = (data) => {
        if (isLeader) {
            // Trust our own state machine, not a stray broadcast.
            return;
        }
        if (typeof data.state !== 'string' || !FORWARDABLE_STATES.includes(data.state)) {
            return;
        }
        // `setState` only updates the state and notifies listeners (the
        // degraded indicator, for one) — it never opens a stream itself, so a
        // follower never creates its own EventSource from this alone.
        core.setState(data.state);
    };

    const startCoordinated = () => {
        const lease = readLease();
        if (lease && lease.tab_id !== tabId && Number(lease.expires_at) > Date.now()) {
            // A leader is already active: ask it for a snapshot immediately
            // instead of waiting for the next event or periodic sync.
            requestSnapshotFromLeader();
        }
        renewOrElect();
        heartbeatTimer = window.setInterval(renewOrElect, heartbeatMs);
    };

    // -- message passing ---------------------------------------------------

    const post = (message) => {
        if (!channel || channelClosed) {
            return;
        }
        try {
            channel.postMessage({
                ...message,
                schema_version: COORDINATION_SCHEMA_VERSION,
                coordination_epoch: coordinationEpoch,
            });
        } catch (error) {
            // A closed channel just means nobody is listening any more.
        }
    };

    // Only already-public event payloads and sync snapshots travel here: the
    // same data the server would hand this user anyway, never anything else.
    core.onLeaderEvent = (eventType, payload) => post({ kind: MESSAGE_EVENT, eventType, payload });
    core.onLeaderSync = (snapshot) => post({ kind: MESSAGE_SYNC, snapshot });
    core.onState((state) => {
        if (state === core.STATES.STOPPED) {
            // Terminal: release every coordination resource this tab owns, so
            // nothing keeps ticking or listening against a dead session.
            releaseCoordination();
            return;
        }
        if (isLeader) {
            post({ kind: MESSAGE_LEADER_STATE, state });
        }
    });

    if (channel) {
        messageListener = (message) => {
            const data = message && message.data;
            if (
                !data
                || typeof data !== 'object'
                || Array.isArray(data)
                || channelClosed
                || data.schema_version !== COORDINATION_SCHEMA_VERSION
                || data.coordination_epoch !== coordinationEpoch
            ) {
                return;
            }
            if (data.kind === MESSAGE_EVENT) {
                core.dispatch(data.eventType, data.payload);
            } else if (data.kind === MESSAGE_SYNC && core.applySyncSnapshot) {
                core.applySyncSnapshot(data.snapshot, { fromLeader: true });
            } else if (data.kind === MESSAGE_SYNC_REQUEST) {
                handleSyncRequest(data);
            } else if (data.kind === MESSAGE_SYNC_RESPONSE) {
                handleSyncResponse(data);
            } else if (data.kind === MESSAGE_LEADER_STATE) {
                handleLeaderState(data);
            }
        };
        channel.addEventListener('message', messageListener);
    }

    /**
     * Release every timer, listener and channel this module owns.
     *
     * Idempotent on purpose: it runs on `pagehide` *and* when the client stops
     * after losing authentication, and either may happen first. Nothing here
     * may throw — a closed channel or an unavailable storage is an ordinary
     * outcome, not an error.
     */
    function releaseCoordination() {
        if (heartbeatTimer !== null) {
            window.clearInterval(heartbeatTimer);
            heartbeatTimer = null;
        }
        clearPendingRequest();
        clearLease();
        if (channel && !channelClosed) {
            channelClosed = true;
            try {
                if (messageListener && typeof channel.removeEventListener === 'function') {
                    channel.removeEventListener('message', messageListener);
                }
            } catch (error) {
                // Not every implementation supports removal; closing is enough.
            }
            try {
                channel.close();
            } catch (error) {
                // Already closed.
            }
        }
    }

    window.addEventListener('pagehide', releaseCoordination);

    core.tabs = {
        tabId,
        get isLeader() {
            return isLeader;
        },
        get isCoordinated() {
            return Boolean(channel && storage);
        },
        start() {
            if (channel && storage) {
                startCoordinated();
                return;
            }
            // No coordination available: this tab runs standalone, which is a
            // fully supported mode — just one stream per tab instead of one
            // per authenticated session.
            isLeader = true;
            core.openStream();
        },
        renewOrElect,
    };
})();
