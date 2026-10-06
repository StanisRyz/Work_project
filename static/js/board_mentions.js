/**
 * «@» in a card's «Чат»: call a colleague into the discussion.
 *
 * Without this script the chat's form (`form[data-board-mentions]`) carries
 * «Упомянуть» — a `<details>` of checkboxes, one per reader of the board
 * (`[data-mention-name]`), each posting `mention=<id>` with the message. With
 * it, that list becomes the source of names and is hidden, and typing «@» in
 * the message opens the same people, filtered by what follows the «@»:
 *
 * - ↑ ↓ choose, Enter or Tab (or a click) take the one chosen: the «@…» typed
 *   becomes «@Имя Фамилия » and a hidden `mention=<id>` joins the form;
 * - Esc, a click elsewhere or a space with nobody matching close the list;
 * - on sending, a hidden `mention` whose «@Имя Фамилия» is no longer in the
 *   text is dropped, so nobody is called by a name that was deleted.
 *
 * Nothing here is a rule: who may be mentioned is the server's list, and
 * `post_card_comment()` keeps only readers of the board whatever is posted.
 * Listeners are delegated from `document`, and the per-form setup is a
 * `window.qualityFragments` initialiser, so a drawer opened without a reload
 * or replaced by the live client keeps working.
 */
(() => {
    'use strict';

    if (window.qualityBoardMentions) {
        return;
    }

    const MAX_SHOWN = 8;
    // «@», then what has been typed of a name since — up to one space, as in
    // «@Иван Пе» — right before the caret, at the start or after a space.
    const TRIGGER = /(^|\s)@([^\s@]*(?: [^\s@]*)?)$/;

    let state = null;   // { form, textarea, start, query, matches, active, list }

    const formOf = (element) =>
        element && typeof element.closest === 'function' ? element.closest('form[data-board-mentions]') : null;

    const all = (root, selector) => Array.from(root.querySelectorAll(selector));

    const personOf = (input) => ({
        id: input.value || input.getAttribute('value') || '',
        name: input.getAttribute('data-mention-name') || '',
    });

    const peopleOf = (form) => all(form, '[data-mention-name]').map(personOf);

    const caretOf = (textarea) =>
        (typeof textarea.selectionStart === 'number' ? textarea.selectionStart : (textarea.value || '').length);

    // -- the list -------------------------------------------------------------

    const close = () => {
        if (state && state.list) {
            state.list.remove();
        }
        if (state && state.textarea) {
            state.textarea.setAttribute('aria-expanded', 'false');
        }
        state = null;
    };

    const draw = () => {
        if (state.list) {
            state.list.remove();
        }
        const list = document.createElement('ul');
        list.setAttribute('class', 'board-mentions');
        list.setAttribute('role', 'listbox');
        list.setAttribute('aria-label', 'Кого упомянуть');
        list.setAttribute('data-mention-list', '');
        state.matches.forEach((person, index) => {
            const option = document.createElement('li');
            option.setAttribute('role', 'option');
            option.setAttribute('class', `board-mentions__option${index === state.active ? ' is-active' : ''}`);
            option.setAttribute('aria-selected', index === state.active ? 'true' : 'false');
            option.setAttribute('data-mention-option', person.id);
            option.textContent = person.name;
            list.append(option);
        });
        const field = state.textarea.closest('.board-mentions__field') || state.form;
        field.append(list);
        state.list = list;
        state.textarea.setAttribute('aria-expanded', 'true');
    };

    /** Open, narrow or close the list for what is typed before the caret. */
    const update = (textarea) => {
        const form = formOf(textarea);
        if (!form) {
            close();
            return;
        }
        const before = (textarea.value || '').slice(0, caretOf(textarea));
        const found = TRIGGER.exec(before);
        if (!found) {
            close();
            return;
        }
        const query = found[2].toLocaleLowerCase('ru');
        const matches = peopleOf(form)
            .filter((person) => person.name && person.name.toLocaleLowerCase('ru').includes(query))
            .slice(0, MAX_SHOWN);
        if (!matches.length) {
            close();
            return;
        }
        const previous = state && state.textarea === textarea ? state.active : 0;
        state = {
            form,
            textarea,
            start: before.length - found[2].length - 1,
            caret: before.length,
            query,
            matches,
            active: Math.min(previous, matches.length - 1),
            list: state && state.textarea === textarea ? state.list : null,
        };
        draw();
    };

    /** «@Имя Фамилия » in the text and a hidden `mention` in the form. */
    const choose = (person) => {
        if (!state || !person) {
            return;
        }
        const { form, textarea, start, caret } = state;
        const value = textarea.value || '';
        const inserted = `@${person.name} `;
        textarea.value = value.slice(0, start) + inserted + value.slice(caret);
        const position = start + inserted.length;
        if (typeof textarea.setSelectionRange === 'function') {
            textarea.setSelectionRange(position, position);
        }
        const already = all(form, '[data-mention-hidden]').some((input) => input.getAttribute('value') === person.id);
        if (!already) {
            const hidden = document.createElement('input');
            hidden.setAttribute('type', 'hidden');
            hidden.setAttribute('name', 'mention');
            hidden.setAttribute('value', person.id);
            hidden.setAttribute('data-mention-hidden', '');
            hidden.setAttribute('data-mention-hidden-name', person.name);
            form.append(hidden);
        }
        close();
        if (typeof textarea.focus === 'function') {
            textarea.focus();
        }
    };

    // -- the form: setup and sending -----------------------------------------

    /** The checkboxes become the source of names: hidden, posting nothing. */
    const initialise = (scope) => {
        const root = scope || document;
        const forms = all(root, 'form[data-board-mentions]');
        const own = typeof root.matches === 'function' && root.matches('form[data-board-mentions]') ? [root] : [];
        [...own, ...forms].forEach((form) => {
            const fallback = form.querySelector('[data-mention-fallback]');
            if (!fallback || fallback.getAttribute('data-mention-enhanced') !== null) {
                return;
            }
            fallback.setAttribute('data-mention-enhanced', '');
            fallback.hidden = true;
            all(form, '[data-mention-name]').forEach((input) => {
                // Ticked in a refused form: carried on as a hidden field.
                if (input.checked) {
                    const hidden = document.createElement('input');
                    hidden.setAttribute('type', 'hidden');
                    hidden.setAttribute('name', 'mention');
                    hidden.setAttribute('value', input.value || input.getAttribute('value'));
                    hidden.setAttribute('data-mention-hidden', '');
                    hidden.setAttribute('data-mention-hidden-name', input.getAttribute('data-mention-name') || '');
                    form.append(hidden);
                }
                input.disabled = true;
            });
            const textarea = form.querySelector('[data-mention-input]');
            if (textarea) {
                textarea.setAttribute('aria-autocomplete', 'list');
                textarea.setAttribute('aria-expanded', 'false');
            }
        });
    };

    /** A mention whose «@Имя Фамилия» was deleted from the text goes too. */
    const prune = (form) => {
        const textarea = form.querySelector('[data-mention-input]');
        const text = textarea ? textarea.value || '' : '';
        all(form, '[data-mention-hidden]').forEach((input) => {
            const name = input.getAttribute('data-mention-hidden-name') || '';
            if (name && !text.includes(`@${name}`)) {
                input.remove();
            }
        });
    };

    document.addEventListener('input', (event) => {
        const target = event.target;
        if (target && typeof target.matches === 'function' && target.matches('[data-mention-input]')) {
            update(target);
        }
    });

    document.addEventListener('keydown', (event) => {
        if (!state || event.target !== state.textarea) {
            return;
        }
        const count = state.matches.length;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            state.active = (state.active + (event.key === 'ArrowDown' ? 1 : count - 1)) % count;
            draw();
        } else if (event.key === 'Enter' || event.key === 'Tab') {
            if (event.ctrlKey || event.metaKey) {
                return;
            }
            event.preventDefault();
            choose(state.matches[state.active]);
        } else if (event.key === 'Escape') {
            // The list, not the drawer: one Esc closes one thing.
            event.preventDefault();
            if (typeof event.stopPropagation === 'function') {
                event.stopPropagation();
            }
            close();
        }
    }, true);

    document.addEventListener('click', (event) => {
        const target = event.target;
        const option = target && typeof target.closest === 'function' ? target.closest('[data-mention-option]') : null;
        if (option && state && state.list && state.list.contains(option)) {
            event.preventDefault();
            const id = option.getAttribute('data-mention-option');
            choose(state.matches.find((person) => person.id === id));
            return;
        }
        if (state && !(state.textarea === target)) {
            close();
        }
    });

    document.addEventListener('submit', (event) => {
        const form = formOf(event.target);
        if (form && event.target === form) {
            close();
            prune(form);
        }
    }, true);

    if (window.qualityFragments) {
        window.qualityFragments.register('board-mentions', initialise);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => initialise(document));
    } else {
        initialise(document);
    }

    window.qualityBoardMentions = {
        initialise,
        close,
        get isOpen() {
            return Boolean(state);
        },
    };
})();
