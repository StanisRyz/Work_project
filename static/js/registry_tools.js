/*
 * Registry conveniences, the same for every registry. Presentation only: the
 * list is still whatever the server's state builder returns for the query
 * string, and nothing here filters, sorts or hides a row by itself.
 *
 * - `form[data-registry-filter]` applies itself: a list box on `change`, the
 *   search box half a second after the user stops typing. «Применить» stays
 *   as the no-JavaScript path. The caret is put back into the search box after
 *   the reload, so typing simply continues.
 * - `[data-registry-memory="<name>:<user id>"]` remembers the query string the
 *   registry was last shown with and reopens it when the registry is entered
 *   bare (the sidebar link, the topbar). Per user, per browser, in
 *   `localStorage`; a URL that already carries a query always wins, and
 *   «Сбросить» therefore resets what is remembered too.
 */
(function () {
    'use strict';

    const SEARCH_DELAY_MS = 600;
    const FOCUS_KEY = 'quality-registry-search-focus';

    function storage(kind) {
        try {
            const store = window[kind];
            const probe = '__quality_probe__';
            store.setItem(probe, probe);
            store.removeItem(probe);
            return store;
        } catch (error) {
            return null;
        }
    }

    // ------------------------------------------------------------ memory
    const memory = document.querySelector('[data-registry-memory]');
    const local = storage('localStorage');
    if (memory && local) {
        const key = `quality-registry:v1:${memory.dataset.registryMemory}`;
        const query = window.location.search;
        if (!query) {
            const remembered = local.getItem(key);
            if (remembered && remembered !== '?') {
                window.location.replace(window.location.pathname + remembered);
                return;
            }
        } else {
            local.setItem(key, query);
        }
    }

    // ------------------------------------------------------------ filters
    const session = storage('sessionStorage');

    document.querySelectorAll('form[data-registry-filter]').forEach((form) => {
        let timer = null;
        const submit = () => {
            window.clearTimeout(timer);
            if (typeof form.requestSubmit === 'function') {
                form.requestSubmit();
            } else {
                form.submit();
            }
        };

        form.addEventListener('change', (event) => {
            if (event.target.matches('select, input[type="date"], input[type="checkbox"]')) {
                submit();
            }
        });

        const search = form.querySelector('[data-registry-search]');
        if (search) {
            search.addEventListener('input', () => {
                window.clearTimeout(timer);
                timer = window.setTimeout(() => {
                    if (session) {
                        session.setItem(FOCUS_KEY, window.location.pathname);
                    }
                    submit();
                }, SEARCH_DELAY_MS);
            });
            // An explicit Enter or «Применить» should not fire a second time.
            form.addEventListener('submit', () => window.clearTimeout(timer));

            if (session && session.getItem(FOCUS_KEY) === window.location.pathname) {
                session.removeItem(FOCUS_KEY);
                search.focus();
                const end = search.value.length;
                try {
                    search.setSelectionRange(end, end);
                } catch (error) {
                    // `type="search"` supports it everywhere that matters.
                }
            }
        }
    });
})();
