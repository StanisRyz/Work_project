import re

from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, inlineformset_factory

from references.models import DefectType, Operation

from .models import Act, ActAttachment, ActComment, ActDefect


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
NUMBER_PATTERN = re.compile(r'^[0-9/-]+$')
D11_OPERATION_CODES = ('OPERATIONAL_CONTROL', 'FINAL_CONTROL')
D11_DEFECT_TYPE_CODES = (
    'SIZE_NONCONFORMITY',
    'DEFORMATION',
    'ASYMMETRIC_CUT',
    'OBLIQUE_CUT',
    'GRINDING_SIZE_DEVIATION',
    'END_FACE_DELAMINATION_DAMAGE',
    'CUT_SURFACE_DELAMINATION',
    'OL_WINDING_TENSION_LOSS',
    'WINDING_SHIFT',
    'HIGH_ROUGHNESS',
)


class ActCreateForm(forms.ModelForm):
    class Meta:
        model = Act
        fields = (
            'customer',
            'order_number',
            'znp_number',
            'party_number',
            'nomenclature',
            'operation',
        )
        labels = {
            'customer': 'Заказчик',
            'order_number': 'Номер заказа',
            'znp_number': 'Номер ЗНП',
            'party_number': 'Номер партии',
            'nomenclature': 'Номенклатура',
            'operation': 'Операция',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['operation'].queryset = Operation.objects.filter(
            code__in=D11_OPERATION_CODES,
            is_active=True,
        ).order_by('sort_order', 'name')

    def _clean_number_field(self, field_name):
        value = self.cleaned_data.get(field_name, '').strip()
        if value and not NUMBER_PATTERN.match(value):
            raise ValidationError('Допустимы только цифры, дефис и слэш.')
        return value

    def clean_order_number(self):
        return self._clean_number_field('order_number')

    def clean_znp_number(self):
        return self._clean_number_field('znp_number')

    def clean_party_number(self):
        return self._clean_number_field('party_number')


class ActDefectForm(forms.ModelForm):
    class Meta:
        model = ActDefect
        fields = ('defect_type', 'description', 'detected_at')
        labels = {
            'defect_type': 'Вид дефекта',
            'description': 'Описание дефекта',
            'detected_at': 'Срок обнаружения несоответствия',
        }
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'detected_at': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['defect_type'].queryset = DefectType.objects.filter(
            code__in=D11_DEFECT_TYPE_CODES,
            is_active=True,
        ).order_by('name')


class BaseActDefectFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        completed_forms = [
            form
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get('DELETE', False)
        ]
        if not completed_forms:
            raise ValidationError('Добавьте хотя бы один дефект.')


ActDefectFormSet = inlineformset_factory(
    Act,
    ActDefect,
    form=ActDefectForm,
    formset=BaseActDefectFormSet,
    extra=0,
    min_num=1,
    validate_min=True,
    can_delete=False,
)


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


class ActCloseForm(forms.ModelForm):
    class Meta:
        model = Act
        fields = ('closing_comment',)
        labels = {
            'closing_comment': 'Комментарий закрытия',
        }
        widgets = {
            'closing_comment': forms.Textarea(
                attrs={
                    'rows': 4,
                    'placeholder': 'Кратко укажите основание закрытия акта...',
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['closing_comment'].required = False
