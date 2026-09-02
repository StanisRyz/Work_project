"""The two forms the file browser posts.

Thin: they clean and normalise input, and `documents/services.py` decides
whether the operation may happen. Neither form knows anything about roles.
"""

from django import forms

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


class DocumentUploadForm(forms.Form):
    """One uploaded file, plus an optional display name.

    `file` goes through the library's own policy (size, blocked executables,
    allowed types). The service validates it again — a form is only one of the
    ways a document can arrive.
    """

    file = forms.FileField(
        label='Файл',
        error_messages={'required': 'Выберите файл.'},
    )
    name = forms.CharField(
        label='Название документа',
        max_length=255,
        required=False,
        strip=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['file'].widget.attrs['aria-label'] = 'Выберите файл для загрузки'
        self.fields['name'].widget.attrs.update({
            'aria-label': 'Название документа',
            'placeholder': 'Название (необязательно)',
            'autocomplete': 'off',
        })

    def clean_file(self):
        return validate_document_upload(self.cleaned_data['file'])


class DocumentVersionForm(forms.Form):
    """A new version of an existing document: the file, and why it changed.

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
        required=False,
        strip=True,
        widget=forms.Textarea(attrs={'rows': 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['file'].widget.attrs['aria-label'] = 'Выберите файл новой версии'
        self.fields['comment'].widget.attrs.update({
            'aria-label': 'Комментарий к версии',
            'placeholder': 'Комментарий (необязательно)',
        })

    def clean_file(self):
        return validate_document_upload(self.cleaned_data['file'])
