/**
 * «Обсуждение» in the card panel opens at its newest message.
 *
 * The message list scrolls on its own (`.board-discussion__list`), oldest
 * first, so a long discussion would otherwise open at its first message.
 * Presentation only, once, on load; the live client (`realtime/boards.js`)
 * keeps a reader at the bottom when a new message arrives.
 */
(() => {
    'use strict';

    const scrollToNewest = () => {
        const list = document.querySelector('[data-live-board-comments]');
        if (list) {
            list.scrollTop = list.scrollHeight;
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', scrollToNewest);
    } else {
        scrollToNewest();
    }
})();
