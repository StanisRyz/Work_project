import re

from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, inlineformset_factory, modelformset_factory

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
    'OTHER',
)


class ActCreateForm(forms.ModelForm):
    class Meta:
        model = Act
        fields = (
            'customer',
            'order_number',
            'nomenclature',
            'kd_designation',
        )
        labels = {
            'customer': 'Заказчик',
            'order_number': 'Заказ покупателя',
            'nomenclature': 'Наименование продукции',
            'kd_designation': 'Обозначение по КД',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ('customer', 'order_number', 'nomenclature', 'kd_designation'):
            self.fields[field_name].required = True
        for field_name in ('order_number',):
            self.fields[field_name].widget.attrs['pattern'] = r'[0-9/-]+'
    def _clean_number_field(self, field_name):
        value = self.cleaned_data.get(field_name, '').strip()
        if value and not NUMBER_PATTERN.match(value):
            raise ValidationError('Допустимы только цифры, дефис и слэш.')
        return value

    def clean_order_number(self):
        return self._clean_number_field('order_number')

class ActDefectForm(forms.ModelForm):
    class Meta:
        model = ActDefect
        fields = (
            'znp_number',
            'party_number',
            'checked_quantity',
            'nonconforming_quantity',
            'detected_at',
            'defect_type',
            'operation',
            'mp_type',
            'description',
        )
        labels = {
            'znp_number': 'Номер ЗНП',
            'party_number': 'Номер партии',
            'defect_type': 'Вид дефекта',
            'operation': 'Операция',
            'mp_type': 'Тип МП',
            'checked_quantity': 'Проверено',
            'nonconforming_quantity': 'С отклонением',
            'description': 'Описание дефекта',
            'detected_at': 'Дата обнаружения несоответствия',
        }
        widgets = {
            'checked_quantity': forms.NumberInput(attrs={'min': 0, 'step': 1}),
            'nonconforming_quantity': forms.NumberInput(attrs={'min': 0, 'step': 1}),
            'description': forms.Textarea(attrs={'rows': 4}),
            'detected_at': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['defect_type'].queryset = DefectType.objects.filter(
            code__in=D11_DEFECT_TYPE_CODES,
            is_active=True,
        ).order_by('name')
        self.fields['operation'].queryset = Operation.objects.filter(
            code__in=D11_OPERATION_CODES,
            is_active=True,
        ).order_by('sort_order', 'name')
        self.fields['operation'].required = True
        self.fields['mp_type'].required = True
        for field_name in ('znp_number', 'party_number'):
            self.fields[field_name].required = True
            self.fields[field_name].widget.attrs['pattern'] = r'[0-9/-]+'
        self.fields['checked_quantity'].required = True
        self.fields['nonconforming_quantity'].required = True

    def clean(self):
        cleaned_data = super().clean()
        checked_quantity = cleaned_data.get('checked_quantity')
        nonconforming_quantity = cleaned_data.get('nonconforming_quantity')
        if (
            checked_quantity is not None
            and nonconforming_quantity is not None
            and nonconforming_quantity > checked_quantity
        ):
            self.add_error(
                'nonconforming_quantity',
                'Количество несоответствующей продукции не может превышать количество проверенной продукции.',
            )
        return cleaned_data

    def _clean_number_field(self, field_name):
        value = self.cleaned_data.get(field_name, '').strip()
        if value and not NUMBER_PATTERN.match(value):
            raise ValidationError('Допустимы только цифры, дефис и слэш.')
        return value

    def clean_znp_number(self):
        return self._clean_number_field('znp_number')

    def clean_party_number(self):
        return self._clean_number_field('party_number')


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
    ko_decision = forms.ChoiceField(choices=Act.KoDecision.new_choices())

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


class ActDefectKoDecisionForm(forms.ModelForm):
    ko_decision = forms.ChoiceField(choices=Act.KoDecision.new_choices())

    class Meta:
        model = ActDefect
        fields = ('ko_decision', 'ko_comment')
        labels = {
            'ko_decision': 'Решение КО',
            'ko_comment': 'Комментарий КО',
        }
        widgets = {
            'ko_comment': forms.Textarea(attrs={'rows': 4}),
        }


ActDefectKoDecisionFormSet = modelformset_factory(
    ActDefect,
    form=ActDefectKoDecisionForm,
    extra=0,
    can_delete=False,
)


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
