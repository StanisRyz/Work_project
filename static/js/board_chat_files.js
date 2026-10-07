/**
 * Files in a card's «Чат»: choosing them before the message is sent.
 *
 * Without this script the chat's form (`form[data-chat-files]`, multipart)
 * carries an ordinary file input (`[data-chat-files-input]`, `multiple`) and
 * the files go with the message to `boards:card_comment`. With it, the same
 * input stays the one thing the form posts, and three more ways fill it:
 *
 * - «📎» — the input's label: what is picked is added to what was picked
 *   before, instead of replacing it;
 * - files dropped on «Чат» (`[data-board-tab-body="chat"]`, lit while
 *   something is dragged over it);
 * - an image pasted into the message (Ctrl+V of a screenshot), named
 *   `скриншот-ГГГГ-ММ-ДД-ЧЧММСС.png` — unless the clipboard also holds text,
 *   which is then what was meant (a cell of a table copies a picture of
 *   itself too).
 *
 * Every chosen file is a chip under the message — its name, its size and «×»
 * to take it back before sending. The selection is written into the input
 * through `DataTransfer`; a browser without it keeps the ordinary input and
 * none of this. It is not `attachment_upload.js`, where choosing a file *is*
 * the upload: here the files wait for «Отправить».
 *
 * Nothing here is a rule: how many files, which types and how large is the
 * server's to say (`post_card_comment()`, `ecosystem.attachments`), and a
 * refusal comes back as the form with the message. The form is outside every
 * live block, so a live refresh never drops what was chosen. Listeners are
 * delegated from `document`, and the per-form setup is a
 * `window.qualityFragments` initialiser, so a drawer opened without a reload
 * keeps working.
 */
