/**
 * «Восстановить черновик»: what was typed into a long form survives a closed
 * tab, a crashed browser or an expired session.
 *
 * A form marked `[data-draft-key]` — the act form, the protocol editor, the СМК
 * form — is copied into this browser's `localStorage` a second after every
 * change. Opening the same form again offers the copy in a notice above it:
 * «Восстановить» or «Удалить». Nothing is ever sent to the server and nothing
 * is restored without being asked: this is not an autosave, the document is
 * still stored only by its own «Сохранить».
 *
 * The key carries the user id, so on a shared workstation a draft is offered
 * only to the person who typed it; the current user's drafts are removed on
 * «Выйти», and any draft older than `MAX_AGE_DAYS` is removed on the next page
 * load.
 *
 * When is a draft finished with? A submission cannot be judged from here, so
 * it is judged by the page that follows:
 *
 * * the same form again, rendered from the posted data (a validation error,
 *   the СМК confirmation step — `[data-unsaved-guard="dirty"]`) — the draft
 *   stays, the text is still unsaved;
 * * the login page — the session had expired and the POST was lost: the draft
 *   stays and is offered once the form is opened again;
 * * anything else — the submission went through, and the draft is removed.
 *
 * Restoring rebuilds the rows first, by pressing the form's own «+» and «×»
 * buttons, then writes every value back by field name and replays the
 * `change`/`input` events the form's own scripts listen to. No markup is
 * assembled here and no rule is restated: the server validates a restored
 * form exactly like a typed one.
 */
