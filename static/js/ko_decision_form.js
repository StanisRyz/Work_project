/**
 * «Анализ влияния отклонений» показывается ровно тогда, когда он осмыслен.
 *
 * Запрещающее решение стирает анализ при сохранении, поэтому и полей для него
 * не показывается; остальные решения его допускают. CSS так не умеет —
 * значение `select` в селекторах не матчится, — и это единственная строчка
 * поведения на всю карточку дефекта.
 *
 * Показ никогда не является разрешением. Правило «когда анализ обязателен»
 * живёт в `acts/quality_impact.py` и спрашивается дважды на сервере: формой,
 * чтобы ошибка легла на поле, и `apply_ko_decision()` под блокировкой строки
 * акта. Здесь только видимость, и без JavaScript блок виден всегда — это
 * правильная сторона отказа: заполнить лишнее можно, а пропустить
 * обязательное сервер не даст.
 */
(() => {
    // Единственное решение, при котором анализа быть не должно. Значение то
    // же, что `Act.KoDecision.PROHIBIT_USE`.
    const PROHIBIT_USE = 'PROHIBIT_USE';
    const CARD = '[data-defect-card]';

    const syncCard = (card) => {
        const select = card.querySelector('select[name$="ko_decision"]');
        const impact = card.querySelector('[data-ko-impact]');
        if (!select || !impact) {
            return;
        }
        impact.hidden = select.value === PROHIBIT_USE;
    };

    /**
     * Идемпотентно, как требует реестр: `claim()` пропускает уже связанную
     * карточку, а видимость пересчитывается при каждом проходе — после замены
     * живого фрагмента разметка новая, и её состояние надо привести к её же
     * выбранному решению.
     */
    const initialiseKoDecisionCards = (root) => {
        const scope = root || document;
        scope.querySelectorAll(CARD).forEach((card) => {
            if (window.qualityFragments.claim(card)) {
                card.addEventListener('change', (event) => {
                    if (event.target.matches('select[name$="ko_decision"]')) {
                        syncCard(card);
                    }
                });
            }
            syncCard(card);
        });
    };

    window.qualityFragments.register('koDecisionCards', initialiseKoDecisionCards);
    document.addEventListener('DOMContentLoaded', () => initialiseKoDecisionCards(document));
})();
