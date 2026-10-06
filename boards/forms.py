"""The board forms: a new board, its members, and one card.

They parse and offer choices; they decide nothing. `boards/services.py` checks
every rule again under its locks — who may do it, that every исполнитель is an
active member, that the column is a working one — so a form that let something
through still writes nothing wrong.
"""

from django import forms
from django.contrib.auth import get_user_model

from accounts.templatetags.people import person_name

from .columns import WORK_STAGES
from .models import BoardCard
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
    """A new board: its name and its members — nothing else.

    The owner is whoever submits it, so it is not asked; the rows do not offer
    them (they become a member anyway), and if they are posted all the same
    they are simply dropped — never an error, never a second membership.
    """

    name = forms.CharField(label='Название', max_length=200)
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

class CardForm(forms.Form):
    """One card, new or edited. `stage` matters only when creating."""

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
    stage = forms.ChoiceField(
        choices=[(value, label) for value, label in BoardCard.Stage.choices if value in WORK_STAGES],
        required=False,
        widget=forms.HiddenInput,
    )
    # The `BoardCard.version` the edit form was drawn with, posted back so
    # `update_card(expected_version=…)` can tell a save made against an older
    # card from one made against the current one. Unused when creating.
    version = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, board, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignees'].queryset = active_members(board)


class MoveCardForm(forms.Form):
    stage = forms.ChoiceField(
        label='Переместить в',
        choices=[(value, label) for value, label in BoardCard.Stage.choices if value in WORK_STAGES],
    )
    # Where in the column: before this card, or — empty — at the end. Only
    # dragging sends it; the panel's «Переместить в…» always means the end.
    # Whether the card really is on this board and in that column is
    # `move_card()`'s question, not the form's.
    before_card_id = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)


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
