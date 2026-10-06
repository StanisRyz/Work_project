/*
 * Registry conveniences, the same for every registry. Presentation only: the
 * list is still whatever the server's state builder returns for the query
 * string, and nothing here filters, sorts or hides a row by itself.
 *
 * - `form[data-registry-filter]` applies itself: a list box, a date or a tick
 *   box on `change`, the search box — and any field marked
 *   `[data-registry-delayed]` (a number, a text of a board's field filter) —
 *   half a second after the user stops typing. «Применить» stays as the
 *   no-JavaScript path. The caret is put back into the field that was being
 *   typed in after the reload, so typing simply continues, and a
 *   `details[data-registry-keep-open]` of the form that was open when it
 *   applied itself is opened again. An empty `[data-registry-omit-empty]`
 *   field is left out of the address the form submits.
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
    const OPEN_KEY = 'quality-registry-open-details';

    document.querySelectorAll('form[data-registry-filter]').forEach((form) => {
        let timer = null;
        const panels = () => [...form.querySelectorAll('details[data-registry-keep-open]')];
        const submit = () => {
            window.clearTimeout(timer);
            if (session) {
                // Which of the form's panels were open, to open them again
                // on the page the filter leads to.
                const open = panels().map((panel, index) => (panel.open ? index : -1)).filter((index) => index >= 0);
                if (open.length) {
                    session.setItem(OPEN_KEY, JSON.stringify({ path: window.location.pathname, open }));
                } else {
                    session.removeItem(OPEN_KEY);
                }
            }
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

        const delayed = [...form.querySelectorAll('[data-registry-search], [data-registry-delayed]')];
        delayed.forEach((field) => {
            field.addEventListener('input', () => {
                window.clearTimeout(timer);
                timer = window.setTimeout(() => {
                    if (session) {
                        session.setItem(FOCUS_KEY, JSON.stringify({
                            path: window.location.pathname,
                            name: field.getAttribute('name') || '',
                        }));
                    }
                    submit();
                }, SEARCH_DELAY_MS);
            });
        });
        // An explicit Enter or «Применить» should not fire a second time. A
        // field marked `[data-registry-omit-empty]` that is empty is left out
        // of the address (a board's many field inputs would otherwise spell
        // `f_3_from=&f_3_to=&…`); it is enabled again if the page comes back
        // from the browser's history cache.
        form.addEventListener('submit', () => {
            window.clearTimeout(timer);
            form.querySelectorAll('[data-registry-omit-empty]').forEach((field) => {
                if (!field.disabled && field.value.trim() === '') {
                    field.disabled = true;
                    field.dataset.registryOmitted = 'true';
                }
            });
        });
        window.addEventListener('pageshow', () => {
            form.querySelectorAll('[data-registry-omitted]').forEach((field) => {
                field.disabled = false;
                delete field.dataset.registryOmitted;
            });
        });

        if (session) {
            let opened = null;
            try {
                opened = JSON.parse(session.getItem(OPEN_KEY) || 'null');
            } catch (error) {
                opened = null;
            }
            if (opened && opened.path === window.location.pathname && Array.isArray(opened.open)) {
                session.removeItem(OPEN_KEY);
                const all = panels();
                opened.open.forEach((index) => {
                    if (all[index]) {
                        all[index].open = true;
                    }
                });
            }

            let focus = null;
            const stored = session.getItem(FOCUS_KEY);
            try {
                focus = JSON.parse(stored || 'null');
            } catch (error) {
                // The older spelling: the bare path, meaning the search box.
                focus = stored ? { path: stored, name: '' } : null;
            }
            if (focus && focus.path === window.location.pathname) {
                session.removeItem(FOCUS_KEY);
                const field = delayed.find((item) => (
                    focus.name ? item.getAttribute('name') === focus.name : item.matches('[data-registry-search]')
                ));
                if (field) {
                    field.focus();
                    const end = field.value.length;
                    try {
                        field.setSelectionRange(end, end);
                    } catch (error) {
                        // `type="search"` and `type="text"` support it everywhere that matters.
                    }
                }
            }
        }
    });
})();
