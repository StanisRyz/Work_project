/**
 * Multi-tab coordination: one SSE connection per user, not per tab.
 *
 * A leader tab holds the EventSource and the fallback polling; it forwards
 * normalized events and sync snapshots to the other tabs over BroadcastChannel.
 * Followers feed those straight into the same core bus, so every tab updates
 * its own fragments without opening a second stream.
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
    if (!core) {
        return;
    }

    const CHANNEL_NAME = 'quality-realtime-v1';
    const LEASE_KEY = 'quality-realtime-leader-v1';
    const MESSAGE_EVENT = 'event';
    const MESSAGE_SYNC = 'sync';

    const tabId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

    const storage = (() => {
        try {
            const probe = '__quality_realtime_probe__';
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
            return typeof window.BroadcastChannel === 'function'
                ? new window.BroadcastChannel(CHANNEL_NAME)
                : null;
        } catch (error) {
            return null;
        }
    })();

    const readLease = () => {
        if (!storage) {
            return null;
        }
        try {
            const raw = storage.getItem(LEASE_KEY);
            if (!raw) {
                return null;
            }
            const lease = JSON.parse(raw);
            return lease && typeof lease === 'object' ? lease : null;
        } catch (error) {
            return null;
        }
    };

    const writeLease = (lease) => {
        if (!storage) {
            return;
        }
        try {
            storage.setItem(LEASE_KEY, JSON.stringify(lease));
        } catch (error) {
            // A failed write only means this tab may lose the election.
        }
    };

    const clearLease = () => {
        if (!storage) {
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

    const leaseMs = core.config.leaderLeaseSeconds * 1000;
    const heartbeatMs = core.config.leaderHeartbeatSeconds * 1000;

    const becomeLeader = () => {
        if (isLeader) {
            return;
        }
        isLeader = true;
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

    const startCoordinated = () => {
        renewOrElect();
        heartbeatTimer = window.setInterval(renewOrElect, heartbeatMs);
    };

    // -- message passing ---------------------------------------------------

    const post = (message) => {
        if (!channel) {
            return;
        }
        try {
            channel.postMessage(message);
        } catch (error) {
            // A closed channel just means nobody is listening any more.
        }
    };

    // Only already-public event payloads and sync snapshots travel here: the
    // same data the server would hand this user anyway, never anything else.
    core.onLeaderEvent = (eventType, payload) => post({ kind: MESSAGE_EVENT, eventType, payload });
    core.onLeaderSync = (snapshot) => post({ kind: MESSAGE_SYNC, snapshot });

    if (channel) {
        channel.addEventListener('message', (message) => {
            const data = message && message.data;
            if (!data || typeof data !== 'object') {
                return;
            }
            if (data.kind === MESSAGE_EVENT) {
                core.dispatch(data.eventType, data.payload);
            } else if (data.kind === MESSAGE_SYNC && core.applySyncSnapshot) {
                core.applySyncSnapshot(data.snapshot, { fromLeader: true });
            }
        });
    }

    window.addEventListener('pagehide', () => {
        if (heartbeatTimer !== null) {
            window.clearInterval(heartbeatTimer);
            heartbeatTimer = null;
        }
        clearLease();
        if (channel) {
            try {
                channel.close();
            } catch (error) {
                // Already closed.
            }
        }
    });

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
            // per user.
            isLeader = true;
            core.openStream();
        },
        renewOrElect,
    };
})();
