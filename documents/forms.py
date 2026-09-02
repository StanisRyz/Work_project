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
