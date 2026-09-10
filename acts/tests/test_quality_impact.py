"""«Анализ влияния отклонений на качество изделия» — правило, отдельно от всего.

Модуль `acts/quality_impact.py` не импортирует модели и ничего не сохраняет: он
отвечает на два вопроса — «обязателен ли анализ при таком решении КО» и «что в
поданных значениях не так». Поэтому и тесты здесь работают с голыми словарями,
без базы: то, как это доезжает до `ActDefect`, проверяется в тестах формы и
перехода КО.
"""

from django.test import SimpleTestCase

from acts import quality_impact


def _values(**overrides):
    """Пустой анализ, поверх которого тест выставляет только своё."""
    values = {name: False for name in quality_impact.CHECKLIST_FIELDS}
    values.update({name: '' for name in quality_impact.TEXT_FIELDS})
    values.update(overrides)
    return values


class QualityImpactRuleTests(SimpleTestCase):
    def test_analysis_is_required_only_by_the_two_deviation_decisions(self):
        """Анализируется влияние *отклонений* — значит нужны отклонения.

        Два разрешающих решения «с отклонением» его требуют. Решение «без
        отклонения с доработкой» разрешает изделие, но отклонения в нём нет, и
        запрет — тем более: анализировать нечего.
        """
        self.assertTrue(quality_impact.is_required('ALLOW_NO_REWORK'))
        self.assertTrue(quality_impact.is_required('ALLOW_WITH_REWORK'))
        self.assertFalse(quality_impact.is_required('ALLOW_NO_DEVIATION_REWORK'))
        self.assertFalse(quality_impact.is_required('PROHIBIT_USE'))
        # Неизвестное или пустое решение ничего не требует: недопустимое
        # решение отвергается раньше, и это не задача этого модуля.
        self.assertFalse(quality_impact.is_required(''))

    def test_a_required_analysis_needs_at_least_one_item(self):
        empty = _values()
        errors = quality_impact.validate('ALLOW_NO_REWORK', empty)
        self.assertIn(quality_impact.NON_FIELD, errors)

        one = _values(quality_impact_assembly=True)
        self.assertEqual(quality_impact.validate('ALLOW_NO_REWORK', one), {})

    def test_an_optional_analysis_may_be_left_empty(self):
        """«Не требуется» — это не «запрещено»."""
        empty = _values()
        self.assertEqual(quality_impact.validate('ALLOW_NO_DEVIATION_REWORK', empty), {})
        self.assertEqual(quality_impact.validate('PROHIBIT_USE', empty), {})

    def test_a_checked_item_must_carry_the_values_it_promises(self):
        """Отмеченный пункт с полями обязан их заполнить — и когда анализ в целом
        необязателен, тоже: отметку поставили, значит утверждение сделано."""
        half = _values(
            quality_impact_em_parameters=True,
            quality_impact_em_value='1,2 мГн',
        )
        errors = quality_impact.validate('ALLOW_NO_REWORK', half)
        self.assertIn('quality_impact_em_tolerance', errors)
        self.assertNotIn('quality_impact_em_value', errors)

        filled = _values(
            quality_impact_em_parameters=True,
            quality_impact_em_value='1,2 мГн',
            quality_impact_em_tolerance='±5%',
        )
        self.assertEqual(quality_impact.validate('ALLOW_NO_REWORK', filled), {})

        other = _values(quality_impact_other=True)
        self.assertIn(
            'quality_impact_other_text',
            quality_impact.validate('ALLOW_NO_DEVIATION_REWORK', other),
        )

    def test_normalize_drops_text_of_unchecked_items(self):
        """Галочка — авторитет, а не текст рядом с ней.

        Снятая галочка с оставшимся текстом не ошибка пользователя, а обычный
        след правки формы, поэтому текст молча очищается — ровно так же, как
        `workshops.py` очищает поля, которых цех не собирает. Это же и делает
        табличный constraint невозможным к нарушению.
        """
        stale = _values(
            quality_impact_em_value='1,2 мГн',
            quality_impact_em_tolerance='±5%',
            quality_impact_other_text='остатки прошлой правки',
            quality_impact_assembly=True,
        )
        normalized = quality_impact.normalize('ALLOW_NO_REWORK', stale)
        self.assertEqual(normalized['quality_impact_em_value'], '')
        self.assertEqual(normalized['quality_impact_em_tolerance'], '')
        self.assertEqual(normalized['quality_impact_other_text'], '')
        # Отмеченное не трогается.
        self.assertTrue(normalized['quality_impact_assembly'])

    def test_normalize_clears_everything_when_the_decision_prohibits_use(self):
        """Запрет стирает анализ целиком: документ не хранит утверждений о
        качестве изделия, которое запретили использовать."""
        filled = _values(
            quality_impact_assembly=True,
            quality_impact_em_parameters=True,
            quality_impact_em_value='1,2 мГн',
            quality_impact_em_tolerance='±5%',
        )
        normalized = quality_impact.normalize('PROHIBIT_USE', filled)
        self.assertEqual(normalized, _values())

    def test_an_optional_analysis_is_kept_rather_than_cleared(self):
        """«Без отклонения с доработкой» не требует анализа, но и не выбрасывает
        набранное: «Собираемость обеспечена» после доработки — всё ещё факт."""
        filled = _values(quality_impact_assembly=True)
        normalized = quality_impact.normalize('ALLOW_NO_DEVIATION_REWORK', filled)
        self.assertTrue(normalized['quality_impact_assembly'])

    def test_describe_lists_only_the_checked_items_in_document_order(self):
        """Что печатная форма и режим чтения показывают вместо чек-листа."""
        values = _values(
            quality_impact_no_interturn_short=True,
            quality_impact_em_parameters=True,
            quality_impact_em_value='1,2 мГн',
            quality_impact_em_tolerance='±5%',
            quality_impact_other=True,
            quality_impact_other_text='Согласовано с главным конструктором',
        )
        described = quality_impact.describe(values)
        self.assertEqual(
            [item['label'] for item in described],
            [
                'Риск межвиткового замыкания исключен',
                'Электромагнитные параметры',
                'Другое',
            ],
        )
        self.assertEqual(described[0]['detail'], '')
        self.assertEqual(described[1]['detail'], '1,2 мГн (допуск по КД/ГОСТ: ±5%)')
        self.assertEqual(described[2]['detail'], 'Согласовано с главным конструктором')
        self.assertEqual(quality_impact.describe(_values()), [])
