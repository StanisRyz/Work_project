"""What the board pages show: the left panel, one sub-board's columns, one card.

Read only, and never a permission decision of its own — the flags returned are
`boards.permissions` asked once. The number of queries depends neither on the
number of cards nor on the number of columns: the tabs and the columns are one
query each, the open cards and the latest completed ones are each one query
through their tasks, the исполнители one prefetch each, and the card the panel
shows one more. The board's own card fields are one query with their options
one prefetch, and the values of every card on the page — the panel's included
— one more. A filter — «Мои», the search, a field's — is a condition of the
tasks' own query (a field filter an `Exists()`), never a query of its own.
A tile's «☑ 2/5» is an annotation of that same query; the open card's
«Чек-лист» is one query, its followers (the board's readers, marked) one
more, and the mentions of its messages one prefetch. Who hears about a card
is `card_audience()`, one query.
"""

import datetime
import hashlib
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db.models import (
    Count, DateTimeField, Exists, F, IntegerField, Max, OuterRef, Prefetch, Q, Subquery, Value,
)
from django.db.models import prefetch_related_objects
from django.db.models.functions import Coalesce
from django.utils import timezone

from .columns import MAX_COLUMNS, card_column
from .models import (
    Board,
    BoardCardChecklistItem,
    BoardCardComment,
    BoardCardCommentMention,
    BoardCardEvent,
    BoardCardFieldValue,
    BoardColumn,
    BoardField,
    BoardFieldOption,
    BoardCardSubscription,
    BoardMember,
    SubBoard,
)
from .permissions import (
    active_employee_q,
    board_readers_q,
    can_cancel_card,
    can_edit_checklist,
    can_follow_card,
    can_manage_board,
    readable_boards_q,
    can_restore_board,
    can_work_on_board,
)


# How many completed cards the closing column draws. The rest are counted, not read:
# a board in use for a year has hundreds of them and nobody scrolls that far.
DONE_LIMIT = 50

# How many messages of «Обсуждение» the panel draws: the newest ones, oldest
# first. The earlier ones are counted, not read, and `?comments=all` shows
# them — a card discussed for months stays one query and a short list.
COMMENTS_LIMIT = 100

# How many member avatars the board heading draws before «+N».
MEMBER_PREVIEW_LIMIT = 5

# How many pinned people a column header draws before «+N».
PIN_PREVIEW_LIMIT = 3

# How many followers «Подписчики» on «Описание» draws before «+N».
SUBSCRIBER_PREVIEW_LIMIT = 5

# The longest `?q=` a board search reads; anything past it is dropped.
SEARCH_MAX_LENGTH = 200


# The parameters of a field filter all start with it: `f_<field id>`,
# `f_<id>_from`/`_to` (a date), `f_<id>_min`/`_max` (a number).
FIELD_PARAM_PREFIX = 'f_'

# `f_<id>=none` of a list: the cards holding no value of that field.
FIELD_NONE_VALUE = 'none'


@dataclass(frozen=True)
class FieldFilter:
    """One field of the board narrowing the cards — what its `f_<id>…`
    parameters said, already checked against the field (`parse_board_filters()`).

    A list: `option_ids` (any of them, in the options' order) and/or `none`
    (no value at all). A date: `date_from`/`date_to`, a number:
    `number_min`/`number_max` — each bound inclusive, either may be missing.
    A text: `text`, a substring whatever the case.
    """

    field_id: int
    kind: str
    option_ids: tuple = ()
    none: bool = False
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    number_min: Decimal | None = None
    number_max: Decimal | None = None
    text: str = ''

    @property
    def key(self):
        return f'{FIELD_PARAM_PREFIX}{self.field_id}'

    def params(self):
        """The filter as `(name, value)` pairs, in a fixed order: what
        `parse_board_filters()` reads back into this very filter."""
        key = self.key
        if self.kind == BoardField.Kind.SELECT:
            pairs = [(key, str(option_id)) for option_id in self.option_ids]
            return pairs + ([(key, FIELD_NONE_VALUE)] if self.none else [])
        if self.kind == BoardField.Kind.DATE:
            return [
                (f'{key}_{name}', value.isoformat())
                for name, value in (('from', self.date_from), ('to', self.date_to)) if value is not None
            ]
        if self.kind == BoardField.Kind.NUMBER:
            return [
                (f'{key}_{name}', format(value, 'f'))
                for name, value in (('min', self.number_min), ('max', self.number_max)) if value is not None
            ]
        return [(key, self.text)]

    def condition(self):
        """The cards this filter keeps, as a condition on their tasks — one
        `Exists()` (or `~Exists()` for «не задано») inside the tasks' own
        query, so a filter costs no query of its own."""
        values = BoardCardFieldValue.objects.filter(card=OuterRef('board_card'), field_id=self.field_id)
        if self.kind == BoardField.Kind.SELECT:
            condition = Q()
            if self.option_ids:
                condition |= Q(Exists(values.filter(option_id__in=self.option_ids)))
            if self.none:
                condition |= ~Q(Exists(values))
            return condition
        if self.kind == BoardField.Kind.DATE:
            bounds = {'value_date__gte': self.date_from, 'value_date__lte': self.date_to}
        elif self.kind == BoardField.Kind.NUMBER:
            bounds = {'value_number__gte': self.number_min, 'value_number__lte': self.number_max}
        else:
            bounds = {'value_text__icontains': self.text}
        return Q(Exists(values.filter(**{name: value for name, value in bounds.items() if value is not None})))


@dataclass(frozen=True)
class BoardFilters:
    """What the board shows: `?mine=1`, `?overdue=1`, `?stale=1`, `?q=<text>`
    and the filters of the board's own card fields (`fields`, `FieldFilter`s
    in the fields' order).

    Parsed once by `parse_board_filters()` for the page, its fragment and the
    drag's JSON counts alike, so the three can never filter differently.
    """

    mine: bool = False
    overdue: bool = False
    stale: bool = False
    q: str = ''
    fields: tuple = ()

    @property
    def is_active(self):
        return self.mine or self.overdue or self.stale or bool(self.q) or bool(self.fields)

    @property
    def query(self):
        """The filter as a query string without `?` — `''` when none is set.

        Always in the same order — `mine`, `overdue`, `stale`, `q`, then the
        fields by their position — so an address never changes by itself, and
        parsing it gives this very filter back.
        """
        params = []
        if self.mine:
            params.append(('mine', '1'))
        if self.overdue:
            params.append(('overdue', '1'))
        if self.stale:
            params.append(('stale', '1'))
        if self.q:
            params.append(('q', self.q))
        for field_filter in self.fields:
            params.extend(field_filter.params())
        return urlencode(params)

    def field_filter(self, field_id):
        """The filter of that field, or `None`."""
        return next((item for item in self.fields if item.field_id == field_id), None)

    def without_field(self, field_id):
        """The same filters with that field's dropped — a chip's «×»."""
        return replace(self, fields=tuple(item for item in self.fields if item.field_id != field_id))


NO_FILTERS = BoardFilters()


def _param_values(params, key):
    """Every value of `key` in `params` — a `QueryDict` or a plain mapping
    (whose value may be a list) — stripped."""
    if hasattr(params, 'getlist'):
        values = params.getlist(key)
    else:
        value = params.get(key)
        values = value if isinstance(value, (list, tuple)) else ([] if value is None else [value])
    return [str(value).strip() for value in values]