(() => {
    'use strict';

    if (window.qualityBoardChatFiles) {
        return;
    }

    const FORM = 'form[data-chat-files]';
    const ZONE = '[data-board-tab-body="chat"]';
    const READY = 'data-chat-files-ready';

    const chosen = new WeakMap();   // form → the File objects chosen, in order

    const formOf = (element) =>
        element && typeof element.closest === 'function' ? element.closest(FORM) : null;
    const inputOf = (form) => form.querySelector('[data-chat-files-input]');
    const listOf = (form) => form.querySelector('[data-chat-files-list]');
    const filesOf = (form) => chosen.get(form) || [];
    const isReady = (form) => Boolean(form) && form.getAttribute(READY) !== null;

    /** The size as the server words it (`ecosystem.attachments.format_file_size`). */
    const formatSize = (bytes) => {
        const size = Number(bytes) || 0;
        if (size < 1024) {
            return `${size} Б`;
        }
        if (size < 1024 * 1024) {
            return `${(size / 1024).toFixed(1)} КБ`;
        }
        return `${(size / (1024 * 1024)).toFixed(1)} МБ`;
    };

    const canTransfer = () => {
        if (typeof window.DataTransfer !== 'function') {
            return false;
        }
        try {
            return Boolean(new window.DataTransfer().items);
        } catch (error) {
            return false;
        }
    };

    /** What the form posts: the selection, written into its own input. */
    const writeBack = (form, files) => {
        const input = inputOf(form);
        if (!input) {
            return;
        }
        const transfer = new window.DataTransfer();
        files.forEach((file) => transfer.items.add(file));
        input.files = transfer.files;
    };

    const draw = (form) => {
        const list = listOf(form);
        if (!list) {
            return;
        }
        Array.from(list.children || []).forEach((child) => child.remove());
        filesOf(form).forEach((file, index) => {
            const chip = document.createElement('li');
            chip.setAttribute('class', 'board-chat__chip');
            chip.setAttribute('data-chat-file', String(index));
            const name = document.createElement('span');
            name.setAttribute('class', 'board-chat__chip-name text-ellipsis');
            name.setAttribute('title', file.name);
            name.textContent = file.name;
            const size = document.createElement('span');
            size.setAttribute('class', 'board-chat__chip-size');
            size.textContent = formatSize(file.size);
            const remove = document.createElement('button');
            remove.setAttribute('type', 'button');
            remove.setAttribute('class', 'board-chat__chip-remove');
            remove.setAttribute('data-chat-file-remove', String(index));
            remove.setAttribute('aria-label', `Убрать файл ${file.name}`);
            remove.setAttribute('title', 'Убрать файл');
            remove.textContent = '×';
            chip.append(name, size, remove);
            list.append(chip);
        });
        list.hidden = filesOf(form).length === 0;
    };

    const setFiles = (form, files) => {
        chosen.set(form, files);
        writeBack(form, files);
        draw(form);
        // A chosen file is unsaved input: leaving the page asks first
        // (`unsaved_guard.js`), whether it came by a pick, a drop or a paste.
        if (files.length && window.qualityUnsavedGuard) {
            window.qualityUnsavedGuard.markDirty();
        }
    };

    const add = (form, files) => {
        const incoming = Array.from(files || []).filter(Boolean);
        if (!isReady(form) || !incoming.length) {
            return;
        }
        setFiles(form, filesOf(form).concat(incoming));
    };

    const pad = (value) => String(value).padStart(2, '0');

    /** `скриншот-2026-10-07-142530.png` — the moment it was pasted. */
    const screenshotName = (type, index, now = new Date()) => {
        const extension = { 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif' }[type] || '.png';
        const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
            + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
        return `скриншот-${stamp}${index ? `-${index + 1}` : ''}${extension}`;
    };

    // -- per form ------------------------------------------------------------

    const initialiseForm = (form) => {
        if (isReady(form) || !canTransfer() || !inputOf(form)) {
            return;
        }
        form.setAttribute(READY, '');
        chosen.set(form, []);
        draw(form);
    };

    const initialise = (scope) => {
        const root = scope || document;
        if (root && typeof root.matches === 'function' && root.matches(FORM)) {
            initialiseForm(root);
        }
        if (root && typeof root.querySelectorAll === 'function') {
            Array.from(root.querySelectorAll(FORM)).forEach(initialiseForm);
        }
    };

    // -- «📎»: picked files join the ones picked before -----------------------

    document.addEventListener('change', (event) => {
        const input = event.target;
        if (!input || typeof input.matches !== 'function' || !input.matches('[data-chat-files-input]')) {
            return;
        }
        const form = formOf(input);
        if (!isReady(form)) {
            return;
        }
        // The browser has just replaced the input's files with the new pick.
        add(form, input.files);
    });

    document.addEventListener('click', (event) => {
        const target = event.target;
        const remove = target && typeof target.closest === 'function' ? target.closest('[data-chat-file-remove]') : null;
        const form = formOf(remove);
        if (!remove || !isReady(form)) {
            return;
        }
        event.preventDefault();
        const index = Number(remove.getAttribute('data-chat-file-remove'));
        setFiles(form, filesOf(form).filter((_file, position) => position !== index));
        const text = form.querySelector('textarea');
        if (text && typeof text.focus === 'function') {
            text.focus();
        }
    });

    // -- Ctrl+V of a screenshot ------------------------------------------------

    document.addEventListener('paste', (event) => {
        const form = formOf(event.target);
        const data = event.clipboardData;
        if (!isReady(form) || !data) {
            return;
        }
        const images = Array.from(data.items || [])
            .filter((item) => item.kind === 'file' && /^image\//.test(item.type || ''))
            .map((item) => item.getAsFile())
            .filter(Boolean);
        if (!images.length) {
            return;
        }
        const text = typeof data.getData === 'function' ? data.getData('text/plain') : '';
        if (text) {
            return;
        }
        event.preventDefault();
        const now = new Date();
        add(form, images.map((image, index) => new window.File(
            [image], screenshotName(image.type, index, now), { type: image.type || 'image/png' },
        )));
    });

    // -- files dropped on «Чат» -------------------------------------------------

    const zoneOf = (target) =>
        target && typeof target.closest === 'function' ? target.closest(ZONE) : null;
    const formIn = (zone) => (zone ? zone.querySelector(`${FORM}[${READY}]`) : null);
    const carriesFiles = (event) =>
        Boolean(event.dataTransfer) && Array.from(event.dataTransfer.types || []).includes('Files');

    document.addEventListener('dragover', (event) => {
        const zone = zoneOf(event.target);
        if (!formIn(zone) || !carriesFiles(event)) {
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
        zone.classList.add('is-dropping');
    });

    document.addEventListener('dragleave', (event) => {
        const zone = zoneOf(event.target);
        if (zone && !(event.relatedTarget && zone.contains(event.relatedTarget))) {
            zone.classList.remove('is-dropping');
        }
    });

    document.addEventListener('drop', (event) => {
        const zone = zoneOf(event.target);
        const form = formIn(zone);
        if (!form || !carriesFiles(event)) {
            return;
        }
        event.preventDefault();
        zone.classList.remove('is-dropping');
        add(form, event.dataTransfer.files);
    });

    if (window.qualityFragments) {
        window.qualityFragments.register('board-chat-files', initialise);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => initialise(document));
    } else {
        initialise(document);
    }

    window.qualityBoardChatFiles = {
        initialise,
        add,
        filesOf,
        screenshotName,
    };
})();
