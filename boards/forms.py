"""The board forms: a new board, its members, and one card.

They parse and offer choices; they decide nothing. `boards/services.py` checks
every rule again under its locks — who may do it, that every исполнитель is an
active member, that the column is a working one of the card's own sub-board —
so a form that let something through still writes nothing wrong.
"""

from django import forms
from django.contrib.auth import get_user_model

from accounts.templatetags.people import person_name

from .models import (
    INTAKE_DUE_DAYS_MAX,
    INTAKE_HINT_MAX_LENGTH,
    REQUEST_DESCRIPTION_MAX_LENGTH,
    REQUEST_TITLE_MAX_LENGTH,
    BOARD_CODE_MAX_LENGTH,
    DUE_COMMENT_MAX_LENGTH,
    BoardCard,
    BoardColumn,
    BoardField,
    BoardFieldColor,
)
from .permissions import active_employee_q


def employee_label(user):
    """«ФИО — подразделение», the way a person is picked everywhere on boards."""
    profile = getattr(user, 'userprofile', None)
    department = getattr(profile, 'department', None)
    name = person_name(user)
    return f'{name} — {department.name}' if department is not None else name


class EmployeeMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return employee_label(obj)


class EmployeeRowsField(EmployeeMultipleChoiceField):
    """People picked in «Подразделение | Сотрудник» rows, one value per row.

    Every row posts under the same name, so an empty row is an empty value and
    the same person in two rows is one person: blanks are dropped and repeats
    collapsed before the ordinary choice validation, which still refuses
    anybody outside the queryset — an inactive account sent by hand included.
    The department beside each row is never posted.
    """

    def clean(self, value):
        seen = []
        for item in value or ():
            item = str(item).strip()
            if item and item not in seen:
                seen.append(item)
        return super().clean(seen)


def active_employees():
    """Who may be put on a board: any active employee."""
    return (
        get_user_model().objects.filter(active_employee_q())
        .select_related('userprofile__department')
        .order_by('last_name', 'first_name', 'username')
    )


def active_members(board):
    """The people a card of `board` may be put on."""
    return active_employees().filter(board_memberships__board=board)


class BoardForm(forms.Form):
    """A new board: its name, its code and its members — nothing else.

    The code («ZAP») is checked — format, upper case, uniqueness — by
    `services.create_board()`, whose refusal lands on the form.

    The owner is whoever submits it, so it is not asked; the rows do not offer
    them (they become a member anyway), and if they are posted all the same
    they are simply dropped — never an error, never a second membership.
    """

    name = forms.CharField(label='Название', max_length=200)
    code = forms.CharField(
        label='Код',
        max_length=BOARD_CODE_MAX_LENGTH,
        help_text='2–6 букв или цифр — начало номера каждой карточки, например ZAP-12.',
        widget=forms.TextInput(attrs={'autocapitalize': 'characters', 'spellcheck': 'false'}),
    )
    members = EmployeeRowsField(
        label='Участники',
        queryset=get_user_model().objects.none(),
        required=False,
    )

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        # Querysets are evaluated per form, not once at import.
        self.fields['members'].queryset = active_employees()

    def clean_members(self):
        members = self.cleaned_data['members']
        if self.owner is None:
            return members
        return [user for user in members if user.pk != self.owner.pk]