def _param(params, key):
    """The first non-empty value of `key`, or `''`."""
    return next((value for value in _param_values(params, key) if value), '')


def _parse_field_filter(params, field):
    """The `FieldFilter` `params` ask of `field`, or `None`.

    A value is read exactly as a card's value is (`services._parse_number()`,
    `_parse_date()`), and anything that does not read — an option of another
    field, a date or a number that is not one — is dropped without a word: a
    filter is a convenience of the address, not a rule, so a stale or
    hand-written link shows the board, never an error.
    """
    from .services import BoardError, _parse_date, _parse_number

    key = f'{FIELD_PARAM_PREFIX}{field.pk}'
    if field.kind == BoardField.Kind.SELECT:
        asked = set(_param_values(params, key))
        # An archived option still filters: cards keep holding it.
        option_ids = tuple(option.pk for option in field.options.all() if str(option.pk) in asked)
        none = FIELD_NONE_VALUE in asked
        if not option_ids and not none:
            return None
        return FieldFilter(field.pk, field.kind, option_ids=option_ids, none=none)
    if field.kind == BoardField.Kind.TEXT:
        text = _param(params, key)[:SEARCH_MAX_LENGTH]
        return FieldFilter(field.pk, field.kind, text=text) if text else None
    parse = _parse_date if field.kind == BoardField.Kind.DATE else _parse_number
    names = ('from', 'to') if field.kind == BoardField.Kind.DATE else ('min', 'max')
    bounds = []
    for name in names:
        raw = _param(params, f'{key}_{name}')
        try:
            value = parse(field, raw) if raw else None
        except BoardError:
            value = None
        if isinstance(value, Decimal):
            # «3,50» and «3.5» are one filter and one address.
            value = Decimal(format(value.normalize(), 'f'))
        bounds.append(value)
    if bounds == [None, None]:
        return None
    if field.kind == BoardField.Kind.DATE:
        return FieldFilter(field.pk, field.kind, date_from=bounds[0], date_to=bounds[1])
    return FieldFilter(field.pk, field.kind, number_min=bounds[0], number_max=bounds[1])


def parse_board_filters(params, fields=()):
    """`BoardFilters` from a request's GET (or any mapping of strings).

    `fields` are the board's own fields (`board_fields()`), in order: only a
    live one of them is read, so an unknown id, a field of another board and
    an archived field are no filter — the parameter is simply not there.
    """
    return BoardFilters(
        mine=params.get('mine') == '1',
        overdue=params.get('overdue') == '1',
        stale=params.get('stale') == '1',
        q=(params.get('q') or '').strip()[:SEARCH_MAX_LENGTH],
        fields=tuple(
            field_filter
            for field_filter in (
                _parse_field_filter(params, field) for field in fields if not field.is_archived
            )
            if field_filter is not None
        ),
    )


def names_field_filter(params):
    """Whether `params` carry any `f_…` parameter — whether the board's
    fields need reading to parse them."""
    return any(key.startswith(FIELD_PARAM_PREFIX) for key in params)


# A bare card number in the search: «12», «№12», «#12».
_CARD_NUMBER = re.compile(r'^\s*[№#]?\s*(\d+)\s*$')


def card_search_q(q):
    """The board search over a sub-board's tasks: the title, the card's code
    or the value of one of its text fields.

    A substring of the title; «ZAP-12» in any case (the board's code and the
    number — `tasks.selectors.board_card_code_filter()`, the registry's own
    rule); a bare number («12», «№12»); or a substring of a live text
    field's value («3-1579» of «Номер заявки» —
    `tasks.selectors.board_field_value_filter()`, the registry's and the
    topbar's rule too). The tasks searched are already the board's own, so a
    code of another board finds nothing here.
    """
    from tasks.selectors import board_card_code_filter, board_field_value_filter

    condition = Q(board_card__title__icontains=q)
    card_code = board_card_code_filter(q)
    if card_code is not None:
        condition |= card_code
    field_value = board_field_value_filter(q)
    if field_value is not None:
        condition |= field_value
    number = _CARD_NUMBER.match(q)
    if number is not None:
        condition |= Q(board_card__number=int(number.group(1)))
    return condition


# The journal entries that put a card where it stands: created in a column,
# moved into one, returned to one. A reorder within a column writes no entry,
# so it does not restart the clock.
IN_COLUMN_EVENT_KINDS = (
    BoardCardEvent.Kind.CREATED,
    BoardCardEvent.Kind.MOVED,
    BoardCardEvent.Kind.REOPENED,
)


def in_column_since():
    """«В колонке с»: when the task's card came into the column it stands in.

    The latest `CREATED`/`MOVED`/`REOPENED` entry of its journal, one
    correlated subquery of the tasks' own query — never a query per tile —
    and the card's own `created_at` for a card with no entry at all (none
    exists after `boards.0010`, but the answer must not be empty).
    """
    latest = (
        BoardCardEvent.objects.filter(card=OuterRef('board_card'), kind__in=IN_COLUMN_EVENT_KINDS)
        .order_by().values('card').annotate(at=Max('created_at')).values('at')
    )
    return Coalesce(Subquery(latest, output_field=DateTimeField()), F('board_card__created_at'))


def days_in_column(since, today=None):
    """Calendar days since `since` by the local date: today 0, yesterday 1.

    Weekends count — the clock of a stuck order does not stop for them — and
    the answer is a date difference, never seconds, so it moves once a day.
    """
    if since is None:
        return None
    today = today or timezone.localdate()
    return max((today - timezone.localtime(since).date()).days, 0)


