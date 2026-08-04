/**
 * Boot the real-time client once every module has registered.
 *
 * Loaded last on purpose: by the time this runs, core.js exists and
 * notifications/tasks/acts/sync have all subscribed, so an event arriving
 * immediately after the stream opens always finds its handler.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core) {
        return;
    }

    const indicator = document.querySelector('[data-realtime-status]');

    const start = () => {
        if (!core.markStarted()) {
            return;
        }
        if (indicator) {
            core.onState((state) => {
                // Only a real degradation is announced — a short reconnect
                // attempt stays silent, so the status is not screen-reader spam.
                indicator.hidden = state !== core.STATES.DEGRADED && state !== core.STATES.OFFLINE;
            });
        }
        if (navigator && navigator.onLine === false) {
            core.setState(core.STATES.OFFLINE);
        }
        core.tabs.start();
        window.addEventListener('pagehide', () => core.closeStream());
    };

    // app.js publishes the bell adapter on DOMContentLoaded; these scripts are
    // deferred, so waiting for the same signal removes the initialisation race.
    if (document.readyState === 'complete') {
        start();
    } else {
        document.addEventListener('DOMContentLoaded', start);
    }
})();
