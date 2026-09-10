"""«Анализ влияния отклонений на качество изделия» — одно место, где живёт правило.

Раздел 1 бумажного акта (ДП-СМК 07.04, изм.2) требует от КО не только решения о
возможности использования, но и обоснования: чек-листа из шести пунктов, три из
которых несут собственные значения. Здесь — состав этого чек-листа, правило
обязательности и приведение поданных значений к виду, который может храниться.

Устроен как `acts/workshops.py` и по той же причине: правило спрашивают форма,
сервис и печатная форма, и второе его изложение стало бы вторым источником
истины. Модуль **не импортирует модели** — он работает со словарём «имя поля →
значение», а коды решений КО перечислены строками, как `workshops.py`
перечисляет коды цехов. Ничего не сохраняет и ничего не читает из базы.
"""

from dataclasses import dataclass


# Решения КО, при которых изделие разрешено использовать *с отклонением*, и
# только они требуют анализа: анализируется влияние отклонений, а там, где
# отклонения нет, анализировать нечего. «Разрешить без отклонения с доработкой»
# поэтому в список не входит — блок ему доступен, но не обязателен.
DEVIATION_DECISIONS = ('ALLOW_NO_REWORK', 'ALLOW_WITH_REWORK')

# Решения, стирающие анализ при сохранении. Запрет — единственное: документ не
# хранит утверждений о качестве изделия, которое запретили использовать.
CLEARING_DECISIONS = ('PROHIBIT_USE',)

# Ключ, под которым `validate()` возвращает ошибку, не принадлежащую ни одному
# полю: «не отмечен ни один пункт» — это про чек-лист целиком.
NON_FIELD = '__all__'


@dataclass(frozen=True)
class QualityImpactItem:
    """Один пункт чек-листа: галочка и, возможно, её собственные значения.

    `detail_template` — как отмеченный пункт читается в печатной форме и в
    режиме чтения; `{0}`, `{1}` подставляются из `text_fields` по порядку.
    """

    field: str
    label: str
    text_fields: tuple = ()
    detail_template: str = ''

    def is_checked(self, values):
        return bool(values.get(self.field))

    def missing_text_fields(self, values):
        """Пустые обязательные поля отмеченного пункта; для снятого — ничего."""
        if not self.is_checked(values):
            return ()
        return tuple(
            name for name in self.text_fields
            if not (values.get(name) or '').strip()
        )

    def detail(self, values):
        if not self.text_fields:
            return ''
        return self.detail_template.format(
            *[(values.get(name) or '').strip() for name in self.text_fields]
        )


# Пункты в порядке бумажного документа. Порядок здесь — это порядок на экране и
# в печатной форме: чек-лист читают рядом с бумагой, и перестановка сделала бы
# сверку недостоверной.
ITEMS = (
    QualityImpactItem(
        field='quality_impact_no_interturn_short',
        label='Риск межвиткового замыкания исключен',
    ),
    QualityImpactItem(
        field='quality_impact_em_parameters',
        label='Электромагнитные параметры',
        text_fields=('quality_impact_em_value', 'quality_impact_em_tolerance'),
        detail_template='{0} (допуск по КД/ГОСТ: {1})',
    ),
    QualityImpactItem(
        field='quality_impact_insulation_margin',
        label='Расчетный запас по перегреву изоляции достаточен, ресурс изделия не снижается',
    ),
    QualityImpactItem(
        field='quality_impact_assembly',
        label='Собираемость обеспечена',
    ),
    QualityImpactItem(
        field='quality_impact_customer_agreed',
        label='Согласовано с заказчиком',
    ),
    QualityImpactItem(
        field='quality_impact_other',
        label='Другое',
        text_fields=('quality_impact_other_text',),
        detail_template='{0}',
    ),
)

CHECKLIST_FIELDS = tuple(item.field for item in ITEMS)
TEXT_FIELDS = tuple(name for item in ITEMS for name in item.text_fields)
FIELDS = CHECKLIST_FIELDS + TEXT_FIELDS

# Во что превращается незаполненный анализ. Булевы — в `False`, текстовые — в
# пустую строку: `blank=True` без `null`, как и всё текстовое в проекте, чтобы
# «не заполнено» имело ровно одно представление.
EMPTY_VALUES = {
    **{name: False for name in CHECKLIST_FIELDS},
    **{name: '' for name in TEXT_FIELDS},
}


def is_required(decision):
    """Требует ли это решение КО заполненного анализа."""
    return decision in DEVIATION_DECISIONS


def clears(decision):
    """Стирает ли это решение КО ранее введённый анализ."""
    return decision in CLEARING_DECISIONS


def normalize(decision, values):
    """Значения в том виде, в каком их можно записать в `ActDefect`.

    Две операции, обе — приведение, а не проверка:

    * запрещающее решение стирает анализ целиком;
    * у снятой галочки очищается её текст. Это не ошибка пользователя, а
      обычный след правки формы, поэтому он молча отбрасывается — так же, как
      `workshops.py` очищает поля, которых цех не собирает. Заодно это делает
      табличный constraint «неотмеченный пункт не несёт текста» невозможным к
      нарушению из этого пути.

    Возвращает новый словарь; поданный не изменяется.
    """
    if clears(decision):
        return dict(EMPTY_VALUES)
    normalized = {name: bool(values.get(name)) for name in CHECKLIST_FIELDS}
    for item in ITEMS:
        checked = normalized[item.field]
        for name in item.text_fields:
            normalized[name] = (values.get(name) or '').strip() if checked else ''
    return normalized


def validate(decision, values):
    """`{имя поля или NON_FIELD: сообщение}` — пусто, когда всё в порядке.

    Два правила, и второе не зависит от первого:

    * при решении «с отклонением» должен быть отмечен хотя бы один пункт —
      иначе решение разрешает отклонение, ничем его не обосновав;
    * отмеченный пункт обязан заполнить свои поля, **при любом решении**:
      галочку поставили, значит утверждение сделано, и незаполненное значение
      делает его бессодержательным.

    Ожидает уже нормализованные значения — на ненормализованных второе правило
    поймало бы снятую галочку с текстом.
    """
    errors = {}
    if is_required(decision) and not any(values.get(name) for name in CHECKLIST_FIELDS):
        errors[NON_FIELD] = (
            'Отметьте хотя бы один пункт анализа влияния отклонений на качество изделия.'
        )
    for item in ITEMS:
        for name in item.missing_text_fields(values):
            errors[name] = 'Заполните значение для отмеченного пункта.'
    return errors


def values_of(source):
    """Значения анализа, снятые с `ActDefect` или из словаря формы.

    Существует, чтобы `describe()` и `validate()` принимали одно и то же —
    словарь — независимо от того, пришли значения из POST или уже лежат в базе.
    Модель здесь по-прежнему не импортируется: различает не тип, а наличие
    `get`.
    """
    if hasattr(source, 'get'):
        return {name: source.get(name) for name in FIELDS}
    return {name: getattr(source, name) for name in FIELDS}


def describe(values):
    """Отмеченные пункты в порядке документа — для чтения и печати.

    `[{'label', 'detail'}]`, где `detail` пуст у пункта без собственных полей.
    Неотмеченные пункты не возвращаются вовсе: бумажный акт показывает
    поставленные галочки, а не список из шести строк с пятью пустыми.
    """
    return [
        {'label': item.label, 'detail': item.detail(values)}
        for item in ITEMS
        if item.is_checked(values)
    ]