def _start_of_day(day):
    """The first moment of `day` in the current time zone."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def stale_condition(columns, today=None):
    """«Застрявшие» as a condition on the tasks: an open card standing at least
    its column's threshold (`BoardColumn.stale_after_days`) in that column.

    `columns` are the columns already read (of one sub-board or of the whole
    board), so the condition is one `OR` over the working columns that have a
    threshold — no query of its own. A card whose column was deleted while it
    was closed stands in the first working column of its sub-board
    (`columns.card_column()`), and is judged by that one. «At least N days» is
    `localdate(since) <= today - N`, i.e. `since` before the start of day
    `today - N + 1`. No threshold anywhere keeps no card.
    """
    today = today or timezone.localdate()
    first_working = {}
    for column in columns:
        if not column.is_done:
            first_working.setdefault(column.sub_board_id, column.pk)
    condition = Q(pk__isnull=True)  # nothing: no column has a threshold
    for column in columns:
        days = column.stale_after_days
        if column.is_done or not days:
            continue
        place = Q(board_card__column_id=column.pk)
        if first_working.get(column.sub_board_id) == column.pk:
            place |= Q(board_card__column__isnull=True, board_card__sub_board_id=column.sub_board_id)
        threshold = _start_of_day(today - datetime.timedelta(days=days - 1))
        condition |= place & Q(stale_since__lt=threshold)
    return condition


def _filtered(tasks, filters, user, *, open_work, columns=()):
    """`tasks` narrowed by `filters`; `overdue` and `stale` only ever narrow
    open work.

    «Мои» is «I am an исполнитель» (`TaskAssignee`, one row per person, so the
    join adds no duplicates); `q` is a substring of the card's title, its
    code or a text field's value (`card_search_q()`); each field filter is
    one `Exists()` of the same query (`FieldFilter.condition()`), so neither
    the number of filters nor the number of fields adds a query. «Застрявшие»
    is `stale_condition()` over `columns` — the columns the caller has read
    already — on the same journal subquery the tiles read (`in_column_since()`).
    """
    if filters.mine:
        tasks = tasks.filter(assignees__user=user)
    if filters.q:
        tasks = tasks.filter(card_search_q(filters.q))
    for field_filter in filters.fields:
        tasks = tasks.filter(field_filter.condition())
    if filters.overdue and open_work:
        tasks = tasks.filter(due_date__lt=timezone.localdate())
    if filters.stale and open_work:
        tasks = tasks.alias(stale_since=in_column_since()).filter(stale_condition(columns))
    return tasks


def _board_tasks(sub_board):
    """The sub-board's tasks with their cards, исполнители and message counts.

    The «Обсуждение» count is a subquery annotation of the same query, so a
    tile's counter costs no query of its own.
    """
    return _tasks_with_cards(
        _all_board_tasks().filter(board_card__sub_board=sub_board)
    )


def _all_board_tasks():
    from tasks.models import Task

    return Task.objects.filter(source_type=Task.SourceType.BOARD)


def _tasks_with_cards(tasks):
    """`tasks` with what a tile reads: status, card, исполнители, messages,
    and since when the card stands in its column.

    The исполнители are one prefetch query with their accounts joined — a
    tile draws a name and initials, nothing of the profile. The message count,
    «В колонке с» (`in_column_since()`) and the «Чек-лист» counts
    (`checklist_annotations()`) are subquery annotations of the same query.
    """
    from tasks.models import TaskAssignee

    comments = BoardCardComment.objects.filter(card=OuterRef('board_card'))
    return (
        tasks.select_related('status', 'board_card')
        .prefetch_related(Prefetch('assignees', queryset=TaskAssignee.objects.select_related('user')))
        .annotate(
            comment_count=_count_subquery(comments, 'card'),
            in_column_since=in_column_since(),
            **checklist_annotations(),
        )
    )


def checklist_annotations():
    """«☑ 2/5» of a tile as two subquery annotations of the tasks' own
    query — how many items the card's «Чек-лист» has and how many are ticked
    — so the counter costs no query per tile."""
    items = BoardCardChecklistItem.objects.filter(card=OuterRef('board_card'))
    return {
        'checklist_total': _count_subquery(items, 'card'),
        'checklist_done': _count_subquery(items.filter(is_done=True), 'card'),
    }


def _item(task, board):
    """One card as every board template reads it.

    `board` is the page's own board, attached to the card so its code
    («ZAP-12», `BoardCard.code`) costs no query per tile.
    """
    card = task.board_card
    card.board = board
    is_closed = task.status.is_final
    since = getattr(task, 'in_column_since', None) if not is_closed else None
    return {
        'card': card,
        'task': task,
        'assignees': [assignee.user for assignee in task.assignees.all()],
        'due_date': task.due_date,
        'is_closed': is_closed,
        'comment_count': getattr(task, 'comment_count', 0),
        # «☑ 2/5»: the card's «Чек-лист», counted in the tasks' own query.
        'checklist_total': getattr(task, 'checklist_total', 0),
        'checklist_done': getattr(task, 'checklist_done', 0),
        # «В колонке с» and the days since — for open work only: a completed
        # or cancelled card stands nowhere it could be stuck.
        'in_column_since': since,
        'in_column_days': days_in_column(since),
        'is_stale': False,
    }


# --------------------------------------------------------------------------
# Custom card fields: how a value reads
# --------------------------------------------------------------------------

# How many field values a tile draws under its title.
TILE_FIELD_LIMIT = 4

# Between digit groups of a number: a no-break space, so «1 234 567» never
# breaks across lines.
NUMBER_GROUP_SEPARATOR = '\u00a0'


def board_fields(board):
    """Every field of `board` in order, archived ones too, each with its
    options in order (`field.options.all()`) — one query, plus one for the
    options when the board has any field."""
    return list(
        BoardField.objects.filter(board=board).order_by('position', 'pk')
        .prefetch_related(Prefetch('options', queryset=BoardFieldOption.objects.order_by('position', 'pk')))
    )


def fields_stamp(fields):
    """A short fingerprint of the fields' setup, read off what is in memory.

    The columns block carries it, so their fingerprint moves with every
    change of the setup — a renamed field, a new colour, a field hidden from
    the tiles — even where no tile shows it yet.
    """
    parts = [
        (field.pk, field.name, field.kind, field.position, field.show_on_tile, field.is_archived,
         [(option.pk, option.label, option.color, option.position, option.is_archived)
          for option in field.options.all()])
        for field in fields
    ]
    return hashlib.sha256(repr(parts).encode('utf-8')).hexdigest()[:12]


def format_number(value):
    """«1 234 567,5»: no trailing zeros, digit groups apart, a decimal comma."""
    text = format(value.normalize(), 'f')
    sign = '-' if text.startswith('-') else ''
    integer, _, fraction = text.lstrip('-').partition('.')
    groups = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)
    number = sign + NUMBER_GROUP_SEPARATOR.join(groups)
    return f'{number},{fraction}' if fraction else number


def number_input(value):
    """A stored number as the card form shows it to be edited: «1234,5»."""
    text = format(value.normalize(), 'f')
    return text.replace('.', ',')


def describe_field_value(field, row):
    """One value as every board template reads it.

    `{'field', 'name', 'kind', 'text', 'color', 'is_archived', 'on_tile'}`:
    `text` is the value as it reads — a list option's label, a date
    `ДД.ММ.ГГГГ`, a number by `format_number()` — and `color` the option's
    colour code for its chip (empty for the other kinds). `is_archived` is
    the field's or the option's: «Описание» marks it «(в архиве)», and the
    tile never shows it. `None` when the row names an option the field does
    not hold, or a row whose column is not its kind's — neither can happen:
    the schema and the services keep them together.
    """
    color = ''
    archived = field.is_archived
    if field.kind == BoardField.Kind.SELECT:
        option = next((option for option in field.options.all() if option.pk == row.option_id), None)
        if option is None:
            return None
        text, color = option.label, option.color
        archived = archived or option.is_archived
    elif field.kind == BoardField.Kind.NUMBER:
        if row.value_number is None:
            return None
        text = format_number(row.value_number)
    elif field.kind == BoardField.Kind.DATE:
        if row.value_date is None:
            return None
        text = row.value_date.strftime('%d.%m.%Y')
    else:
        if not row.value_text:
            return None
        text = row.value_text
    return {
        'field': field,
        'name': field.name,
        'kind': field.kind,
        'text': text,
        'color': color,
        'is_archived': archived,
        'on_tile': field.show_on_tile and not archived,
    }


def _filter_chip_text(field, field_filter):
    """«Приоритет: Высокий, Средний», «Срок: по 30.11.2026», «Сумма: от 5 до
    10», «Номер заявки: «3-1579»» — what an active field filter reads as."""
    kind = field.kind
    if kind == BoardField.Kind.SELECT:
        labels = [option.label for option in field.options.all() if option.pk in field_filter.option_ids]
        if field_filter.none:
            labels.append('не задано')
        value = ', '.join(labels)
    elif kind == BoardField.Kind.TEXT:
        value = f'«{field_filter.text}»'
    elif kind == BoardField.Kind.DATE:
        value = ' '.join(
            f'{word} {bound:%d.%m.%Y}'
            for word, bound in (('с', field_filter.date_from), ('по', field_filter.date_to)) if bound is not None
        )
    else:
        value = ' '.join(
            f'{word} {format_number(bound)}'
            for word, bound in (('от', field_filter.number_min), ('до', field_filter.number_max))
            if bound is not None
        )
    return f'{field.name}: {value}'


def describe_field_filters(fields, filters):
    """The «Поля» panel of the filter row: one row per live field, in order.

    Each is `{'field', 'kind', 'key', 'filter', 'chip'}` plus what its inputs
    show: a list's `options` (`{'option', 'checked'}` — the live ones, and an
    archived one only while it is chosen) and `none`; a date's `date_from`/
    `date_to` (ISO, for `type="date"`), a number's `number_min`/`number_max`
    (as typed: «1234,5») and a text's `text`. `chip` is the active filter's
    wording for the chip under the row, `''` while the field filters nothing.
    Read off what is in memory — no query.
    """
    rows = []
    for field in fields:
        if field.is_archived:
            continue
        field_filter = filters.field_filter(field.pk)
        chosen = field_filter.option_ids if field_filter else ()
        row = {
            'field': field,
            'kind': field.kind,
            'key': f'{FIELD_PARAM_PREFIX}{field.pk}',
            'filter': field_filter,
            'chip': _filter_chip_text(field, field_filter) if field_filter else '',
        }
        if field.kind == BoardField.Kind.SELECT:
            row['options'] = [
                {'option': option, 'checked': option.pk in chosen}
                for option in field.options.all()
                if not option.is_archived or option.pk in chosen
            ]
            row['none'] = bool(field_filter and field_filter.none)
        elif field.kind == BoardField.Kind.DATE:
            row['date_from'] = field_filter.date_from.isoformat() if field_filter and field_filter.date_from else ''
            row['date_to'] = field_filter.date_to.isoformat() if field_filter and field_filter.date_to else ''
        elif field.kind == BoardField.Kind.NUMBER:
            row['number_min'] = (
                number_input(field_filter.number_min) if field_filter and field_filter.number_min is not None else ''
            )
            row['number_max'] = (
                number_input(field_filter.number_max) if field_filter and field_filter.number_max is not None else ''
            )
        else:
            row['text'] = field_filter.text if field_filter else ''
        rows.append(row)
    return rows


def card_field_rows(fields, card_ids):
    """`{card id: {field id: BoardCardFieldValue}}` for `card_ids` — one query,
    none at all when the board has no fields."""
    if not fields or not card_ids:
        return {}
    rows = {}
    for row in BoardCardFieldValue.objects.filter(
        card_id__in=card_ids, field_id__in=[field.pk for field in fields],
    ):
        rows.setdefault(row.card_id, {})[row.field_id] = row
    return rows


def attach_field_values(item, fields, rows):
    """`item['field_values']` — every value the card holds, in the fields'
    order, archived ones included — `item['tile_values']` — the first
    `TILE_FIELD_LIMIT` of those a tile shows — and `item['field_rows']`, the
    rows by field id (what the edit form starts from)."""
    item['field_rows'] = rows
    values = []
    for field in fields:
        row = rows.get(field.pk)
        if row is not None:
            value = describe_field_value(field, row)
            if value is not None:
                values.append(value)
    item['field_values'] = values
    item['tile_values'] = [value for value in values if value['on_tile']][:TILE_FIELD_LIMIT]
    return item


def _panel_card(board, sub_board, columns_of, card_id, user, *, loaded, tabs, can_work,
                all_comments=False, fields=(), field_rows=None):
    """The card `?card=` names, with its task — or `None`.

    Any card of this board: one of this sub-board, or one moved to another
    sub-board while this page had it open — then `moved_to` is that sub-board
    and the panel says where the card is now, with a link, without going
    there itself. `None` for anything else: a card of another board, a
    missing id, text. Unlike the columns, a cancelled card is found too — its
    panel is the read-only record of what was withdrawn.

    The panel is where a `BOARD` task is worked, so the card also carries what
    the task page used to show: its attachments
    (`tasks.presentation.task_attachment_cards()`, the task page's own list)
    and the task rights, each asked once of `tasks.permissions` — the board
    has no rules of its own about completing, reopening or files.

    `loaded` are the tasks the columns already read (`{card id: task}`): the
    open card is almost always among them, and its исполнители are taken from
    there instead of being read twice. `columns_of` is the board's columns by
    sub-board, `tabs` its sub-boards — both already read for the page.
    `can_work` is `can_work_on_board()`, asked once for the page: writing in
    «Чат» is exactly that right (`can_comment_card()`).
    """
    from tasks.models import TaskAssignee
    from tasks.permissions import (
        can_complete_task,
        can_reopen_task,
        can_upload_task_attachment,
    )
    from tasks.presentation import task_attachment_cards

    try:
        card_id = int(card_id)
    except (TypeError, ValueError):
        return None
    comments = BoardCardComment.objects.filter(card=OuterRef('board_card'))
    task = (
        _all_board_tasks()
        .filter(board_card__board=board, board_card_id=card_id)
        .select_related(
            'status', 'board_card', 'board_card__created_by', 'department',
            'completed_by', 'cancelled_by',
        )
        .annotate(comment_count=_count_subquery(comments, 'card'))
        .first()
    )
    if task is None:
        return None
    known = loaded.get(card_id)
    if known is not None:
        task._prefetched_objects_cache = dict(known._prefetched_objects_cache)
    else:
        prefetch_related_objects(
            [task], Prefetch('assignees', queryset=TaskAssignee.objects.select_related('user')),
        )
    item = _item(task, board)
    card = task.board_card
    own_columns = columns_of.get(card.sub_board_id, [])
    item['column'] = card_column(card, task, own_columns)
    # Where «Вернуть в работу» puts it: its working column, or the first one
    # if that column was deleted meanwhile.
    item['return_column'] = _return_column(card, own_columns)
    item['moved_to'] = (
        next((tab for tab in tabs if tab.pk == card.sub_board_id), None)
        if card.sub_board_id != sub_board.pk else None
    )
    card.sub_board = item['moved_to'] or sub_board
    item['department'] = task.department
    item['attachments'] = task_attachment_cards(task, user)
    item['can_complete'] = can_complete_task(task, user)
    # An archived board stays as it was shelved: `reopen_card()` refuses it.
    item['can_reopen'] = can_reopen_task(task, user) and not board.is_archived
    item['can_upload_attachment'] = can_upload_task_attachment(task, user)
    item['can_cancel'] = (
        task.status.code == 'IN_PROGRESS' and can_cancel_card(user, card)
    )
    # «Обсуждение»: the newest `COMMENTS_LIMIT` messages, oldest first, in one
    # query sliced by the database — or every one under `all_comments`. How
    # many are left out is the card's `comment_count`, already annotated, so
    # there is no second query. Writing is `can_comment_card()` — i.e.
    # `can_work_on_board()`, already asked for the page — whatever the state
    # of the task.
    messages = (
        BoardCardComment.objects.filter(card=card).select_related('author')
        # Who each message mentions, for the highlight: one query for all
        # the messages shown, none at all while there are none.
        .prefetch_related(Prefetch('mentions', queryset=BoardCardCommentMention.objects.select_related('user')))
    )
    if all_comments:
        item['comments'] = list(messages)
    else:
        item['comments'] = list(messages.order_by('-created_at', '-pk')[:COMMENTS_LIMIT])[::-1]
    item['comments_earlier'] = max(item['comment_count'] - len(item['comments']), 0)
    item['can_comment'] = can_work
    item['log'] = card_log(card, item['attachments'])
    # «Чек-лист»: the items in order, who ticked each one joined — one query.
    item['checklist'] = list(
        BoardCardChecklistItem.objects.filter(card=card).select_related('done_by').order_by('position', 'pk')
    )
    item['checklist_total'] = len(item['checklist'])
    item['checklist_done'] = sum(1 for entry in item['checklist'] if entry.is_done)
    item['can_edit_checklist'] = can_edit_checklist(user, card, task, can_work=can_work)
    # The board's readers who are active employees, each marked whether they
    # follow this card — one query that is both «Подписчики» and the people
    # «@» offers in «Чат» (whoever no longer reads the board is neither).
    readers = list(
        get_user_model().objects.filter(board_readers_q(board))
        .annotate(follows=Exists(BoardCardSubscription.objects.filter(card=card, user=OuterRef('pk'))))
        .distinct()
        .order_by('last_name', 'first_name', 'username', 'pk')
    )
    followers = [person for person in readers if person.follows]
    item['subscribers'] = followers[:SUBSCRIBER_PREVIEW_LIMIT]
    item['subscribers_more'] = max(len(followers) - SUBSCRIBER_PREVIEW_LIMIT, 0)
    item['subscriber_count'] = len(followers)
    item['is_subscribed'] = any(person.pk == user.pk for person in followers)
    # The page is drawn for a reader of the board only.
    item['can_follow'] = can_follow_card(user, card, can_view=True)
    item['mention_people'] = [person for person in readers if person.pk != user.pk] if can_work else []
    attach_field_values(item, fields, (field_rows or {}).get(card.pk, {}))
    return item


# The fields an «Изменение» entry names, in the edit form's order.
EDITED_FIELD_LABELS = {
    'title': 'заголовок',
    'description': 'описание',
    'due_date': 'срок',
    'assignees': 'исполнители',
}


def _place(details, prefix):
    """«Сделать», or «Основная / Сделать» when the entry names the sub-board."""
    column = details.get(f'{prefix}_column') or '—'
    sub_board = details.get(f'{prefix}_sub_board')
    return f'{sub_board} / {column}' if sub_board else column


def describe_card_event(event, code=''):
    """The sentence «Лог» shows for one `BoardCardEvent`.

    Built from the kind and the stored snapshots only — a column's or a
    sub-board's name is the one it had then — and neutral in person:
    «Перенос: «Сделать» → «В работе»», never a verb that would need the
    actor's gender. `code` is the card's («ZAP-12»), named by the entry of
    its creation.
    """
    details = event.details if isinstance(event.details, dict) else {}
    kind = event.kind
    if kind == BoardCardEvent.Kind.CREATED:
        column = details.get('column')
        created = f'Карточка {code} создана' if code else 'Карточка создана'
        return f'{created} в колонке «{column}»' if column else created
    if kind == BoardCardEvent.Kind.EDITED:
        if details.get('by_column'):
            # The people pinned to the column the card came into.
            return f'Исполнители по колонке «{details["by_column"]}»'
        names = []
        for name in details.get('fields') or ():
            if name in EDITED_FIELD_LABELS:
                names.append(EDITED_FIELD_LABELS[name])
            elif name == 'custom':
                # The board's own fields, named as they were at the time.
                names.extend(str(field) for field in details.get('custom_fields') or ())
        return f'Изменено: {", ".join(names)}' if names else 'Карточка изменена'
    if kind == BoardCardEvent.Kind.MOVED:
        return f'Перенос: «{_place(details, "from")}» → «{_place(details, "to")}»'
    if kind == BoardCardEvent.Kind.COMPLETED:
        return 'Задача выполнена'
    if kind == BoardCardEvent.Kind.REOPENED:
        column = details.get('column')
        return f'Возвращена в работу, в колонку «{column}»' if column else 'Возвращена в работу'
    if kind == BoardCardEvent.Kind.CANCELLED:
        return 'Карточка отменена'
    if kind == BoardCardEvent.Kind.CHECKLIST:
        action = CHECKLIST_ACTION_LABELS.get(details.get('action'), 'изменён')
        done, total = details.get('done'), details.get('total')
        counts = f' ({done}/{total})' if isinstance(done, int) and isinstance(total, int) else ''
        return f'Чек-лист: {action}{counts}'
    return event.get_kind_display()


# «Чек-лист: отмечен пункт (3/5)» — the action of a `CHECKLIST` entry, in
# words; a reorder writes no entry and needs none.
CHECKLIST_ACTION_LABELS = {
    'added': 'добавлен пункт',
    'renamed': 'пункт переименован',
    'done': 'отмечен пункт',
    'undone': 'снята отметка с пункта',
    'deleted': 'удалён пункт',
}


def card_log(card, attachments):
    """«Лог» of a card, newest first: its journal and its files.

    The journal is one query (`BoardCardEvent`, its authors joined); the files
    are the panel's own attachment list, already read, so a file costs no
    query here — they are not journal entries, only shown beside them, by
    time: who added which file and when.
    """
    entries = [
        {
            'kind': event.kind.lower(),
            'actor': event.actor,
            'at': event.created_at,
            'text': describe_card_event(event, card.code),
            'order': (event.created_at, 1, event.pk),
        }
        for event in BoardCardEvent.objects.filter(card=card).select_related('actor')
    ]
    entries.extend(
        {
            'kind': 'file',
            'actor': item['object'].uploaded_by,
            'at': item['object'].created_at,
            'text': f'Добавлен файл «{item["object"].original_name}»',
            'order': (item['object'].created_at, 0, item['object'].pk),
        }
        for item in attachments
    )
    entries.sort(key=lambda entry: entry['order'], reverse=True)
    return entries


def _return_column(card, columns):
    working = [column for column in columns if not column.is_done]
    return next(
        (column for column in working if column.pk == card.column_id),
        working[0] if working else None,
    )


def sub_board_columns(sub_board):
    """The sub-board's columns, in order — one query."""
    return list(BoardColumn.objects.filter(sub_board=sub_board).order_by('position', 'pk'))


def first_sub_board(board):
    """The board's first tab — where `/work/boards/<board>/` leads."""
    return SubBoard.objects.filter(board=board).order_by('position', 'pk').first()


def _move_choices(tabs, columns_of, current):
    """«Переместить в…»: every working column of the board, by sub-board.

    `<optgroup>`s in the select — the current sub-board first, then the
    others in tab order — as Django's grouped choices.
    """
    ordered = [current] + [tab for tab in tabs if tab.pk != current.pk]
    return [
        (tab.name, [(column.pk, column.name) for column in columns_of.get(tab.pk, []) if not column.is_done])
        for tab in ordered
        if any(not column.is_done for column in columns_of.get(tab.pk, []))
    ]


def build_board_state(board, sub_board, user, *, done_limit=DONE_LIMIT, card_id=None,
                      filters=NO_FILTERS, all_comments=False, fields=None):
    """Everything one sub-board page renders.

    `sub_boards` are the board's tabs, each `{'sub_board', 'is_active',
    'can_move_left', 'can_move_right'}`. `columns` are this sub-board's own,
    left to right; each is `{'column', 'pk', 'name', 'is_done', 'cards',
    'count', 'more', 'can_move_left', 'can_move_right', 'can_delete',
    'pinned', 'pinned_more', 'pinned_ids'}`, and each card `{'card', 'task',
    'assignees', 'due_date', 'is_closed'}`. The working columns are in
    `position` order. The closing column holds the `done_limit` newest
    completions and `more` counts the rest. A card whose task was cancelled
    is on none of them. The `can_*` flags of a tab or a column say only
    which of ← → «Удалить» to draw for a manager; the services decide.
    `pinned` are the first `PIN_PREVIEW_LIMIT` people pinned to a column
    (its header's avatars), `pinned_more` the rest; `members` — for whoever
    manages the board — the active members its pin menus offer.

    `card` is the card `card_id` names (see `_panel_card()`), else `None` —
    found whatever the filters say, so the open panel never disappears. Its
    «Обсуждение» holds the newest `COMMENTS_LIMIT` messages and counts the
    rest in `comments_earlier`; `all_comments` (`?comments=all`) reads them all.
    `move_choices` are the working columns of every sub-board, grouped, for
    its «Переместить в…».

    `filters` (`BoardFilters`) narrows the columns and their counts: «Мои»
    and the search apply to every column, «Просроченные» to the open ones
    only — a completed card is never overdue.

    Each open card also says what this user may do with it by dragging —
    markup only, the routes ask again: `is_movable` (may work on the board)
    and `can_complete` (`tasks.permissions.can_complete_task()`, asked for the
    whole sub-board in one query through `completable_task_ids()`).

    The board's columns are one query for all its sub-boards — the panel's
    «Переместить в…» offers them all — and the pins of this sub-board's
    columns one more.

    `fields` are the board's own card fields, archived ones too, with their
    options (`board_fields()`), and `fields_stamp` their fingerprint for the
    columns block. Every card — on a column and in the panel — carries
    `field_values` (what it holds, in the fields' order, `describe_field_value()`),
    `tile_values` (the first `TILE_FIELD_LIMIT` a tile shows) and `field_rows`;
    the values of all of them are one query (`card_field_rows()`). A caller
    that has read the fields already — to parse the field filters — passes
    them as `fields`, and they are not read twice.
    """
    from tasks.permissions import completable_task_ids

    can_work = can_work_on_board(user, board)
    can_manage = can_manage_board(user, board)
    tabs = list(SubBoard.objects.filter(board=board).order_by('position', 'pk'))
    columns_of = {}
    for column in BoardColumn.objects.filter(sub_board__board=board).order_by('position', 'pk'):
        columns_of.setdefault(column.sub_board_id, []).append(column)
    columns = columns_of.get(sub_board.pk, [])
    prefetch_related_objects(columns, Prefetch(
        'pinned_assignees',
        queryset=get_user_model().objects.order_by('last_name', 'first_name', 'username', 'pk'),
    ))
    tasks = _board_tasks(sub_board)
    open_tasks = list(
        _filtered(tasks.filter(status__is_final=False), filters, user, open_work=True, columns=columns)
    )
    done_tasks = _filtered(tasks.filter(status__code='COMPLETED'), filters, user, open_work=False)
    completable = completable_task_ids([task.pk for task in open_tasks], user)
    cards_by_column = {column.pk: [] for column in columns}
    for task in open_tasks:
        column = card_column(task.board_card, task, columns)
        if column is not None:
            item = _item(task, board)
            item['is_movable'] = can_work
            item['can_complete'] = task.pk in completable
            item['is_stale'] = is_stale(item['in_column_days'], column)
            cards_by_column[column.pk].append(item)
    for cards in cards_by_column.values():
        cards.sort(key=lambda item: (item['card'].position, item['card'].pk))
    done_column = next((column for column in columns if column.is_done), None)
    done_total = done_tasks.count()
    done_list = []
    if done_column is not None:
        done_list = list(done_tasks.order_by('-completed_at', '-pk')[:done_limit])
        cards_by_column[done_column.pk] = [_item(task, board) for task in done_list]
    # The board's own fields, and the values of every card on the page — the
    # panel's included, whichever it is — in one query.
    if fields is None:
        fields = board_fields(board)
    panel_id = int(card_id) if str(card_id or '').isdigit() else None
    field_rows = card_field_rows(
        fields,
        [task.board_card_id for task in [*open_tasks, *done_list]] + ([panel_id] if panel_id else []),
    )
    for cards in cards_by_column.values():
        for item in cards:
            attach_field_values(item, fields, field_rows.get(item['card'].pk, {}))
    working = [column for column in columns if not column.is_done]
    rows = []
    for column in columns:
        cards = cards_by_column[column.pk]
        total = done_total if column.is_done else len(cards)
        index = working.index(column) if not column.is_done else None
        pinned = list(column.pinned_assignees.all()) if not column.is_done else []
        rows.append({
            'column': column,
            'pk': column.pk,
            'name': column.name,
            'is_done': column.is_done,
            'cards': cards,
            'count': total,
            'more': total - len(cards),
            'can_move_left': index is not None and index > 0,
            'can_move_right': index is not None and index < len(working) - 1,
            'can_delete': index is not None and len(working) > 1,
            'pinned': pinned[:PIN_PREVIEW_LIMIT],
            'pinned_more': max(len(pinned) - PIN_PREVIEW_LIMIT, 0),
            'pinned_all': pinned,
            'pinned_ids': {person.pk for person in pinned},
        })
    loaded = {task.board_card_id: task for task in [*open_tasks, *done_list]}
    return {
        'board': board,
        'sub_board': sub_board,
        'sub_boards': [
            {
                'sub_board': tab,
                'is_active': tab.pk == sub_board.pk,
                'can_move_left': index > 0,
                'can_move_right': index < len(tabs) - 1,
                'can_delete': len(tabs) > 1,
            }
            for index, tab in enumerate(tabs)
        ],
        'columns': rows,
        'first_working_column': working[0] if working else None,
        'done_column': done_column,
        'can_add_column': len(columns) < MAX_COLUMNS,
        'member_count': BoardMember.objects.filter(board=board).count(),
        # Whom a column's «Закреплённые исполнители» may name — drawn for the
        # manager only, so nobody else pays for the query.
        'members': (
            list(
                get_user_model().objects.filter(active_employee_q(), board_memberships__board=board)
                .order_by('last_name', 'first_name', 'username', 'pk')
            ) if can_manage and not board.is_archived else []
        ),
        'card': (
            _panel_card(
                board, sub_board, columns_of, card_id, user,
                loaded=loaded, tabs=tabs, can_work=can_work, all_comments=all_comments,
                fields=fields, field_rows=field_rows,
            )
            if card_id not in (None, '') else None
        ),
        'move_choices': _move_choices(tabs, columns_of, sub_board),
        # Every field of the board, archived ones too, with its options: the
        # card form offers the live ones, «Описание» reads them all.
        'fields': fields,
        'fields_stamp': fields_stamp(fields),
        'can_work': can_work,
        'can_manage': can_manage,
        'can_restore': can_restore_board(user, board),
        'filters': filters,
    }


def column_counts(sub_board, user=None, filters=NO_FILTERS):
    """`{column id: number of cards}` — the numbers the column headers show.

    What a drag's JSON answer carries back so the headers can be corrected:
    the open cards per working column (those whose column was deleted count
    in the first one, where they stand), and every completed one for the
    closing column — under the same `filters` the sub-board is drawn with.
    Keys are the column ids as strings, as JSON writes them anyway.
    """
    from tasks.models import Task

    columns = sub_board_columns(sub_board)
    counts = {str(column.pk): 0 for column in columns}
    working = [column for column in columns if not column.is_done]
    first_working_id = working[0].pk if working else None
    tasks = Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__sub_board=sub_board)
    open_cards = (
        _filtered(tasks.filter(status__is_final=False), filters, user, open_work=True, columns=columns)
        .order_by()
        .values('board_card__column_id')
        .annotate(n=Count('pk'))
    )
    for row in open_cards:
        key = str(row['board_card__column_id'] or first_working_id)
        if key in counts:
            counts[key] += row['n']
    done = next((column for column in columns if column.is_done), None)
    if done is not None:
        counts[str(done.pk)] = _filtered(
            tasks.filter(status__code='COMPLETED'), filters, user, open_work=False,
        ).count()
    return counts


