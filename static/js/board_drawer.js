/**
 * The card drawer of a board page: opened, switched and closed without a
 * reload.
 *
 * Without this script every part of it is an ordinary link: a tile opens
 * `?card=<pk>`, a tab is `&tab=…`, «×» goes back to the sub-board, and the
 * server draws the page with the drawer open on the right tab. With it:
 *
 * - a click on a tile asks `boards:fragment` for that card (the tile's own
 *   query string — the server built it) and puts the answer's `drawer_html`
 *   into `<aside data-board-drawer>`; the address becomes the answer's
 *   `page_url` through `history.pushState`, so «назад» and «вперёд»
 *   (`popstate`) open and close the drawer the same way;
 * - a tab is switched in place: the `<aside>`'s `data-board-tab` says which
 *   body is shown (CSS does the rest), and the address and the live
 *   fragment's URL become the tab link's own, server-built ones, through
 *   `history.replaceState`. The attribute is outside every block the live
 *   client replaces, so a replacement never changes the tab;
 * - «×», Esc and a click on the dimmed part of the board close it;
 * - a tile clicked while a card is open opens the new card on the tab the
 *   drawer shows now («Лог» stays «Лог»); without JavaScript a tile opens
 *   «Описание», as its address says;
 * - «Карточка ZAP-12» in the heading copies the link to the card (the async
 *   clipboard, or the selection fallback on plain HTTP — never a browser
 *   dialog) and says so in `[data-board-message]`; with a modifier key, or
 *   without this script, it is an ordinary link.
 *
 * Unsaved input is never thrown away by any of this: while the page holds
 * some (`qualityUnsavedGuard`, the live client's own dirty flag), a tile, «×»
 * or the dimmed board is an ordinary navigation and the browser asks first.
 *
 * Nothing here is a rule. Every address comes from the server — the tile, the
 * tab links, the fragment's answer, the `data-board-*` of the page — and every
 * action in the drawer is a form or a link to a route that asks the right
 * again. The live client (`realtime/boards.js`) reads the addresses and the
 * fingerprints off `[data-board]`, which this script rewrites, and hears
 * `quality:board-drawer` after every open and close.
 */
