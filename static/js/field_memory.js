/*
 * `form[data-field-memory="<name>:<user id>"]`: the form comes back with the
 * values it was last left with — per user, in this browser's localStorage.
 *
 * For calculators, whose inputs are a working set somebody returns to, not a
 * document. Presentation only: nothing reaches the server, and the restored
 * values go through the form's own `input`/`change` listeners exactly as if
 * they had been typed, so the page's own code — not this file — decides what
 * they mean. Hidden and file inputs are never remembered.
 *
 * Loaded by the page *after* its own scripts, so those listeners exist when
 * the restored values are announced.
 */
(function () {
    'use strict';

    let store = null;
    try {
        store = window.localStorage;
        store.setItem('__quality_probe__', '1');
        store.removeItem('__quality_probe__');
    } catch (error) {
        return;
    }

    const remembered = (field) => field.name
        && !['hidden', 'file', 'submit', 'button', 'password'].includes(field.type);

    document.querySelectorAll('form[data-field-memory]').forEach((form) => {
        const key = `quality-fields:v1:${form.dataset.fieldMemory}`;
        const fields = Array.from(form.elements).filter(remembered);

        let saved = null;
        try {
            saved = JSON.parse(store.getItem(key) || 'null');
        } catch (error) {
            saved = null;
        }
        if (saved && typeof saved === 'object') {
            fields.forEach((field) => {
                if (!(field.name in saved)) {
                    return;
                }
                const value = String(saved[field.name]);
                if (field.type === 'checkbox' || field.type === 'radio') {
                    field.checked = value === 'true';
                } else if (field.value !== value) {
                    field.value = value;
                } else {
                    return;
                }
                field.dispatchEvent(new Event('input', { bubbles: true }));
                field.dispatchEvent(new Event('change', { bubbles: true }));
            });
        }

        const save = () => {
            const values = {};
            fields.forEach((field) => {
                values[field.name] = (field.type === 'checkbox' || field.type === 'radio')
                    ? field.checked
                    : field.value;
            });
            try {
                store.setItem(key, JSON.stringify(values));
            } catch (error) {
                // A full storage only means the next visit starts from defaults.
            }
        };
        form.addEventListener('input', save);
        form.addEventListener('change', save);
    });
})();
