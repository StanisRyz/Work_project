"""The board forms: a new board, its members, and one card.

They parse and offer choices; they decide nothing. `boards/services.py` checks
every rule again under its locks — who may do it, that every исполнитель is an
active member, that the column is a working one of the card's own sub-board —
so a form that let something through still writes nothing wrong.
"""

from django import forms
from django.contrib.auth import get_user_model

from accounts.templatetags.people import person_name

from .models import BOARD_CODE_MAX_LENGTH, BoardCard, BoardColumn, BoardField, BoardFieldColor
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

    def __init__(self, *args, board, fields=None, field_rows=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignees'].queryset = active_members(board)
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


class ColumnStaleForm(forms.Form):
    """«Застой» of a column: a number of days, or empty to switch it off.

    Only that it is a whole number is checked here; the range and that the
    column is a working one are `services.set_column_stale_days()`'s, in its
    own words.
    """

    days = forms.IntegerField(label='Застой: подсвечивать через, дней', required=False)


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
    whether its tile shows it."""

    name = forms.CharField(label='Название', max_length=60)
    kind = forms.ChoiceField(label='Вид', choices=BoardField.Kind.choices, required=False)
    show_on_tile = forms.BooleanField(label='Показывать на плитке', required=False)


class OptionForm(forms.Form):
    """A list option's label and colour, created or changed."""

    label = forms.CharField(label='Подпись', max_length=60)
    color = forms.ChoiceField(label='Цвет', choices=BoardFieldColor.choices)