(() => {
    'use strict';

    if (window.qualityBoardDrawer) {
        return;
    }

    const root = document.querySelector('[data-board]');
    const drawer = root && root.querySelector('[data-board-drawer]');
    const layout = root && root.querySelector('[data-board-layout]');
    if (!root || !drawer || !layout) {
        return;
    }
    const columns = root.querySelector('[data-live-board-columns]');
    const FAILED = 'Не удалось открыть карточку. Попробуйте ещё раз.';

    let request = null;   // the open in flight, aborted by a newer one
    let opener = null;    // the tile that opened the drawer, for the focus

    const hasUnsaved = () => {
        const guard = window.qualityUnsavedGuard;
        const live = window.QualityRealtime && window.QualityRealtime.boardLive;
        return Boolean((guard && guard.isDirty) || (live && live.isDirty));
    };

    const isOpen = () => !drawer.hidden;

    const showMessage = (text) => {
        const message = root.querySelector('[data-board-message]');
        if (message) {
            message.textContent = text;
            message.hidden = false;
        }
    };

    // -- «Карточка ZAP-12»: copy the link ------------------------------------

    /**
     * Copy `link` and say so. The plant's intranet is often plain HTTP, where
     * the async clipboard does not exist; the selection-copy fallback works
     * there, and if even that fails the link is shown to copy by hand.
     */
    const copyLink = (link, code) => {
        const done = () => showMessage(`Ссылка на карточку ${code} скопирована.`);
        const fallback = () => {
            const area = document.createElement('textarea');
            area.value = link;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            let copied = false;
            try {
                copied = document.execCommand('copy');
            } catch (error) {
                copied = false;
            }
            area.remove();
            if (copied) {
                done();
            } else {
                showMessage(`Ссылка на карточку ${code}: ${link}`);
            }
        };
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(link).then(done, fallback);
        } else {
            fallback();
        }
    };

    const announce = () => {
        document.dispatchEvent(new CustomEvent('quality:board-drawer', {
            detail: { open: isOpen(), panel: root.dataset.boardPanel || '' },
        }));
    };

    // -- the chat opens at its newest message ------------------------------

    const scrollChatToEnd = () => {
        const list = drawer.querySelector('[data-live-board-comments]');
        if (list) {
            list.scrollTop = list.scrollHeight;
        }
    };

    // -- what the page keeps about the open card ---------------------------

    const markTile = (cardId) => {
        if (columns) {
            columns.dataset.currentCard = cardId ? String(cardId) : '';
            columns.querySelectorAll('.board-tile--open').forEach((tile) => tile.classList.remove('board-tile--open'));
            const item = cardId ? columns.querySelector(`[data-card-id="${cardId}"]`) : null;
            const tile = item && item.querySelector('.board-tile');
            if (tile) {
                tile.classList.add('board-tile--open');
            }
        }
    };

    const syncFilter = (cardId, tab, resetUrl) => {
        const card = root.querySelector('[data-board-filter-card]');
        const tabField = root.querySelector('[data-board-filter-tab]');
        if (card) {
            card.value = cardId ? String(cardId) : '';
            card.disabled = !cardId;
        }
        if (tabField) {
            tabField.value = cardId ? tab || '' : '';
            tabField.disabled = !cardId;
        }
        const reset = root.querySelector('[data-board-filter-reset]');
        if (reset && resetUrl) {
            reset.setAttribute('href', resetUrl);
        }
        // A field filter chip's «×» keeps the open card and its tab, as the
        // form's hidden fields do: only those two parameters change.
        root.querySelectorAll('[data-board-filter-chip]').forEach((link) => {
            const url = new URL(link.getAttribute('href'), window.location.href);
            const params = new URLSearchParams();
            if (cardId) {
                params.set('card', String(cardId));
                params.set('tab', tab || 'description');
            }
            url.searchParams.forEach((value, name) => {
                if (name !== 'card' && name !== 'tab') {
                    params.append(name, value);
                }
            });
            const query = params.toString();
            link.setAttribute('href', url.pathname + (query ? `?${query}` : ''));
        });
    };

    // -- tabs ----------------------------------------------------------------

    const setTab = (name, { history: writeHistory = true } = {}) => {
        const link = drawer.querySelector(`[data-board-tab-link="${name}"]`);
        if (!link) {
            return false;
        }
        drawer.setAttribute('data-board-tab', name);
        drawer.querySelectorAll('[data-board-tab-link]').forEach((other) => {
            const active = other === link;
            other.classList.toggle('is-active', active);
            if (active) {
                other.setAttribute('aria-current', 'true');
            } else {
                other.removeAttribute('aria-current');
            }
        });
        const pageUrl = link.getAttribute('href');
        root.dataset.boardPageUrl = pageUrl;
        if (link.dataset.boardTabFragmentUrl) {
            root.dataset.boardFragmentUrl = link.dataset.boardTabFragmentUrl;
        }
        const tabField = root.querySelector('[data-board-filter-tab]');
        if (tabField && !tabField.disabled) {
            tabField.value = name;
        }
        if (writeHistory && window.history && typeof window.history.replaceState === 'function') {
            window.history.replaceState(window.history.state, '', pageUrl);
        }
        if (name === 'chat') {
            scrollChatToEnd();
        }
        return true;
    };

    // -- open and close ------------------------------------------------------

    const show = (payload) => {
        drawer.innerHTML = payload.drawer_html;
        drawer.setAttribute('data-board-tab', payload.tab || 'description');
        drawer.hidden = false;
        layout.classList.add('board-layout--with-panel');
        root.dataset.boardPanel = payload.panel;
        root.dataset.boardPageUrl = payload.page_url;
        root.dataset.boardFragmentUrl = payload.fragment_url;
        root.dataset.panelRevision = payload.panel_revision || '';
        root.dataset.commentsRevision = payload.comments_revision || '';
        root.dataset.logRevision = payload.log_revision || '';
        root.dataset.panelHoldsInput = 'false';
        markTile(payload.card_id);
        syncFilter(payload.card_id, payload.tab, payload.reset_url);
        const banner = document.querySelector('[data-board-conflict-banner]');
        if (banner) {
            banner.hidden = true;
        }
        if (window.qualityFragments) {
            window.qualityFragments.reinitialise(drawer);
        }
        if (payload.tab === 'chat') {
            scrollChatToEnd();
        }
        announce();
    };

    const close = ({ push = true } = {}) => {
        if (request) {
            request.abort();
            request = null;
        }
        drawer.hidden = true;
        drawer.innerHTML = '';
        layout.classList.remove('board-layout--with-panel');
        root.dataset.boardPanel = '';
        root.dataset.boardPageUrl = root.dataset.boardCloseUrl || root.dataset.boardUrl || '';
        root.dataset.boardFragmentUrl = root.dataset.boardCloseFragmentUrl || root.dataset.boardFragmentUrl;
        root.dataset.panelRevision = '';
        root.dataset.commentsRevision = '';
        root.dataset.logRevision = '';
        root.dataset.panelHoldsInput = 'false';
        markTile(null);
        syncFilter(null, '', root.dataset.boardUrl);
        const banner = document.querySelector('[data-board-conflict-banner]');
        if (banner) {
            banner.hidden = true;
        }
        if (push && window.history && typeof window.history.pushState === 'function') {
            window.history.pushState({ boardDrawer: 'closed' }, '', root.dataset.boardPageUrl);
        }
        announce();
        if (opener && typeof opener.focus === 'function' && columns && columns.contains(opener)) {
            opener.focus();
        }
        opener = null;
    };

    /**
     * Ask `boards:fragment` for the panel `search` names and show it.
     * `fallback` is where to go if that fails — an ordinary navigation, so
     * nothing is ever lost to a network error.
     */
    const open = (search, { push = true, fallback = '' } = {}) => {
        const base = root.dataset.boardFragmentBase;
        if (!base || typeof window.fetch !== 'function') {
            if (fallback) {
                window.location.assign(fallback);
            }
            return Promise.resolve(false);
        }
        if (request) {
            request.abort();
        }
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        request = controller;
        return window.fetch(base + search, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
            signal: controller ? controller.signal : undefined,
        })
            .then((response) => {
                const type = response.headers && typeof response.headers.get === 'function'
                    ? response.headers.get('Content-Type') || ''
                    : 'application/json';
                if (!response.ok || !type.includes('application/json')) {
                    throw new Error('fragment');
                }
                return response.json();
            })
            .then((payload) => {
                if (request !== controller) {
                    return false;
                }
                request = null;
                if (!payload.panel || typeof payload.drawer_html !== 'string') {
                    // The card is not on this sub-board (any more): the page
                    // the server draws for that address says what is there.
                    if (fallback) {
                        window.location.assign(fallback);
                    } else {
                        close({ push: false });
                    }
                    return false;
                }
                show(payload);
                if (push && window.history && typeof window.history.pushState === 'function') {
                    window.history.pushState({ boardDrawer: payload.card_id }, '', payload.page_url);
                }
                const head = drawer.querySelector('.board-drawer__title');
                if (head && typeof head.focus === 'function') {
                    head.setAttribute('tabindex', '-1');
                    head.focus({ preventScroll: true });
                }
                return true;
            })
            .catch((error) => {
                if (error && error.name === 'AbortError') {
                    return false;
                }
                request = null;
                if (fallback) {
                    window.location.assign(fallback);
                } else {
                    showMessage(FAILED);
                }
                return false;
            });
    };

    const plainClick = (event) =>
        event.button === 0 && !event.defaultPrevented
        && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;

    // -- clicks, delegated ---------------------------------------------------

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || typeof target.closest !== 'function' || !plainClick(event)) {
            return;
        }

        const tabLink = target.closest('[data-board-tab-link]');
        if (tabLink && drawer.contains(tabLink)) {
            // Switching a tab keeps every typed character: nothing navigates.
            if (setTab(tabLink.dataset.boardTabLink)) {
                event.preventDefault();
            }
            return;
        }

        const cardLink = target.closest('[data-board-card-link]');
        if (cardLink && drawer.contains(cardLink)) {
            event.preventDefault();
            copyLink(new URL(cardLink.getAttribute('href'), window.location.href).href, cardLink.dataset.boardCardLink);
            return;
        }

        const closer = target.closest('[data-board-drawer-close]');
        if (closer && drawer.contains(closer)) {
            if (!hasUnsaved()) {
                event.preventDefault();
                close();
            }
            return;
        }

        const tile = target.closest('a.board-tile');
        if (tile && columns && columns.contains(tile)) {
            if (hasUnsaved()) {
                return;
            }
            const url = new URL(tile.href, window.location.href);
            // Another card opens on the tab this one shows: the tile's own
            // address names none, so the drawer's current tab is added.
            const tab = isOpen() && root.dataset.boardPanel !== 'new'
                ? drawer.getAttribute('data-board-tab') : '';
            if (tab && tab !== 'description' && drawer.querySelector(`[data-board-tab-link="${tab}"]`)) {
                url.searchParams.set('tab', tab);
            }
            event.preventDefault();
            opener = tile;
            open(url.search, { fallback: url.href });
            return;
        }

        // A click on the dimmed board — not on anything that does something
        // of its own — closes the drawer.
        if (isOpen() && columns && columns.contains(target)
            && !target.closest('a, button, summary, details, input, select, textarea, label, form, [data-card-movable]')) {
            if (hasUnsaved()) {
                window.location.assign(root.dataset.boardCloseUrl || root.dataset.boardUrl);
                return;
            }
            close();
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || event.defaultPrevented || !isOpen()) {
            return;
        }
        const target = event.target;
        // Esc belongs to whatever is open above the drawer, and to a field
        // being typed in.
        if (document.querySelector('dialog[open], details[data-board-menu][open]')
            || root.getAttribute('data-board-busy') !== null
            || (target && typeof target.closest === 'function' && target.closest('input, textarea, select'))) {
            return;
        }
        if (hasUnsaved()) {
            window.location.assign(root.dataset.boardCloseUrl || root.dataset.boardUrl);
            return;
        }
        close();
    // Capturing: an open menu or dialog is still open when this runs, before
    // its own Esc closes it — so one Esc closes the menu, not the drawer too.
    }, true);

    // «назад» / «вперёд»: the address says which card, the fragment draws it.
    window.addEventListener('popstate', () => {
        const params = new URLSearchParams(window.location.search);
        if (params.get('card') || params.get('new')) {
            open(window.location.search, { push: false, fallback: window.location.href });
        } else if (isOpen()) {
            close({ push: false });
        }
    });

    // A page drawn with the drawer open on «Чат» starts at the newest message.
    const start = () => {
        if (isOpen() && drawer.getAttribute('data-board-tab') === 'chat') {
            scrollChatToEnd();
        }
    };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    window.qualityBoardDrawer = {
        open,
        close,
        setTab,
        /** Draw the open card again from the server (its column changed). */
        refresh() {
            const url = new URL(root.dataset.boardPageUrl || '', window.location.href);
            return isOpen() ? open(url.search, { push: false }) : Promise.resolve(false);
        },
        get isOpen() {
            return isOpen();
        },
    };
})();