def build_board_nav(user, current_board=None):
    """The left panel of every board page: the boards `user` reads.

    One query — the readable boards (`readable_boards_q()`: every board for
    full access, a member's own otherwise) by name — split here into the live
    ones and the archive. `current_id` is the board the page shows, or `None`
    («Новая доска», the empty state). Not live: a board created or archived
    elsewhere shows on the next page.
    """
    boards = list(
        Board.objects.filter(readable_boards_q(user))
        .only('pk', 'name', 'status')
        .order_by('name', 'pk')
    )
    return {
        'boards': [board for board in boards if not board.is_archived],
        'archived': [board for board in boards if board.is_archived],
        'current_id': getattr(current_board, 'pk', None),
    }


def member_preview(board, limit=MEMBER_PREVIEW_LIMIT):
    """The first `limit` members by name, for the avatars in the board heading.

    One query; how many are left over is the caller's `member_count` minus
    the length of this list.
    """
    return [
        member.user
        for member in BoardMember.objects.filter(board=board)
        .select_related('user')
        .order_by('user__last_name', 'user__first_name', 'user__username', 'pk')[:limit]
    ]


def card_audience(card, task, *, assignees=True, author=True, subscribers=True):
    """Who hears about a card: its исполнители, its author and its followers.

    The one answer for every card notification — a message in «Чат»
    (all three), a cancellation (исполнители and followers), a completion
    (followers and the author) — each caller choosing its parts. Only
    active employees who still read the board (`board_readers_q()`): a
    follower taken off the board, a deactivated author and an inactive
    account drop out here. One query, each person once, by id; whoever acted
    is the caller's to leave out (`exclude_actor`).
    """
    from functools import reduce
    from operator import or_

    from tasks.models import TaskAssignee

    parts = []
    if assignees:
        parts.append(Q(pk__in=TaskAssignee.objects.filter(task=task).values('user_id')))
    if author:
        parts.append(Q(pk=card.created_by_id))
    if subscribers:
        parts.append(Q(pk__in=BoardCardSubscription.objects.filter(card=card).values('user_id')))
    if not parts:
        return []
    return list(
        get_user_model().objects.filter(reduce(or_, parts))
        .filter(board_readers_q(card.board_id))
        .distinct()
        .order_by('pk')
    )


