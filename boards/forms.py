"""The two board forms: a new board, and one card.

They parse and offer choices; they decide nothing. `boards/services.py` checks
every rule again under its locks — who may do it, that every исполнитель is an
active member, that the column is a working one — so a form that let something
through still writes nothing wrong.
"""

from django import forms
from django.contrib.auth import get_user_model

from accounts.models import Department
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


def active_employees():
    return (
        get_user_model().objects.filter(active_employee_q())
        .select_related('userprofile__department')
        .order_by('last_name', 'first_name', 'username')
    )


def active_members(board):
    """The people a card of `board` may be put on."""
    return active_employees().filter(board_memberships__board=board)


class BoardForm(forms.Form):
    """A new board. The owner is whoever submits it, so it is not asked."""

    name = forms.CharField(label='Название', max_length=200)
    description = forms.CharField(
        label='Описание', required=False, widget=forms.Textarea(attrs={'rows': 3}),
    )
    department = forms.ModelChoiceField(
        label='Подразделение',
        queryset=Department.objects.filter(is_active=True).order_by('name'),
        empty_label='Выберите подразделение',
    )
    members = EmployeeMultipleChoiceField(
        label='Участники',
        queryset=active_employees(),
        required=False,
        widget=forms.SelectMultiple(attrs={'size': 10}),
        help_text='Вы станете владельцем и участником доски сами.',
    )

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Querysets are evaluated per form, not once at import.
        members = active_employees()
        if owner is not None:
            members = members.exclude(pk=owner.pk)
        self.fields['members'].queryset = members
        self.fields['department'].queryset = Department.objects.filter(is_active=True).order_by('name')


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
    users = EmployeeMultipleChoiceField(
        label='Добавить участников',
        queryset=get_user_model().objects.none(),
        widget=forms.SelectMultiple(attrs={'size': 8}),
        error_messages={'required': 'Выберите сотрудников.'},
    )

    def __init__(self, *args, board, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['users'].queryset = active_employees().exclude(board_memberships__board=board)
