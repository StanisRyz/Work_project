"""Forms owned by the tasks app.

Only the attachment upload lives here: task completion is a single textarea
posted straight to the service, and task creation has no form at all — tasks
are made by workflow services, never by hand.
"""

from django import forms

# The one file policy, shared with act and protocol attachments so the three
# can never drift apart. Imported, never restated.
from ecosystem.attachments import validate_attachment_upload

from .models import TaskAttachment


class TaskAttachmentForm(forms.ModelForm):
    """One optional file for an ordinary task.

    Deliberately its own form and its own request: the completion form must
    never carry a file field, or finishing a task would start to look as if it
    needed one.
    """

    class Meta:
        model = TaskAttachment
        fields = ('file',)
        labels = {'file': 'Файл'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['file'].widget.attrs['aria-label'] = 'Выберите файл для загрузки'

    def clean_file(self):
        return validate_attachment_upload(self.cleaned_data['file'])
