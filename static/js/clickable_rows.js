/**
 * One shared «the whole row opens the object» behaviour for every registry
 * and related-activities table.
 *
 * A row opts in by carrying the target itself:
 *
 *   <tr data-row-url="{% url 'acts:detail' act.pk %}">
 *
 * Nothing else is needed — no per-page script, no per-table stylesheet. The
 * pointer cursor and the hover tint come from the `[data-row-url]` rules in
 * `components.css`, so a row that has no URL (a СМК мероприятие whose task was
 * never created) also gets no cursor and no hover, and stays inert.
 *
 * The number cell stays a real <a> in every one of those tables: that is what
 * keeps keyboard navigation, «open in new tab» and the status bar preview
 * working. This handler only adds a second, larger hit area for the mouse.
 *
 * The listener is delegated on `document` — registries and the activities
 * table are all replaced live by the real-time client, and a delegated handler
 * keeps working on markup that arrived after page load without being re-bound,
 * exactly as `confirm_modal.js` does it.
 */
(() => {
    'use strict';

    // Anything that already does something of its own when clicked. A click
    // that lands on one of these is that control's click, never the row's, so
    // buttons, dropdowns, checkboxes, status controls and the row's own links
    // keep the behaviour they have today.
    const INTERACTIVE = 'a, button, input, select, textarea, label, summary, details, [role="button"], [data-confirm], [data-no-row-url]';

    /** True while the user is selecting text: releasing the drag is not a click. */
    const isSelectingText = () => {
        const selection = window.getSelection();
        return Boolean(selection) && !selection.isCollapsed && selection.toString().trim() !== '';
    };

    document.addEventListener('click', (event) => {
        const row = event.target.closest ? event.target.closest('[data-row-url]') : null;
        const url = row && row.dataset.rowUrl;
        if (!url) {
            return;
        }

        // A control between the click and the row wins. Looked up from the
        // target and bounded by the row, so a control *outside* the table can
        // never suppress it.
        const control = event.target.closest(INTERACTIVE);
        if (control && row.contains(control)) {
            return;
        }

        if (isSelectingText()) {
            return;
        }

        // The modifiers a real link would honour, honoured here too.
        if (event.metaKey || event.ctrlKey || event.shiftKey) {
            window.open(url, '_blank', 'noopener');
            return;
        }

        window.location.assign(url);
    });
})();
