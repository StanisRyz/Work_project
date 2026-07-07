from django import forms
from django.core.exceptions import ValidationError

from references.models import DefectType, Operation, Priority

from .models import Act, ActAttachment, ActComment


ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.pdf',
    '.doc',
    '.docx',
    '.xls',
    '.xlsx',
    '.png',
    '.jpg',
    '.jpeg',
    '.webp',
    '.txt',
}
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024


class ActCreateForm(forms.ModelForm):
    class Meta:
        model = Act
        fields = (
            'party_number',
            'nomenclature',
            'operation',
            'defect_type',
            'priority',
            'description',
            'due_date',
        )
        labels = {
            'party_number': 'Номер партии',
            'nomenclature': 'Номенклатура',
            'operation': 'Операция',
            'defect_type': 'Вид дефекта',
            'priority': 'Приоритет',
            'description': 'Описание',
            'due_date': 'Срок рассмотрения',
        }
        widgets = {
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['operation'].queryset = Operation.objects.filter(is_active=True)
        self.fields['defect_type'].queryset = DefectType.objects.filter(is_active=True)
        self.fields['priority'].queryset = Priority.objects.filter(is_active=True)
        self.fields['priority'].required = False


class KoDecisionForm(forms.ModelForm):
    class Meta:
        model = Act
        fields = ('ko_decision', 'ko_comment')
        labels = {
            'ko_decision': 'Решение КО',
            'ko_comment': 'Комментарий КО',
        }
        widgets = {
            'ko_comment': forms.Textarea(attrs={'rows': 5}),
        }


class ToAnalysisForm(forms.ModelForm):
    class Meta:
        model = Act
        fields = ('to_root_cause', 'to_action_summary')
        labels = {
            'to_root_cause': 'Корневая причина',
            'to_action_summary': 'Предлагаемые мероприятия',
        }
        widgets = {
            'to_root_cause': forms.Textarea(attrs={'rows': 5}),
            'to_action_summary': forms.Textarea(attrs={'rows': 5}),
        }


class ActCommentForm(forms.ModelForm):
    class Meta:
        model = ActComment
        fields = ('text',)
        labels = {
            'text': 'Комментарий',
        }
        widgets = {
            'text': forms.Textarea(
                attrs={
                    'rows': 4,
                    'placeholder': 'Введите комментарий по акту...',
                }
            ),
        }


class ActAttachmentForm(forms.ModelForm):
    class Meta:
        model = ActAttachment
        fields = ('file', 'description')
        labels = {
            'file': 'Файл',
            'description': 'Описание',
        }
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].required = False

    def clean_file(self):
        uploaded_file = self.cleaned_data['file']
        extension = uploaded_file.name.rsplit('.', 1)
        extension = f'.{extension[1].lower()}' if len(extension) == 2 else ''
        if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
            raise ValidationError('Недопустимый тип файла.')
        if uploaded_file.size > MAX_ATTACHMENT_SIZE:
            raise ValidationError('Размер файла превышает допустимый лимит.')
        return uploaded_file
