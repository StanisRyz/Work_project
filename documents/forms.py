"""The forms the documentation library posts.

Thin: they clean and normalise input, and `documents/services.py` decides
whether the operation may happen. Neither form knows anything about roles.
"""

from django import forms
from django.contrib.auth.models import User

from accounts.models import RETIRED_ROLES, Department, UserProfile
from accounts.templatetags.people import person_name

from .models import Document
from .validators import validate_document_upload


class FolderForm(forms.Form):
    """Creating or renaming a folder — one field, one rule."""

    name = forms.CharField(
        label='Название папки',
        max_length=180,
        strip=True,
        error_messages={'required': 'Укажите название папки.'},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].widget.attrs.update({
            'aria-label': 'Название папки',
            'placeholder': 'Название папки',
            'autocomplete': 'off',
        })


class MultipleFileInput(forms.ClearableFileInput):
    """A file input that accepts a whole selection.

    Django's widget refuses `multiple` by default because a plain `FileField`
    would silently keep only the last file. This one opts in, and
    `MultipleFileField` below is the half that actually validates the list.
    """

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """`FileField` over a selection: cleans every file, returns a list.

    Only the ordinary field checks happen here — «is this an uploaded file at
    all». The library's own policy (size, blocked executables, allowed types)
    is applied per file by `upload_documents()`, deliberately *not* here: a
    form error would refuse the whole selection, and a person dropping ten
    files should get the nine that are fine plus a line saying what was wrong
    with the tenth.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        clean_one = super().clean
        if isinstance(data, (list, tuple)):
            return [clean_one(item, initial) for item in data]
        return [clean_one(data, initial)]


class DocumentUploadForm(forms.Form):
    """The files a manager drops into a folder.

    No display-name field: a document is named after the file it was uploaded
    from, and asking for a name would make no sense for a selection of several.
    Renaming stays a separate, deliberate action.
    """

    file = MultipleFileField(
        label='Файлы',
        error_messages={'required': 'Выберите файл.'},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['file'].widget.attrs.update({
            'multiple': True,
            'aria-label': 'Выберите файлы для загрузки',
        })


def role_choices():
    """Every role an employee can hold today — retired ones are not offered."""
    return [(value, label) for value, label in UserProfile.Role.choices if value not in RETIRED_ROLES]


def department_choices():
    return [(department.pk, department.name) for department in Department.objects.filter(is_active=True).order_by('name')]


def person_choices():
    users = (
        User.objects.filter(is_active=True, userprofile__is_active=True)
        .order_by('last_name', 'first_name', 'username')
    )
    return [(user.pk, person_name(user)) for user in users]


class AcknowledgementFieldsMixin(forms.Form):
    """«Ознакомить»: roles and departments, both optional, both free to combine."""

    ack_roles = forms.MultipleChoiceField(label='Ознакомить роли', required=False,
                                          widget=forms.CheckboxSelectMultiple)
    ack_departments = forms.MultipleChoiceField(label='Ознакомить подразделения', required=False,
                                                widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['ack_roles'].choices = role_choices()
        self.fields['ack_departments'].choices = department_choices()


class DocumentUploadWithAckForm(AcknowledgementFieldsMixin, DocumentUploadForm):
    """The upload dialog: the files plus, optionally, who must read them."""


class AcknowledgementForm(AcknowledgementFieldsMixin):
    """«Разослать на ознакомление» for a document already in force."""


class DocumentVersionForm(AcknowledgementFieldsMixin):
    """A new version: the file, what changed, and optionally who approves it
    and who must read it once it is in force.

    No `name` field on purpose — the document's name is its identity and does
    not follow whatever the new file happened to be called on someone's disk.
    """

    file = forms.FileField(
        label='Файл новой версии',
        error_messages={'required': 'Выберите файл.'},
    )
    comment = forms.CharField(
        label='Что изменилось',
        max_length=500,
        strip=True,
        widget=forms.Textarea(attrs={'rows': 2}),
        error_messages={'required': 'Опишите, что изменилось в новой версии.'},
    )
    revision_label = forms.CharField(label='Изменение (изм.)', max_length=40, required=False, strip=True)
    approvers = forms.MultipleChoiceField(label='Согласующие', required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['approvers'].choices = person_choices()
        self.fields['file'].widget.attrs['aria-label'] = 'Выберите файл новой версии'
        self.fields['comment'].widget.attrs.update({
            'aria-label': 'Что изменилось',
            'placeholder': 'Что изменилось в этой версии',
        })
        self.fields['revision_label'].widget.attrs['placeholder'] = 'напр. 2'
        self.fields['approvers'].widget.attrs['size'] = 6

    def clean_file(self):
        return validate_document_upload(self.cleaned_data['file'])


class DocumentCardForm(forms.Form):
    """The document card. Every field optional except the name — documents
    uploaded before the card existed have none of it."""

    name = forms.CharField(label='Название', max_length=255, strip=True,
                           error_messages={'required': 'Укажите название документа.'})
    designation = forms.CharField(label='Обозначение', max_length=80, required=False, strip=True)
    status = forms.ChoiceField(label='Статус', choices=Document.Status.choices)
    cancellation_reason = forms.CharField(label='Причина отмены', required=False, strip=True,
                                          widget=forms.Textarea(attrs={'rows': 2}))
    effective_date = forms.DateField(label='Дата введения', required=False,
                                     widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    review_date = forms.DateField(label='Дата пересмотра', required=False,
                                  widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    owner_department = forms.ModelChoiceField(label='Подразделение-владелец', required=False,
                                              queryset=Department.objects.none(), empty_label='—')
    responsible = forms.ModelChoiceField(label='Ответственный', required=False,
                                         queryset=User.objects.none(), empty_label='—')

    def __init__(self, *args, document=None, **kwargs):
        if document is not None and 'initial' not in kwargs:
            kwargs['initial'] = {
                'name': document.name,
                'designation': document.designation,
                'status': document.status,
                'cancellation_reason': document.cancellation_reason,
                'effective_date': document.effective_date,
                'review_date': document.review_date,
                'owner_department': document.owner_department_id,
                'responsible': document.responsible_id,
            }
        super().__init__(*args, **kwargs)
        departments = Department.objects.filter(is_active=True)
        people = User.objects.filter(is_active=True, userprofile__is_active=True)
        if document is not None:
            # A department or a person since deactivated stays selectable on
            # the document that already names them — saving the card must not
            # silently clear a value nobody touched.
            if document.owner_department_id:
                departments = departments | Department.objects.filter(pk=document.owner_department_id)
            if document.responsible_id:
                people = people | User.objects.filter(pk=document.responsible_id)
        self.fields['owner_department'].queryset = departments.distinct().order_by('name')
        self.fields['responsible'].queryset = people.distinct().order_by('last_name', 'first_name', 'username')
        self.fields['responsible'].label_from_instance = person_name

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('status') == Document.Status.CANCELLED and not cleaned.get('cancellation_reason'):
            self.add_error('cancellation_reason', 'Укажите причину отмены документа.')
        return cleaned


class FolderAccessForm(forms.Form):
    """Which roles may open a folder. None ticked — open to everyone."""

    roles = forms.MultipleChoiceField(label='Доступ только для ролей', required=False,
                                      widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['roles'].choices = role_choices()