def checklist_counts(card):
    """`(done, total)` of a card's «Чек-лист» — one query."""
    counts = BoardCardChecklistItem.objects.filter(card=card).aggregate(
        total=Count('pk'), done=Count('pk', filter=Q(is_done=True)),
    )
    return counts['done'], counts['total']


def is_stale(days, column):
    """Whether an open card `days` in `column` is stuck: at least the column's
    threshold, not a day earlier. A column without one never says so."""
    threshold = getattr(column, 'stale_after_days', None)
    return bool(threshold) and days is not None and days >= threshold


def _count_subquery(queryset, group_by):
    """`queryset` (filtered on an `OuterRef`) counted, 0 when empty."""
    counted = queryset.order_by().values(group_by).annotate(n=Count('pk')).values('n')
    return Coalesce(Subquery(counted, output_field=IntegerField()), Value(0))


def resolve_new_column(columns, value):
    """The working column `?new=<column id>` names among `columns`, or `None`.

    The closing column, a column of another sub-board and anything that is
    not a number are `None`.
    """
    try:
        column_id = int(value)
    except (TypeError, ValueError):
        return None
    return next(
        (column for column in columns if column.pk == column_id and not column.is_done),
        None,
    )


# --------------------------------------------------------------------------
# «Таблица»: the sub-board — or the whole board — as rows
# --------------------------------------------------------------------------

