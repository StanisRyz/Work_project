/**
 * Notification bell and toasts.
 *
 * `notification.created` is the only event in the whole system that produces a
 * toast; everything else updates silently. Toast text and links always come
 * from the Django-rendered fragment, never from the SSE payload.
 */
(() => {
    'use strict';

    const core = window.QualityRealtime;
    if (!core || !core.claimModule('notifications')) {
        // A repeated include would subscribe a second handler and toast twice.
        return;
    }

    const TOAST_TIMEOUT_MS = 8000;
    const MAX_VISIBLE_TOASTS = 3;

    const region = document.querySelector('[data-toast-region]');
    const bell = () => window.qualityNotificationMenu || null;

    const reducedMotion = window.matchMedia
        ? window.matchMedia('(prefers-reduced-motion: reduce)')
        : { matches: false };

    const trimToasts = () => {
        if (!region) {
            return;
        }
        const toasts = [...region.querySelectorAll('.toast')];
        while (toasts.length > MAX_VISIBLE_TOASTS) {
            toasts.shift().remove();
        }
    };

    const showToast = ({ title, message, url }) => {
        if (!region) {
            return null;
        }
        const toast = document.createElement('div');
        toast.className = 'toast';
        toast.setAttribute('role', 'status');
        toast.tabIndex = -1;

        const heading = document.createElement('strong');
        heading.className = 'toast__title';
        // textContent everywhere: server text is inserted as text, never markup.
        heading.textContent = title;
        toast.append(heading);

        if (message) {
            const body = document.createElement('p');
            body.className = 'toast__message';
            body.textContent = message;
            toast.append(body);
        }

        const actions = document.createElement('div');
        actions.className = 'toast__actions';
        const link = document.createElement('a');
        link.className = 'toast__link';
        link.href = url;
        link.textContent = 'Открыть';
        actions.append(link);
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'toast__close';
        close.setAttribute('aria-label', 'Закрыть уведомление');
        close.textContent = '×';
        actions.append(close);
        toast.append(actions);

        let timer = null;
        const dismiss = () => {
            if (timer !== null) {
                window.clearTimeout(timer);
                timer = null;
            }
            toast.remove();
        };
        const startTimer = () => {
            if (timer === null) {
                timer = window.setTimeout(dismiss, TOAST_TIMEOUT_MS);
            }
        };
        const pauseTimer = () => {
            if (timer !== null) {
                window.clearTimeout(timer);
                timer = null;
            }
        };

        close.addEventListener('click', dismiss);
        toast.addEventListener('mouseenter', pauseTimer);
        toast.addEventListener('mouseleave', startTimer);
        toast.addEventListener('focusin', pauseTimer);
        toast.addEventListener('focusout', startTimer);
        toast.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                dismiss();
            }
        });

        if (reducedMotion.matches) {
            toast.classList.add('toast--no-motion');
        }
        region.append(toast);
        trimToasts();
        startTimer();
        return toast;
    };

    const showToastForNotification = (notificationId) => {
        const menu = bell();
        const item = menu
            ? menu.itemsContainer.querySelector(`[data-notification-id="${notificationId}"]`)
            : null;
        if (!item) {
            showToast({
                title: 'Новое уведомление',
                message: 'Откройте страницу уведомлений, чтобы прочитать его.',
                url: core.config.notificationsUrl,
            });
            return;
        }
        const titleNode = item.querySelector('[data-notification-title]');
        const messageNode = item.querySelector('[data-notification-message]');
        showToast({
            title: titleNode ? titleNode.textContent.trim() : 'Новое уведомление',
            message: messageNode ? messageNode.textContent.trim() : '',
            url: item.getAttribute('href') || core.config.notificationsUrl,
        });
    };

    const coordinator = core.createRefreshCoordinator({
        url: core.config.notificationFragmentUrl,
        apply(payload, toastIds) {
            const menu = bell();
            if (menu) {
                if (typeof payload.items_html === 'string') {
                    menu.replaceItems(payload.items_html);
                }
                if (typeof payload.unread_count === 'number') {
                    menu.updateCounter(payload.unread_count);
                }
            }
            toastIds.forEach(showToastForNotification);
        },
        onDenied: () => core.stop(),
    });

    core.registerAdapter({
        name: 'notifications',
        revisions: ['notifications'],
        // A sync-driven refresh never toasts: it describes existing state.
        refresh: () => coordinator.schedule(null),
        stop: () => coordinator.stop(),
    });

    core.onOpen(() => coordinator.schedule(null));

    core.subscribe(core.EVENT_TYPES.NOTIFICATION_CREATED, (payload, meta) => {
        if (payload.resource_type !== 'notification' || !core.isPositiveInteger(payload.resource_id)) {
            return;
        }
        coordinator.schedule(meta.isFirstDelivery ? payload.resource_id : null);
    });

    core.subscribe(core.EVENT_TYPES.NOTIFICATION_READ, (payload) => {
        if (payload.resource_type !== 'user') {
            return;
        }
        // Never a toast, and never derived from notification_ids: `scope=all`
        // deliberately reports only counts.
        coordinator.schedule(null);
    });

    core.notifications = { showToast, coordinator };
})();
