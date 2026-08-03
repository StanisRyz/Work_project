document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('[data-sidebar-overlay]');
    const toggle = document.querySelector('[data-sidebar-toggle]');
    const closeButton = document.querySelector('[data-sidebar-close]');
    const storageKey = 'quality-sidebar-open';

    if (!sidebar || !overlay || !toggle) {
        return;
    }

    const setOpen = (isOpen) => {
        sidebar.classList.toggle('sidebar--open', isOpen);
        overlay.hidden = !isOpen;
        toggle.setAttribute('aria-expanded', String(isOpen));
        toggle.setAttribute('aria-label', isOpen ? 'Закрыть меню' : 'Открыть меню');
        localStorage.setItem(storageKey, String(isOpen));
    };

    setOpen(localStorage.getItem(storageKey) === 'true');
    toggle.addEventListener('click', () => setOpen(!sidebar.classList.contains('sidebar--open')));
    closeButton?.addEventListener('click', () => setOpen(false));
    overlay.addEventListener('click', () => setOpen(false));
    sidebar.querySelectorAll('.sidebar__link').forEach((link) => {
        link.addEventListener('click', () => {
            if (window.matchMedia('(max-width: 760px)').matches) {
                setOpen(false);
            }
        });
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
        counter.textContent = String(unreadCount);
        counter.hidden = unreadCount <= 0;
        summary.setAttribute(
            'aria-label',
            unreadCount > 0 ? `Уведомления: ${unreadCount} непрочитанных` : 'Уведомления',
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

    menu.addEventListener('toggle', () => {
        if (!menu.open) {
            return;
        }

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
        })
            .then((response) => {
                if (!response.ok) {
                    throw new Error(`Unexpected status ${response.status}`);
                }
                return response.json();
            })
            .then((data) => {
                unreadItems.forEach((item) => item.remove());
                showEmptyStateIfNeeded();
                updateCounter(data.unread_count);
            })
            .catch(() => {
                // Leave the menu and counter untouched on failure; server state is unchanged.
            });
    });
});