class FieldOptionSelect(forms.Select):
    """A list field's `<select>`: each option carries its colour.

    `board-option--<colour>` paints the choice in the open list and «●» marks
    it in the closed select — a native `<option>` holds no markup, so a chip
    is not possible there.
    """

    def __init__(self, *args, colors=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.colors = colors or {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        color = self.colors.get(str(value))
        if color:
            option['attrs']['class'] = f'board-option board-option--{color}'
            option['attrs']['data-color'] = color
        return option


# The input each kind of field is typed in; the service parses what it sends.
def _custom_form_field(field, current_row):
    """The form field for one of the board's fields. Raw text in, always: the
    service parses it by kind (`services._clean_field_values()`) and names
    the field in its refusal, so there is one set of rules and messages."""
    attrs = {'data-board-field': field.pk}
    if field.kind == BoardField.Kind.NUMBER:
        widget = forms.TextInput(attrs={**attrs, 'inputmode': 'decimal', 'autocomplete': 'off'})
    elif field.kind == BoardField.Kind.DATE:
        widget = forms.DateInput(attrs={**attrs, 'type': 'date'}, format='%Y-%m-%d')
    elif field.kind == BoardField.Kind.SELECT:
        current_option_id = getattr(current_row, 'option_id', None)
        options = [
            option for option in field.options.all()
            # A choice the card already holds stays offered — marked — even
            # once archived, so saving the card never drops it silently.
            if not option.is_archived or option.pk == current_option_id
        ]
        widget = FieldOptionSelect(
            attrs=attrs,
            choices=[('', '—')] + [
                (str(option.pk), f'● {option.label}' + (' (в архиве)' if option.is_archived else ''))
                for option in options
            ],
            colors={str(option.pk): option.color for option in options},
        )
    else:
        widget = forms.TextInput(attrs={**attrs, 'maxlength': 500})
    return forms.CharField(label=field.name, required=False, strip=False, widget=widget)


def custom_field_name(field):
    """The form's name for one of the board's fields: `field_<id>`."""
    return f'field_{getattr(field, "pk", field)}'


class CardForm(forms.Form):
    """One card, new or edited. `column` matters only when creating.

    After the standard fields come the board's own live fields, in order
    (`field_<id>`, see `_custom_form_field()`); `fields` — the board's fields
    with their options, as `selectors.board_fields()` reads them — saves the
    page a query, and `field_rows` (`{field id: value row}`) are the card's
    current values, which keep an archived list option it holds on offer.
    `field_values()` hands the raw values to the service.
    """

    title = forms.CharField(label='Заголовок', max_length=BoardCard._meta.get_field('title').max_length)
    description = forms.CharField(
        label='Описание', required=False, widget=forms.Textarea(attrs={'rows': 4}),
    )
    due_date = forms.DateField(
        label='Срок',
        widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
    )
    assignees = EmployeeMultipleChoiceField(
        label='Исполнители',
        queryset=get_user_model().objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={'required': 'Укажите хотя бы одного исполнителя.'},
    )
    # The working column `?new=<column_id>` named. Whether it is one of this
    # sub-board's working columns is `create_card()`'s question; empty is the
    # first working column.
    column = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)
    # The `BoardCard.version` the edit form was drawn with, posted back so
    # `update_card(expected_version=…)` can tell a save made against an older
    # card from one made against the current one. Unused when creating.
    version = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)
    # «Причина переноса» and «Комментарий к переносу»: only on the edit form
    # (`editing`). The reason is a raw id — whether a move needs one and
    # whether it is an active reason is `services.update_card()`'s question,
    # and its refusal lands here. The page shows both while the срок in the
    # form differs from the stored one (`board_due.js`); without JavaScript
    # always, marked «если меняете срок».
    due_reason = forms.CharField(label='Причина переноса', required=False, widget=forms.Select)
    due_comment = forms.CharField(
        label='Комментарий к переносу', required=False, max_length=DUE_COMMENT_MAX_LENGTH,
        widget=forms.Textarea(attrs={'rows': 2}),
    )

    def __init__(self, *args, board, fields=None, field_rows=None, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignees'].queryset = active_members(board)
        if editing:
            from references.models import DeviationReason

            self.fields['due_reason'].widget.choices = [('', '— выберите причину —')] + [
                (str(reason.pk), reason.name) for reason in DeviationReason.objects.filter(is_active=True)
            ]
        else:
            del self.fields['due_reason']
            del self.fields['due_comment']
        if fields is None:
            from .selectors import board_fields

            fields = board_fields(board)
        field_rows = field_rows or {}
        self.board_fields = [field for field in fields if not field.is_archived]
        for field in self.board_fields:
            self.fields[custom_field_name(field)] = _custom_form_field(field, field_rows.get(field.pk))

    @property
    def custom_fields(self):
        """The bound fields of the board's own fields, in order — for the template."""
        return [self[custom_field_name(field)] for field in self.board_fields]

    def field_values(self):
        """`{field id: raw value}` of every live field the form holds."""
        return {
            field.pk: self.cleaned_data.get(custom_field_name(field), '')
            for field in self.board_fields
        }


class SubtaskForm(forms.Form):
    """«+ Подзадача» in a card's «Подзадачи»: a title, the исполнители and a
    срок — the form starts from the card's own (`initial`), and
    `services.create_subtask()` checks every one again. A срок later than
    the card's is allowed; the page only warns."""

    title = forms.CharField(
        label='Подзадача', max_length=BoardCard._meta.get_field('title').max_length,
        widget=forms.TextInput(attrs={
            'placeholder': 'Название подзадачи', 'aria-label': 'Название подзадачи',
            'class': 'board-subtasks__title-input',
        }),
    )
    due_date = forms.DateField(
        label='Срок',
        widget=forms.DateInput(attrs={
            'type': 'date', 'data-subtask-due': '', 'aria-label': 'Срок подзадачи',
            'title': 'Срок подзадачи — по умолчанию срок карточки',
        }, format='%Y-%m-%d'),
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
    )
    assignees = EmployeeMultipleChoiceField(
        label='Исполнители',
        queryset=get_user_model().objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={'required': 'Укажите хотя бы одного исполнителя.'},
    )

    def __init__(self, *args, board, members=None, **kwargs):
        super().__init__(*args, prefix='subtask', **kwargs)
        self.fields['assignees'].queryset = active_members(board)
        if members is not None and not self.is_bound:
            # The board's active members the panel has read already: the
            # unbound form draws them without a query of its own. A posted
            # form validates against the queryset, as ever.
            self.fields['assignees'].choices = [(user.pk, employee_label(user)) for user in members]


class MoveCardForm(forms.Form):
    """«Переместить в…» in the panel, and a drag on the board.

    `column_id` is a number, not a choice: a column deleted or never part of
    the card's board is `move_card()`'s refusal, in its own words, so a drag
    into a column somebody has just removed puts the tile back with a
    sentence that says why. The panel's select offers the working columns of
    every sub-board of the board, grouped; a drag only ever reaches this
    sub-board's. Both are the same route and the same service, which accepts
    a working column of any sub-board of the card's own board — the form does
    not tell the two sources apart.
    """

    column_id = forms.IntegerField(
        label='Переместить в', min_value=1,
        widget=forms.Select(attrs={'aria-label': 'Переместить в колонку', 'title': 'Переместить в колонку'}),
    )
    # Where in the column: before this card, or — empty — at the end. Only
    # dragging sends it; the panel's «Переместить в…» always means the end.
    # Whether the card really is on this board and in that column is
    # `move_card()`'s question, not the form's.
    before_card_id = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, columns=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['column_id'].widget.choices = [
            (column.pk, column.name) for column in columns if not column.is_done
        ]


class BoardNameForm(forms.Form):
    """A board's new name. The service trims and asks again."""

    name = forms.CharField(label='Название доски', max_length=200)


class IdListField(forms.Field):
    """Several ids posted under one name, as a list of numbers."""

    widget = forms.MultipleHiddenInput

    def to_python(self, value):
        values = [str(item).strip() for item in value or () if str(item).strip()]
        if not all(item.isdigit() for item in values):
            raise forms.ValidationError('Неверный выбор сотрудников.')
        return sorted({int(item) for item in values})


class BoardCodeForm(forms.Form):
    """A board's new code. `services.change_board_code()` normalises and checks it."""

    code = forms.CharField(label='Код доски', max_length=BOARD_CODE_MAX_LENGTH)


class ColumnPinsForm(forms.Form):
    """«Закреплённые исполнители» of a column: the people ticked and the mode.

    Ids, not a choice of members: whether each is an active member of the
    board is `services.set_column_pins()`'s question, in its own words.
    """

    users = IdListField(required=False)
    mode = forms.ChoiceField(choices=BoardColumn.PinnedMode.choices)


class ColumnNormForm(forms.Form):
    """«Норматив этапа» of a column: a number of working days, or empty for none.

    Only that it is a whole number is checked here; the range and that the
    column is a working one are `services.set_column_norm()`'s, in its
    own words.
    """

    days = forms.IntegerField(label='Норматив этапа, рабочих дней', required=False)


class SubBoardNameForm(forms.Form):
    """A sub-board's name, created or renamed. The services trim and ask again."""

    name = forms.CharField(label='Название поддоски', max_length=100)


class ColumnNameForm(forms.Form):
    """A column's name, created or renamed. The services trim and ask again."""

    name = forms.CharField(label='Название колонки', max_length=60)


class DirectionForm(forms.Form):
    """← or →: one step towards the start or the end."""

    direction = forms.ChoiceField(choices=[('left', '←'), ('right', '→')])


class AddMembersForm(forms.Form):
    """«Участники»: more people, picked in the same rows as on «Новая доска»."""

    users = EmployeeRowsField(
        label='Добавить участников',
        queryset=get_user_model().objects.none(),
        error_messages={'required': 'Выберите сотрудников.'},
    )

    def __init__(self, *args, board, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['users'].queryset = active_employees().exclude(board_memberships__board=board)


class FieldForm(forms.Form):
    """«+ Поле» on «Поля карточек»: a name, a kind and — for a list — its
    options, one `option_label`/`option_color` pair per row (empty rows are
    dropped). The service checks every rule again."""

    name = forms.CharField(label='Название', max_length=60)
    kind = forms.ChoiceField(label='Вид', choices=BoardField.Kind.choices)
    # «Сумма в колонке»: a number only — the service says so for any other.
    sum_in_column = forms.BooleanField(label='Сумма в колонке', required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.option_rows = []
        if self.is_bound:
            labels = self.data.getlist('option_label') if hasattr(self.data, 'getlist') else []
            colors = self.data.getlist('option_color') if hasattr(self.data, 'getlist') else []
            for index, label in enumerate(labels):
                color = colors[index] if index < len(colors) else BoardFieldColor.GRAY
                self.option_rows.append({'label': label, 'color': color or BoardFieldColor.GRAY})

    def options(self):
        """`(label, colour)` of the filled rows — for a list only."""
        if self.cleaned_data.get('kind') != BoardField.Kind.SELECT:
            return []
        return [(row['label'], row['color']) for row in self.option_rows if row['label'].strip()]


class FieldUpdateForm(forms.Form):
    """A field's name, its kind (offered only while it has no values) and
    whether its tile shows it, and «Сумма в колонке»."""

    name = forms.CharField(label='Название', max_length=60)
    kind = forms.ChoiceField(label='Вид', choices=BoardField.Kind.choices, required=False)
    show_on_tile = forms.BooleanField(label='Показывать на плитке', required=False)
    sum_in_column = forms.BooleanField(label='Сумма в колонке', required=False)


class OptionForm(forms.Form):
    """A list option's label and colour, created or changed."""

    label = forms.CharField(label='Подпись', max_length=60)
    color = forms.ChoiceField(label='Цвет', choices=BoardFieldColor.choices)


# ---------------------------------------------------------------------------
# «Приём заявок»
# ---------------------------------------------------------------------------

DATE_INPUT = forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')
DATE_FORMATS = ['%Y-%m-%d', '%d.%m.%Y']


def working_column_choices(columns):
    """`<optgroup>`s of the working columns of a board, by sub-board, in the
    board's order — `columns` read with their sub-board, in order."""
    groups = []
    for column in columns:
        if column.is_done:
            continue
        if not groups or groups[-1][0] != column.sub_board.name:
            groups.append((column.sub_board.name, []))
        groups[-1][1].append((column.pk, column.name))
    return groups


class IntakeForm(forms.Form):
    """«Приём заявок» of a board: on/off, the column, the default срок, the
    hint, the handlers (`handlers`, the board's active members as tick boxes)
    and, per live field, `form_<id>` — «нет» / «в форме» / «обязательно».
    `services.update_intake()` checks every one again."""

    FIELD_MODES = (('off', 'нет'), ('on', 'в форме'), ('required', 'обязательно'))

    enabled = forms.BooleanField(label='Принимать заявки', required=False)
    column = forms.CharField(label='Куда ставить принятую заявку', required=False, widget=forms.Select)
    due_days = forms.IntegerField(
        label='Срок по умолчанию, рабочих дней', required=False, min_value=0, max_value=INTAKE_DUE_DAYS_MAX,
        help_text='Если автор не указал желаемую дату. Пусто — срок назначает принявший.',
    )
    hint = forms.CharField(
        label='Подсказка над формой', required=False, max_length=INTAKE_HINT_MAX_LENGTH,
        widget=forms.Textarea(attrs={'rows': 3}),
    )
    handlers = EmployeeMultipleChoiceField(
        label='Кто разбирает заявки',
        queryset=get_user_model().objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text='Им приходят новые заявки. Никого не отмечено — владелец доски.',
    )

    def __init__(self, *args, board, columns, fields, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['handlers'].queryset = active_members(board)
        self.fields['column'].widget.choices = (
            [('', 'Первая рабочая колонка первой поддоски')] + working_column_choices(columns)
        )
        self.board_fields = [field for field in fields if not field.is_archived]
        for field in self.board_fields:
            self.fields[f'form_{field.pk}'] = forms.ChoiceField(
                label=field.name, choices=self.FIELD_MODES, required=False,
                widget=forms.RadioSelect,
            )

    @property
    def field_rows(self):
        return [(field, self[f'form_{field.pk}']) for field in self.board_fields]

    def form_fields(self):
        """`{field id: (in the form, required)}` for `update_intake()`."""
        modes = {}
        for field in self.board_fields:
            mode = self.cleaned_data.get(f'form_{field.pk}') or 'off'
            modes[field.pk] = (mode in ('on', 'required'), mode == 'required')
        return modes

    @staticmethod
    def initial_for(board, handler_ids, fields):
        initial = {
            'enabled': board.intake_enabled,
            'column': str(board.intake_column_id or ''),
            'due_days': board.intake_due_days,
            'hint': board.intake_hint,
            'handlers': list(handler_ids),
        }
        for field in fields:
            initial[f'form_{field.pk}'] = (
                'required' if field.required_in_request else 'on' if field.in_request_form else 'off'
            )
        return initial


class RequestForm(forms.Form):
    """«Подать заявку»: the title, the description, the desired date and the
    board's fields of its form (`field_<id>`, as on a card, raw text — the
    service parses each one and refuses a required one empty)."""

    title = forms.CharField(
        label='Что нужно сделать', max_length=REQUEST_TITLE_MAX_LENGTH,
        widget=forms.TextInput(attrs={'placeholder': 'Коротко: что нужно сделать'}),
    )
    description = forms.CharField(
        label='Подробности', required=False, max_length=REQUEST_DESCRIPTION_MAX_LENGTH,
        widget=forms.Textarea(attrs={'rows': 5}),
    )
    desired_date = forms.DateField(
        label='Желаемая дата', required=False, widget=DATE_INPUT, input_formats=DATE_FORMATS,
    )

    def __init__(self, *args, fields=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.board_fields = list(fields)
        for field in self.board_fields:
            form_field = _custom_form_field(field, None)
            form_field.label = field.name
            if field.required_in_request:
                form_field.widget.attrs['required'] = 'required'
            self.fields[custom_field_name(field)] = form_field

    @property
    def custom_fields(self):
        return [
            (field, self[custom_field_name(field)]) for field in self.board_fields
        ]

    def field_values(self):
        return {
            field.pk: self.cleaned_data.get(custom_field_name(field), '')
            for field in self.board_fields
        }


class AcceptRequestForm(forms.Form):
    """«Принять»: where the card stands (a working column of any sub-board),
    its исполнители, its срок, and its title and description — all starting
    from the request and the board's intake settings; `accept_request()`
    checks every one again."""

    column = forms.IntegerField(label='Колонка', min_value=1, widget=forms.Select)
    assignees = EmployeeMultipleChoiceField(
        label='Исполнители',
        queryset=get_user_model().objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={'required': 'Укажите хотя бы одного исполнителя.'},
    )
    due_date = forms.DateField(label='Срок', widget=DATE_INPUT, input_formats=DATE_FORMATS)
    title = forms.CharField(label='Заголовок карточки', max_length=BoardCard._meta.get_field('title').max_length)
    description = forms.CharField(
        label='Описание карточки', required=False, widget=forms.Textarea(attrs={'rows': 4}),
    )

    def __init__(self, *args, board, columns, **kwargs):
        super().__init__(*args, prefix='accept', **kwargs)
        self.fields['assignees'].queryset = active_members(board)
        self.fields['column'].widget.choices = working_column_choices(columns)


class DuplicateRequestForm(forms.Form):
    """«Дубль»: the code of the card of this board the request repeats."""

    card_code = forms.CharField(
        label='Код карточки', max_length=20,
        widget=forms.TextInput(attrs={'placeholder': 'например ZAP-12', 'autocomplete': 'off'}),
    )
    comment = forms.CharField(
        label='Комментарий', required=False, widget=forms.Textarea(attrs={'rows': 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, prefix='duplicate', **kwargs)
