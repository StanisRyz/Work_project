/**
 * Документация: presentation only. Every action here ends in an ordinary form
 * POST that the server checks again — nothing is decided in the browser.
 *
 * - «Загрузить файлы» opens the upload panel and the file picker; files
 *   dropped anywhere on a folder page land in the same input.
 * - `[data-panel-open]` / `[data-panel-close]` open and close the header
 *   panels (`<details class="doc-panel">`), which work without JavaScript too.
 * - `[data-copy-link]` copies a link and says so.
 * - The folder table's checkboxes drive the selection bar, and a document row
 *   dragged onto a folder (in the tree or in the table) is moved there.
 * - The card form shows «Причина отмены» only for «Отменён».
 */
(() => {
    'use strict';

    // ------------------------------------------------------------ helpers
    const toast = (text) => {
        const node = document.createElement('div');
        node.className = 'doc-toast';
        node.setAttribute('role', 'status');
        node.textContent = text;
        document.body.appendChild(node);
        window.setTimeout(() => node.remove(), 2200);
    };

    const closeMenus = (except) => {
        document.querySelectorAll('.doc-menu[open]').forEach((menu) => {
            if (menu !== except) {
                menu.removeAttribute('open');
            }
        });
    };

    document.addEventListener('click', (event) => {
        const menu = event.target.closest('.doc-menu');
        closeMenus(menu);
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closeMenus(null);
        }
    });

    // ------------------------------------------------------------- panels
    const openPanel = (id) => {
        const panel = document.getElementById(id);
        if (!panel) {
            return null;
        }
        document.querySelectorAll('.doc-panel[open]').forEach((other) => {
            if (other !== panel) {
                other.removeAttribute('open');
            }
        });
        panel.setAttribute('open', '');
        panel.scrollIntoView({ block: 'nearest' });
        const field = panel.querySelector('input:not([type=hidden]):not([type=file]), select, textarea');
        if (field) {
            field.focus({ preventScroll: true });
        }
        return panel;
    };

    document.addEventListener('click', (event) => {
        const opener = event.target.closest('[data-panel-open]');
        if (opener) {
            closeMenus(null);
            openPanel(opener.dataset.panelOpen);
            return;
        }
        const closer = event.target.closest('[data-panel-close]');
        if (closer) {
            const panel = closer.closest('.doc-panel, details');
            if (panel) {
                panel.removeAttribute('open');
            }
        }
    });

    // --------------------------------------------------------- copy link
    document.addEventListener('click', (event) => {
        const button = event.target.closest('[data-copy-link]');
        if (!button) {
            return;
        }
        const link = button.dataset.copyLink;
        closeMenus(null);
        const done = () => toast('Ссылка скопирована');
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(link).then(done, () => window.prompt('Ссылка на документ:', link));
        } else {
            window.prompt('Ссылка на документ:', link);
        }
    });

    // --------------------------------------------------------- dropzones
    const describeFiles = (files) => {
        const names = Array.from(files, (file) => file.name);
        if (!names.length) {
            return '';
        }
        return names.length === 1 ? names[0] : `${names.length} файл.: ${names.join(', ')}`;
    };

    document.querySelectorAll('[data-dropzone]').forEach((zone) => {
        const input = zone.querySelector('input[type="file"]');
        const text = zone.querySelector('[data-dropzone-text]');
        if (!input || !text) {
            return;
        }
        const initial = text.innerHTML;
        const refresh = () => {
            const described = describeFiles(input.files || []);
            zone.classList.toggle('has-files', Boolean(described));
            if (described) {
                text.textContent = described;
            } else {
                text.innerHTML = initial;
            }
        };
        input.addEventListener('change', refresh);
        zone.addEventListener('dragover', (event) => {
            event.preventDefault();
            zone.classList.add('is-dragging');
        });
        zone.addEventListener('dragleave', () => zone.classList.remove('is-dragging'));
        zone.addEventListener('drop', (event) => {
            event.preventDefault();
            event.stopPropagation();
            zone.classList.remove('is-dragging');
            if (!event.dataTransfer || !event.dataTransfer.files.length) {
                return;
            }
            try {
                input.files = event.dataTransfer.files;
            } catch (error) {
                return;
            }
            refresh();
        });
    });

    // ------------------------------------------------------------ upload
    const uploadPanel = document.querySelector('[data-upload-panel]');
    const uploadInput = uploadPanel ? uploadPanel.querySelector('input[type="file"]') : null;
    document.querySelectorAll('[data-upload-open]').forEach((button) => {
        button.addEventListener('click', () => {
            if (!uploadPanel) {
                return;
            }
            openPanel(uploadPanel.id);
            if (uploadInput && !(uploadInput.files && uploadInput.files.length)) {
                uploadInput.click();
            }
        });
    });

    // Files dropped anywhere on a folder page go to the upload panel.
    const carriesFiles = (event) => Boolean(
        event.dataTransfer && Array.from(event.dataTransfer.types || []).includes('Files')
    );
    if (uploadPanel && uploadInput) {
        let depth = 0;
        document.addEventListener('dragenter', (event) => {
            if (carriesFiles(event)) {
                depth += 1;
                document.body.classList.add('doc-dropping');
            }
        });
        document.addEventListener('dragleave', (event) => {
            if (carriesFiles(event)) {
                depth = Math.max(0, depth - 1);
                if (!depth) {
                    document.body.classList.remove('doc-dropping');
                }
            }
        });
        document.addEventListener('dragover', (event) => {
            if (carriesFiles(event)) {
                event.preventDefault();
            }
        });
        document.addEventListener('drop', (event) => {
            if (!carriesFiles(event)) {
                return;
            }
            event.preventDefault();
            depth = 0;
            document.body.classList.remove('doc-dropping');
            if (!event.dataTransfer.files.length) {
                return;
            }
            openPanel(uploadPanel.id);
            try {
                uploadInput.files = event.dataTransfer.files;
            } catch (error) {
                return;
            }
            uploadInput.dispatchEvent(new Event('change', { bubbles: true }));
        });
    }

    // --------------------------------------------------------- selection
    const bulk = document.querySelector('[data-doc-bulk]');
    const checks = () => Array.from(document.querySelectorAll('[data-doc-check]'));
    const checkAll = document.querySelector('[data-doc-check-all]');
    const bulkCount = bulk ? bulk.querySelector('[data-doc-bulk-count]') : null;

    const refreshBulk = () => {
        if (!bulk) {
            return;
        }
        const selected = checks().filter((box) => box.checked).length;
        bulk.hidden = selected === 0;
        if (bulkCount) {
            bulkCount.textContent = `Выбрано: ${selected}`;
        }
        if (checkAll) {
            const total = checks().length;
            checkAll.checked = total > 0 && selected === total;
            checkAll.indeterminate = selected > 0 && selected < total;
        }
    };
    if (bulk) {
        document.addEventListener('change', (event) => {
            if (event.target.matches('[data-doc-check]')) {
                refreshBulk();
            }
        });
        if (checkAll) {
            checkAll.addEventListener('change', () => {
                checks().forEach((box) => { box.checked = checkAll.checked; });
                refreshBulk();
            });
        }
        refreshBulk();
    }

    // -------------------------------------------------- drag to a folder
    const csrf = document.querySelector('input[name="csrfmiddlewaretoken"]');
    let dragged = null;

    const movePost = (ids, folderId) => {
        if (!csrf || !ids.length) {
            return;
        }
        const form = document.createElement('form');
        form.method = 'post';
        form.action = bulk ? bulk.action : '/documents/bulk/';
        const field = (name, value) => {
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = name;
            input.value = value;
            form.appendChild(input);
        };
        field('csrfmiddlewaretoken', csrf.value);
        field('action', 'move');
        field('target', folderId);
        field('next', window.location.pathname + window.location.search);
        ids.forEach((id) => field('ids', id));
        document.body.appendChild(form);
        form.submit();
    };

    document.addEventListener('dragstart', (event) => {
        const row = event.target.closest && event.target.closest('tr[data-doc-id]');
        if (!row) {
            return;
        }
        const box = row.querySelector('[data-doc-check]');
        // Dragging a ticked row takes the whole selection with it.
        dragged = box && box.checked
            ? checks().filter((item) => item.checked).map((item) => item.value)
            : [row.dataset.docId];
        row.classList.add('is-dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', dragged.join(','));
    });
    document.addEventListener('dragend', () => {
        dragged = null;
        document.querySelectorAll('.is-dragging, .is-drop-target').forEach((node) => {
            node.classList.remove('is-dragging', 'is-drop-target');
        });
    });
    document.addEventListener('dragover', (event) => {
        if (!dragged) {
            return;
        }
        const target = event.target.closest && event.target.closest('[data-drop-folder]');
        document.querySelectorAll('.is-drop-target').forEach((node) => {
            if (node !== target) {
                node.classList.remove('is-drop-target');
            }
        });
        if (target) {
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
            target.classList.add('is-drop-target');
        }
    });
    document.addEventListener('drop', (event) => {
        if (!dragged) {
            return;
        }
        const target = event.target.closest && event.target.closest('[data-drop-folder]');
        if (!target) {
            return;
        }
        event.preventDefault();
        movePost(dragged, target.dataset.dropFolder);
    });

    // --------------------------------------------------------- card form
    const status = document.querySelector('.doc-card-form select[name="status"]');
    const reason = document.querySelector('[data-cancel-reason]');
    if (status && reason) {
        const sync = () => { reason.hidden = status.value !== 'CANCELLED'; };
        status.addEventListener('change', sync);
        sync();
    }
})();