# `?sort=` of the table → how a row is ordered. A key outside this list (and
# outside `field_<id>` of a live field of the board) is ignored: the rows are
# ordered in Python by these keys alone, so nothing the address says ever
# reaches `order_by()`.
TABLE_SORTS = ('code', 'title', 'column', 'due', 'days', 'created', 'completed')
TABLE_FIELD_SORT_PREFIX = 'field_'

# The words the table and its Excel use for a task's state.
TABLE_STATUS_LABELS = {
    'IN_PROGRESS': 'В работе',
    'COMPLETED': 'Выполнена',
    'CANCELLED': 'Отменена',
}


def _table_field_key(field, row):
    """What a field's value is ordered by: a list option by its place among
    the options, a number and a date as such, a text whatever the case."""
    if field.kind == BoardField.Kind.SELECT:
        option = next((option for option in field.options.all() if option.pk == row.option_id), None)
        return None if option is None else (option.position, option.pk)
    if field.kind == BoardField.Kind.NUMBER:
        return row.value_number
    if field.kind == BoardField.Kind.DATE:
        return row.value_date
    return row.value_text.casefold() if row.value_text else None


def _table_cell(field, row):
    """One field's cell: `value` as the board words it (`describe_field_value()`,
    or `None`), `raw` as Excel takes it — a list option's label («(в архиве)»
    after an archived one), a `Decimal`, a `date`, a text — and `key`, what
    the column is ordered by."""
    if row is None:
        return {'value': None, 'raw': None, 'key': None}
    value = describe_field_value(field, row)
    if value is None:
        return {'value': None, 'raw': None, 'key': None}
    if field.kind == BoardField.Kind.SELECT:
        raw = f'{value["text"]} (в архиве)' if value['is_archived'] else value['text']
    elif field.kind == BoardField.Kind.NUMBER:
        raw = row.value_number
    elif field.kind == BoardField.Kind.DATE:
        raw = row.value_date
    else:
        raw = row.value_text
    return {'value': value, 'raw': raw, 'key': _table_field_key(field, row)}