(() => {
    'use strict';

    if (window.qualityFormDrafts) {
        return;
    }

    const PREFIX = 'quality-draft:v1:';
    const PENDING_KEY = 'quality-draft:v1:pending';
    const MAX_AGE_DAYS = 7;
    const SAVE_DELAY_MS = 800;
    const SKIPPED_NAMES = new Set(['csrfmiddlewaretoken']);
    const SKIPPED_TYPES = new Set(['file', 'submit', 'button', 'reset', 'image', 'password']);

    const storage = (kind) => {
        try {
            const store = window[kind];
            const probe = `${PREFIX}probe`;
            store.setItem(probe, '1');
            store.removeItem(probe);
            return store;
        } catch (error) {
            return null;
        }
    };
    const local = storage('localStorage');
    const session = storage('sessionStorage');
    if (!local) {
        // Private mode or storage disabled: the forms work exactly as before.
        return;
    }

    const read = (key) => {
        try {
            const value = JSON.parse(local.getItem(key) || 'null');
            return value && typeof value === 'object' && Array.isArray(value.fields) ? value : null;
        } catch (error) {
            return null;
        }
    };
    const write = (key, value) => {
        try {
            local.setItem(key, JSON.stringify(value));
        } catch (error) {
            // Quota exceeded: keep working without a draft rather than fail.
        }
    };
    const remove = (key) => {
        try {
            local.removeItem(key);
        } catch (error) {
            // Nothing to do.
        }
    };
    const storedKeys = () => {
        const keys = [];
        for (let index = 0; index < local.length; index += 1) {
            const key = local.key(index);
            if (key && key.startsWith(PREFIX) && key !== PENDING_KEY) {
                keys.push(key);
            }
        }
        return keys;
    };

    const storageKey = (form) => `${PREFIX}${form.dataset.draftKey}`;

    const isField = (element) => {
        if (!element || !element.name || SKIPPED_NAMES.has(element.name)) {
            return false;
        }
        const tag = element.tagName;
        if (tag === 'TEXTAREA' || tag === 'SELECT') {
            return true;
        }
        return tag === 'INPUT' && !SKIPPED_TYPES.has((element.type || '').toLowerCase());
    };

    const fieldsOf = (form) => [...form.elements].filter(isField);

    /** Every field in document order: its name, its value, and for a tick its state. */
    const serialize = (form) =>
        fieldsOf(form).map((field) => {
            const type = (field.type || '').toLowerCase();
            if (type === 'checkbox' || type === 'radio') {
                return { n: field.name, v: field.value, c: field.checked };
            }
            return { n: field.name, v: field.value };
        });

    const hasContent = (fields) =>
        fields.some((field) => {
            if ('c' in field) {
                return field.c;
            }
            // Row counters and row identities are structure, not something
            // the user typed.
            return field.v && !/-(TOTAL_FORMS|INITIAL_FORMS|MIN_NUM_FORMS|MAX_NUM_FORMS|id)$/.test(field.n);
        });

    const sameFields = (left, right) => JSON.stringify(left) === JSON.stringify(right);

    // ------------------------------------------------------------ saving

    const timers = new Map();

    const saveNow = (form) => {
        if (!form.isConnected) {
            return;
        }
        const fields = serialize(form);
        const key = storageKey(form);
        if (form.dataset.draftBaseline && form.dataset.draftBaseline === JSON.stringify(fields)) {
            // Back to exactly what the server rendered: nothing to keep.
            remove(key);
            return;
        }
        write(key, {
            savedAt: Date.now(),
            base: form.dataset.draftBase || '',
            fields,
        });
    };

    const scheduleSave = (form) => {
        window.clearTimeout(timers.get(form));
        timers.set(form, window.setTimeout(() => saveNow(form), SAVE_DELAY_MS));
    };

    const draftFormOf = (target) =>
        target && typeof target.closest === 'function'
            ? (target.form && target.form.closest('[data-draft-key]')) || target.closest('[data-draft-key]')
            : null;

    ['input', 'change'].forEach((type) =>
        document.addEventListener(type, (event) => {
            if (event.isTrusted === false) {
                return;
            }
            const form = draftFormOf(event.target);
            if (form) {
                scheduleSave(form);
            }
        }, true),
    );
    // Adding or removing a row is a change too, though no field fired.
    document.addEventListener('click', (event) => {
        if (event.isTrusted === false) {
            return;
        }
        const target = event.target;
        if (!target || typeof target.closest !== 'function') {
            return;
        }
        if (target.closest('[data-add-row], [data-remove-row], [data-add-assignee], [data-remove-assignee], [data-add-defect], [data-remove-defect]')) {
            const form = draftFormOf(target);
            if (form) {
                scheduleSave(form);
            }
        }
    });

    // Leaving the page flushes whatever the debounce has not written yet.
    window.addEventListener('pagehide', () => {
        document.querySelectorAll('form[data-draft-key]').forEach((form) => {
            if (timers.has(form)) {
                window.clearTimeout(timers.get(form));
                saveNow(form);
            }
        });
    });

    // A submission is judged by the page that follows it (see above).
    window.addEventListener('submit', (event) => {
        if (event.defaultPrevented) {
            return;
        }
        const form = event.target;
        if (form && form.matches && form.matches('[data-draft-key]')) {
            window.clearTimeout(timers.get(form));
            saveNow(form);
            if (session) {
                session.setItem(PENDING_KEY, storageKey(form));
            }
        }
        if (form && form.matches && form.matches('[data-logout-form]')) {
            // A shared workstation: what this user typed leaves with them.
            const owner = form.dataset.draftOwner;
            storedKeys()
                .filter((key) => owner && key.startsWith(`${PREFIX}${owner}:`))
                .forEach(remove);
        }
    });

    // ------------------------------------------------------------ restoring

    /** Press a form's own button: the row markup comes from its `<template>`. */
    const press = (button) => {
        if (button) {
            button.click();
        }
    };

    const savedValue = (fields, name) => {
        const found = fields.find((field) => field.n === name);
        return found ? found.v : null;
    };

    const countOf = (fields, name) => fields.filter((field) => field.n === name).length;

    /** Protocol and СМК: `[data-block]` sections with `[data-row-list]` rows. */
    const rebuildBlocks = (form, fields) => {
        form.querySelectorAll('[data-block]').forEach((block) => {
            const total = block.querySelector('[data-total]');
            const list = block.querySelector('[data-row-list]');
            if (!total || !list) {
                return;
            }
            const wanted = Number.parseInt(savedValue(fields, total.name), 10);
            if (!Number.isFinite(wanted) || wanted < 0) {
                return;
            }
            let guard = 200;
            while (list.children.length < wanted && guard > 0) {
                guard -= 1;
                press(block.querySelector('[data-add-row]'));
            }
            while (list.children.length > wanted && guard > 0) {
                guard -= 1;
                const before = list.children.length;
                const last = list.children[list.children.length - 1];
                press(last.querySelector('[data-remove-row]'));
                if (list.children.length === before) {
                    // The last row is cleared rather than removed.
                    break;
                }
            }
        });
        // Every мероприятие's исполнители, by how many were posted under its name.
        form.querySelectorAll('[data-assignee-list]').forEach((assignees) => {
            const select = assignees.querySelector('select[name$="-assignees"]');
            if (!select) {
                return;
            }
            const wanted = Math.max(1, countOf(fields, select.name));
            const rows = () => assignees.querySelectorAll('[data-assignee-row]');
            const row = assignees.closest('[data-row]');
            let guard = 100;
            while (rows().length < wanted && guard > 0) {
                guard -= 1;
                press(row && row.querySelector('[data-add-assignee]'));
            }
            while (rows().length > wanted && guard > 0) {
                guard -= 1;
                const current = rows();
                press(current[current.length - 1].querySelector('[data-remove-assignee]'));
            }
        });
    };

    /** The act form: one `.defect-form-block` per defect. */
    const rebuildDefects = (form, fields) => {
        const formset = form.querySelector('[data-defect-formset]');
        if (!formset) {
            return;
        }
        const total = formset.querySelector('input[name$="-TOTAL_FORMS"]');
        const wanted = Number.parseInt(savedValue(fields, total && total.name), 10);
        const blocks = () => formset.querySelectorAll('.defect-form-block');
        let guard = 200;
        while (Number.isFinite(wanted) && blocks().length < wanted && guard > 0) {
            guard -= 1;
            press(formset.querySelector('[data-add-defect]'));
        }
    };

    /** Write the saved values back, the n-th saved value of a name into its n-th field. */
    const assignValues = (form, fields) => {
        const byName = new Map();
        fields.forEach((field) => {
            if (!byName.has(field.n)) {
                byName.set(field.n, []);
            }
            byName.get(field.n).push(field);
        });
        const used = new Map();
        fieldsOf(form).forEach((element) => {
            const saved = byName.get(element.name);
            if (!saved) {
                return;
            }
            const index = used.get(element.name) || 0;
            used.set(element.name, index + 1);
            const field = saved[index];
            if (!field) {
                return;
            }
            if ('c' in field) {
                element.checked = field.c;
            } else if (element.value !== field.v) {
                element.value = field.v;
            }
        });
    };

    /** Replay what a user's typing would have told the form's own scripts. */
    const replayEvents = (form) => {
        fieldsOf(form).forEach((element) => {
            const tag = element.tagName;
            const type = tag === 'SELECT' || element.type === 'checkbox' || element.type === 'date'
                ? 'change'
                : 'input';
            element.dispatchEvent(new Event(type, { bubbles: true }));
        });
    };

    const restore = (form, draft) => {
        const fields = draft.fields;
        rebuildBlocks(form, fields);
        rebuildDefects(form, fields);
        // Twice: a selector whose options depend on another field (the
        // исполнитель on the подразделение, the speaker on the participants,
        // the finding a measure answers) only offers the saved value once the
        // field it depends on has been written and announced.
        assignValues(form, fields);
        replayEvents(form);
        assignValues(form, fields);
        replayEvents(form);
        // A defect the user had removed before the draft was saved.
        form.querySelectorAll('input[name$="-DELETE"]').forEach((field) => {
            if (field.checked) {
                const block = field.closest('.defect-form-block');
                if (block && !block.hidden) {
                    press(block.querySelector('[data-remove-defect]'));
                }
            }
        });
        // Restored text is unsaved text: the leave-page prompt and the live
        // refresh must both treat it as typed.
        if (window.qualityUnsavedGuard && typeof window.qualityUnsavedGuard.markDirty === 'function') {
            window.qualityUnsavedGuard.markDirty();
        }
        form.dispatchEvent(new CustomEvent('quality:form-restored', { bubbles: true }));
        saveNow(form);
    };

    const formatSavedAt = (timestamp) => {
        try {
            return new Date(timestamp).toLocaleString('ru-RU', {
                day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
            });
        } catch (error) {
            return '';
        }
    };

    const offer = (form, draft) => {
        const notice = document.createElement('div');
        notice.className = 'draft-restore';
        notice.setAttribute('role', 'status');
        const text = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = `Найден несохранённый черновик от ${formatSavedAt(draft.savedAt)}.`;
        const hint = document.createElement('p');
        hint.textContent = draft.base && form.dataset.draftBase && draft.base !== form.dataset.draftBase
            ? 'Документ изменился после того, как был сохранён черновик: проверьте данные после восстановления.'
            : 'Он сохранён только в этом браузере. Восстановите его, чтобы продолжить заполнение.';
        text.append(title, hint);
        const actions = document.createElement('div');
        actions.className = 'draft-restore__actions';
        const restoreButton = document.createElement('button');
        restoreButton.type = 'button';
        restoreButton.className = 'link-button';
        restoreButton.textContent = 'Восстановить';
        const discardButton = document.createElement('button');
        discardButton.type = 'button';
        discardButton.className = 'link-button link-button--secondary';
        discardButton.textContent = 'Удалить черновик';
        actions.append(restoreButton, discardButton);
        notice.append(text, actions);

        restoreButton.addEventListener('click', () => {
            notice.remove();
            // The protocol editor sits in a live block and may have been
            // replaced since the notice was drawn: restore into the form that
            // is on the page now.
            const current = [...document.querySelectorAll('form[data-draft-key]')]
                .find((candidate) => candidate.dataset.draftKey === form.dataset.draftKey);
            if (current) {
                restore(current, draft);
            }
        });
        discardButton.addEventListener('click', () => {
            notice.remove();
            remove(storageKey(form));
        });
        form.parentNode.insertBefore(notice, form);
    };

    // ------------------------------------------------------------ page load

    const start = () => {
        const now = Date.now();
        storedKeys().forEach((key) => {
            const draft = read(key);
            if (!draft || now - draft.savedAt > MAX_AGE_DAYS * 24 * 60 * 60 * 1000) {
                remove(key);
            }
        });

        const forms = [...document.querySelectorAll('form[data-draft-key]')];
        const pending = session ? session.getItem(PENDING_KEY) : null;
        if (pending && session) {
            session.removeItem(PENDING_KEY);
            const again = forms.find((form) => storageKey(form) === pending);
            const rejected = again && again.matches('[data-unsaved-guard="dirty"]');
            const loggedOut = Boolean(document.querySelector('[data-login-form]'));
            if (!rejected && !loggedOut) {
                remove(pending);
            }
        }

        forms.forEach((form) => {
            // What the server rendered: typing back to exactly this is not a draft.
            form.dataset.draftBaseline = JSON.stringify(serialize(form));
            if (form.matches('[data-unsaved-guard="dirty"]')) {
                // The page already shows the user's own input.
                return;
            }
            const draft = read(storageKey(form));
            if (!draft || !hasContent(draft.fields) || sameFields(draft.fields, serialize(form))) {
                return;
            }
            offer(form, draft);
        });
    };

    window.qualityFormDrafts = { restore, serialize, saveNow };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
