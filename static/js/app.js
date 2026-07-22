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