def parse_table_sort(value, fields):
    """The `?sort=` the table accepts — `''` for anything it does not know."""
    value = (value or '').strip()
    name = value[1:] if value.startswith('-') else value
    if name in TABLE_SORTS:
        return value
    if name.startswith(TABLE_FIELD_SORT_PREFIX):
        field_id = name[len(TABLE_FIELD_SORT_PREFIX):]
        if field_id.isdigit() and any(
            field.pk == int(field_id) and not field.is_archived for field in fields
        ):
            return value
    return ''


def _sorted_rows(rows, sort):
    """`rows` (already in the board's own order) ordered by the accepted
    `sort`: ascending, or descending with a leading «-»; rows with no value
    always last, in the board's order — a stable sort keeps every tie so."""
    if not sort:
        return rows
    descending = sort.startswith('-')
    name = sort.lstrip('-')
    if name.startswith(TABLE_FIELD_SORT_PREFIX):
        field_id = int(name[len(TABLE_FIELD_SORT_PREFIX):])

        def key_of(row):
            return row['field_keys'].get(field_id)
    else:
        def key_of(row):
            return row['sort_keys'][name]
    present = [row for row in rows if key_of(row) is not None]
    missing = [row for row in rows if key_of(row) is None]
    return sorted(present, key=key_of, reverse=descending) + missing


