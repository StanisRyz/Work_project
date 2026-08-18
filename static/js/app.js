/**
 * Registry of UI initialisers that must run again after a live fragment
 * replacement.
 *
 * The same functions run on first page load and after every swap, so there is
 * exactly one implementation of each behaviour. Initialisers must be
 * idempotent: they mark what they have already wired and skip it next time.
 * No business validation lives here — that stays on the server.
 */
window.qualityFragments = (() => {
    const initialisers = [];
    const FLAG = 'qualityInitialised';

    return {
        register(name, initialise) {
            initialisers.push({ name, initialise });
            return initialise;
        },
        /** Run every initialiser over a freshly rendered subtree. */
        reinitialise(root) {
            const scope = root || document;
            initialisers.forEach(({ initialise }) => {
                try {
                    initialise(scope);
                } catch (error) {
                    // One broken initialiser must not stop the others or the page.
                }
            });
            document.dispatchEvent(
                new CustomEvent('quality:fragment-updated', { detail: { root: scope } }),
            );
        },
        /** True the first time it is asked about this element. */
        claim(element) {
            if (!element || element.dataset[FLAG] === 'true') {
                return false;
            }
            element.dataset[FLAG] = 'true';
            return true;
        },
    };
})();

/**
 * Main navigation: a full-width panel under the topbar holding first-level
 * category buttons, each with its own vertical submenu.
 *
 * All of its state lives here, so there is one place that decides what is
 * open: the panel itself, at most one category submenu, and the mutual
 * exclusion with the profile menu. Nothing is persisted — the navigation
 * always starts closed on a fresh page, including after following a link.
 */
document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('[data-sidebar-overlay]');
    const toggle = document.querySelector('[data-sidebar-toggle]');
    const closeButton = document.querySelector('[data-sidebar-close]');
    const profileMenu = document.querySelector('.profile-menu');

    if (!sidebar || !overlay || !toggle) {
        return;
    }

    const categories = [...sidebar.querySelectorAll('[data-nav-category]')];

    const submenuOf = (category) => {
        const group = category.closest('[data-nav-group]');
        return group ? group.querySelector('[data-nav-submenu]') : null;
    };

    /** Collapse every category; the panel itself is left as it is. */
    const closeCategories = () => {
        categories.forEach((category) => {
            category.setAttribute('aria-expanded', 'false');
            const submenu = submenuOf(category);
            if (submenu) {
                submenu.hidden = true;
            }
        });
    };

    const isOpen = () => sidebar.classList.contains('sidebar--open');

    const setOpen = (open) => {
        sidebar.classList.toggle('sidebar--open', open);
        overlay.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
        // Closing the panel must never leave a category expanded behind it.
        if (!open) {
            closeCategories();
        } else if (profileMenu) {
            profileMenu.open = false;
        }
    };

    setOpen(false);

    toggle.addEventListener('click', () => setOpen(!isOpen()));
    closeButton?.addEventListener('click', () => setOpen(false));
    overlay.addEventListener('click', () => setOpen(false));

    // Click only: at most one submenu is open, and clicking the open category
    // again closes it.
    categories.forEach((category) => {
        category.addEventListener('click', () => {
            const willOpen = category.getAttribute('aria-expanded') !== 'true';
            closeCategories();
            if (!willOpen) {
                return;
            }
            category.setAttribute('aria-expanded', 'true');
            const submenu = submenuOf(category);
            if (submenu) {
                submenu.hidden = false;
            }
        });
    });

    // A leaf link navigates away: close the panel first, on every viewport, so
    // the next page never renders with the menu hanging open.
    sidebar.querySelectorAll('.sidebar__link').forEach((link) => {
        link.addEventListener('click', () => setOpen(false));
    });

    // Anything outside the panel — including the topbar around the toggle —
    // dismisses the whole navigation.
    document.addEventListener('click', (event) => {
        if (!isOpen()) {
            return;
        }
        const target = event.target;
        if (sidebar.contains(target) || toggle.contains(target)) {
            return;
        }
        setOpen(false);
    });

    // The profile menu and the navigation are mutually exclusive.
    profileMenu?.addEventListener('toggle', () => {
        if (profileMenu.open) {
            setOpen(false);
        }
    });
});

document.addEventListener('DOMContentLoaded', () => {
    const menu = document.querySelector('[data-notification-menu]');
    if (!menu) {
        return;
    }

    const itemsContainer = menu.querySelector('[data-notification-items]');
    const counter = menu.querySelector('[data-notification-counter]');
    const summary = menu.querySelector('[data-notification-summary]');
    const markReadUrl = menu.dataset.markReadUrl;
    if (!itemsContainer || !counter || !summary || !markReadUrl) {
        return;
    }

    const getCsrfToken = () => {
        const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
        return match ? decodeURIComponent(match[1]) : '';
    };

    const updateCounter = (unreadCount) => {
        const value = Number.isFinite(unreadCount) ? Math.max(0, unreadCount) : 0;
        counter.textContent = String(value);
        counter.hidden = value <= 0;
        summary.setAttribute(
            'aria-label',
            value > 0 ? `Уведомления: ${value} непрочитанных` : 'Уведомления',
        );
    };

    const showEmptyStateIfNeeded = () => {
        if (itemsContainer.querySelector('[data-notification-id]')) {
            return;
        }
        if (itemsContainer.querySelector('[data-notification-empty]')) {
            return;
        }
        const empty = document.createElement('p');
        empty.className = 'notification-menu__empty';
        empty.dataset.notificationEmpty = '';
        empty.textContent = 'Новых уведомлений нет.';
        itemsContainer.append(empty);
    };

    // Only the container's contents are ever swapped, never the container or the
    // <details> itself, so the handlers below keep finding freshly rendered
    // items and are never re-registered after a fragment refresh.
    const replaceItems = (html) => {
        itemsContainer.innerHTML = html;
        showEmptyStateIfNeeded();
    };

    // Opening the bell must not empty it: the user has to be able to read what
    // arrived. What was on screen is marked read only once the menu is
    // dismissed — by closing it, or by following one of its links away from the
    // page — so nothing disappears while it is being read.
    const markVisibleAsRead = ({ keepalive = false } = {}) => {
        const unreadItems = [...itemsContainer.querySelectorAll('[data-notification-unread="true"]')];
        if (!unreadItems.length) {
            return;
        }

        const formData = new FormData();
        unreadItems.forEach((item) => formData.append('ids', item.dataset.notificationId));

        fetch(markReadUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': getCsrfToken() },
            body: formData,
            // A click on an item navigates away: the request must outlive the page.
            keepalive,
        })
            .then((response) => {
                if (!response.ok) {
                    throw new Error(`Unexpected status ${response.status}`);
                }
                return response.json();
            })
            .then((data) => {
                // Only what was actually marked: an item that arrived after the
                // request left is still unread and stays in the menu.
                unreadItems.forEach((item) => item.remove());
                showEmptyStateIfNeeded();
                updateCounter(data.unread_count);
            })
            .catch(() => {
                // Leave the menu and counter untouched on failure; server state is unchanged.
            });
    };

    menu.addEventListener('toggle', () => {
        if (menu.open) {
            return;
        }
        markVisibleAsRead();
    });

    itemsContainer.addEventListener('click', (event) => {
        if (event.target.closest && event.target.closest('[data-notification-id]')) {
            markVisibleAsRead({ keepalive: true });
        }
    });

    // The narrow contract the real-time client uses, so the bell's DOM rules
    // live in exactly one place. Absent on pages without a bell.
    window.qualityNotificationMenu = {
        element: menu,
        itemsContainer,
        updateCounter,
        replaceItems,
    };
});