def build_board_table(board, sub_board, user, *, filters=NO_FILTERS, sort='', cancelled=False,
                      whole_board=False, fields=None):
    """Everything «Таблица» shows — and exactly what its Excel holds.

    The cards of `sub_board` — or of every sub-board of the board under
    `whole_board` (`?scope=board`) — one per row: the open ones and **every**
    completed one (no `DONE_LIMIT`), the cancelled ones only under
    `cancelled` (`?cancelled=1`). The board's filters apply as on the board
    (`_filtered()`): «Мои», the search and the field filters to every row;
    «Просроченные» and «Застрявшие» describe open work, so under either the
    table holds the open cards that match — a completed card is neither late
    nor stuck.

    Each row is `{'card', 'task', 'sub_board', 'column', 'column_label',
    'status', 'status_label', 'assignees', 'due_date', 'is_closed',
    'in_column_days', 'is_stale', 'checklist_label', 'cells', 'created',
    'completed'}`: `column`
    is where the board shows the card (`columns.card_column()` — the closing
    column for a completed one), `column_label` its name or «Отменена»;
    `checklist_label` the card's «Чек-лист» as «2/5» (empty without items);
    `cells` one per live field of the board, in order (`_table_cell()`);
    `created`/`completed` local dates. `field_columns` are those live fields.

    The order is the board's — sub-board, column, place in it, the closing
    column newest completion first, the cancelled last — unless `sort`
    (already accepted by `parse_table_sort()`) names another, applied in
    Python by `_sorted_rows()`. Queries: the tabs, the columns of the board,
    the tasks with their cards and statuses, their исполнители, and the
    values of every row — none of them grows with the rows, the fields or the
    values; `fields` read by the caller (to parse the field filters) are not
    read again.
    """
    tabs = list(SubBoard.objects.filter(board=board).order_by('position', 'pk'))
    tab_of = {tab.pk: tab for tab in tabs}
    columns_of = {}
    all_columns = list(BoardColumn.objects.filter(sub_board__board=board).order_by('position', 'pk'))
    for column in all_columns:
        columns_of.setdefault(column.sub_board_id, []).append(column)
    if fields is None:
        fields = board_fields(board)
    live_fields = [field for field in fields if not field.is_archived]

    scope = {'board_card__board': board} if whole_board else {'board_card__sub_board': sub_board}
    tasks = _tasks_with_cards(_all_board_tasks().filter(**scope))
    shown_columns = all_columns if whole_board else columns_of.get(sub_board.pk, [])
    if filters.overdue or filters.stale:
        tasks = _filtered(
            tasks.filter(status__is_final=False), filters, user, open_work=True, columns=shown_columns,
        )
    else:
        codes = ['IN_PROGRESS', 'COMPLETED'] + (['CANCELLED'] if cancelled else [])
        tasks = _filtered(
            tasks.filter(Q(status__is_final=False) | Q(status__code__in=codes)),
            filters, user, open_work=False,
        )
    tasks = list(tasks)
    field_rows = card_field_rows(live_fields, [task.board_card_id for task in tasks])

    rows = []
    for task in tasks:
        item = _item(task, board)
        card = item['card']
        own_columns = columns_of.get(card.sub_board_id, [])
        column = card_column(card, task, own_columns)
        tab = tab_of.get(card.sub_board_id)
        card.sub_board = tab
        status = task.status.code
        item.update({
            'sub_board': tab,
            'column': column,
            'column_label': column.name if column is not None else TABLE_STATUS_LABELS['CANCELLED'],
            'status': status,
            'status_label': TABLE_STATUS_LABELS.get(status, task.status.name),
            'is_stale': is_stale(item['in_column_days'], column) if not item['is_closed'] else False,
            'created': timezone.localtime(card.created_at).date(),
            'completed': (
                timezone.localtime(task.completed_at).date()
                if status == 'COMPLETED' and task.completed_at else None
            ),
        })
        values = field_rows.get(card.pk, {})
        cells = [_table_cell(field, values.get(field.pk)) for field in live_fields]
        item['cells'] = cells
        item['field_keys'] = {field.pk: cell['key'] for field, cell in zip(live_fields, cells)}
        place = (
            tab.position if tab is not None else 0,
            column.position if column is not None else MAX_COLUMNS + 1,
        )
        if status == 'COMPLETED':
            # The closing column draws the newest completion first.
            within = (-(task.completed_at.timestamp() if task.completed_at else 0), card.pk)
        elif status == 'CANCELLED':
            within = (-(task.cancelled_at.timestamp() if task.cancelled_at else 0), card.pk)
        else:
            within = (card.position, card.pk)
        item['checklist_label'] = (
            f"{item['checklist_done']}/{item['checklist_total']}" if item['checklist_total'] else ''
        )
        item['board_order'] = (*place, *within)
        item['sort_keys'] = {
            'code': card.number,
            'title': card.title.casefold(),
            'column': item['board_order'] if column is not None else None,
            'due': task.due_date,
            'days': item['in_column_days'],
            'created': card.created_at,
            'completed': task.completed_at if status == 'COMPLETED' else None,
        }
        rows.append(item)
    rows.sort(key=lambda row: row['board_order'])
    rows = _sorted_rows(rows, sort)
    return {
        'board': board,
        'sub_board': sub_board,
        'sub_boards': [{'sub_board': tab, 'is_active': tab.pk == sub_board.pk} for tab in tabs],
        'rows': rows,
        'first_working_column': next(
            (column for column in columns_of.get(sub_board.pk, []) if not column.is_done), None,
        ),
        'field_columns': live_fields,
        # The same fields as the table's headers, each with its `?sort=` key.
        'table_fields': [
            {'field': field, 'sort_key': f'{TABLE_FIELD_SORT_PREFIX}{field.pk}'} for field in live_fields
        ],
        'fields': fields,
        'sort': sort,
        'filters': filters,
        'cancelled': cancelled,
        'whole_board': whole_board,
    }
