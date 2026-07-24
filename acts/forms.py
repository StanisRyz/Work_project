import re
from datetime import date

from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, inlineformset_factory, modelformset_factory
from django.utils import timezone

from accounts.models import Department
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
PRODUCT_FIELD_PATTERN = re.compile(r'^[А-Яа-яЁё0-9.-]+$')
PRODUCT_FIELD_ERROR = 'Допустимы только русские буквы, цифры, точки и тире.'
NOMENCLATURE_PATTERN = re.compile(r'^[А-Яа-яЁё0-9. -]+$')
NOMENCLATURE_ERROR = 'Допустимы только русские буквы, цифры, пробелы, точки и тире.'
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

        self.fields['nomenclature'].widget.attrs['pattern'] = r'^[А-Яа-яЁё0-9. -]+$'
        self.fields['kd_designation'].widget.attrs['pattern'] = r'^[А-Яа-яЁё0-9.-]+$'

    def _clean_number_field(self, field_name):
        value = self.cleaned_data.get(field_name, '').strip()
        if value and not NUMBER_PATTERN.match(value):
            raise ValidationError('Допустимы только цифры, дефис и слэш.')
        return value

    def clean_order_number(self):
        return self._clean_number_field('order_number')

    def _clean_product_field(self, field_name):
        value = self.cleaned_data.get(field_name, '').strip()
        if value and not PRODUCT_FIELD_PATTERN.match(value):
            raise ValidationError(PRODUCT_FIELD_ERROR)
        return value

    def clean_nomenclature(self):
        value = self.cleaned_data.get('nomenclature', '').strip()
        if value and not NOMENCLATURE_PATTERN.match(value):
            raise ValidationError(NOMENCLATURE_ERROR)
        return value

    def clean_kd_designation(self):
        return self._clean_product_field('kd_designation')

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
            'detected_at': forms.DateInput(
                attrs={'type': 'date'},
                format='%Y-%m-%d',
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_date = timezone.localdate()
        self.fields['detected_at'].initial = current_date
        self.fields['detected_at'].widget.attrs['max'] = current_date.isoformat()
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

    def clean_detected_at(self):
        detected_at = self.cleaned_data.get('detected_at')
        if detected_at and detected_at > timezone.localdate():
            raise ValidationError(
                'Дата обнаружения несоответствия не может быть позже текущей даты.'
            )
        return detected_at

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


ActDefectEditFormSet = inlineformset_factory(
    Act,
    ActDefect,
    form=ActDefectForm,
    formset=BaseActDefectFormSet,
    extra=0,
    min_num=1,
    validate_min=True,
    can_delete=True,
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['form'] = 'ko-decision-form'


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


class ReturnToOtkForm(forms.Form):
    comment = forms.CharField(
        label='Комментарий к возврату',
        widget=forms.Textarea(
            attrs={
                'rows': 4,
                'placeholder': 'Укажите, какие данные необходимо уточнить или исправить.',
                'required': True,
            }
        ),
    )

    def clean_comment(self):
        comment = self.cleaned_data['comment'].strip()
        if not comment:
            raise forms.ValidationError('Укажите комментарий к возврату.')
        return comment


class ToAnalysisStructureForm:
    """Validates the nested TO analysis fields submitted from the detail page."""

    ROOT_PREFIX = 'root'

    def __init__(self, data=None, root_analyses=None):
        self.data = data
        self.root_rows = []
        self.analysis_data = []
        self.non_field_errors = []
        self.is_bound = data is not None
        self._valid = None
        if not self.is_bound:
            self.root_rows = self._rows_from_analyses(root_analyses) or [self._empty_root(0)]

    @classmethod
    def _rows_from_analyses(cls, root_analyses):
        if not root_analyses:
            return []
        rows = []
        for root_index, root_analysis in enumerate(root_analyses):
            actions = list(root_analysis.corrective_actions.all())
            rows.append(
                {
                    'index': root_index,
                    'root_cause': root_analysis.root_cause,
                    'root_cause_errors': [],
                    'actions': [
                        {
                            'index': action_index,
                            'comment': action.comment,
                            'department': str(action.department_id),
                            'assignees': [str(assignee.user_id) for assignee in action.assignees.all()],
                            'due_date': action.due_date.isoformat(),
                            'errors': {},
                        }
                        for action_index, action in enumerate(actions)
                    ]
                    or [cls._empty_root(root_index)['actions'][0]],
                }
            )
        return rows

    @staticmethod
    def _empty_root(index):
        return {
            'index': index,
            'root_cause': '',
            'root_cause_errors': [],
            'actions': [
                {
                    'index': 0,
                    'comment': '',
                    'department': '',
                    'assignees': [],
                    'due_date': '',
                    'errors': {},
                }
            ],
        }

    def is_valid(self):
        if self._valid is not None:
            return self._valid
        if not self.is_bound:
            self._valid = False
            return False

        root_count = self._parse_count(f'{self.ROOT_PREFIX}-TOTAL_FORMS', 'корневых проработок')
        if root_count is None or root_count < 1:
            self.non_field_errors.append('Добавьте хотя бы одну корневую проработку.')
            self._valid = False
            return False

        departments = {department.pk: department for department in Department.objects.all()}
        users = {
            user.pk: user
            for user in User.objects.select_related('userprofile').all()
        }
        valid = True
        for root_index in range(root_count):
            prefix = f'{self.ROOT_PREFIX}-{root_index}'
            root = self._empty_root(root_index)
            root['root_cause'] = self.data.get(f'{prefix}-root_cause', '').strip()
            root['actions'] = []
            if not root['root_cause']:
                root['root_cause_errors'].append('Укажите корневую причину.')
                valid = False

            action_count = self._parse_count(f'{prefix}-actions-TOTAL_FORMS', 'корректирующих мероприятий')
            if action_count is None or action_count < 1:
                root['root_cause_errors'].append('Добавьте хотя бы одно корректирующее мероприятие.')
                valid = False
                action_count = 0

            valid_actions = []
            for action_index in range(action_count):
                action_prefix = f'{prefix}-actions-{action_index}'
                action = {
                    'index': action_index,
                    'comment': self.data.get(f'{action_prefix}-comment', '').strip(),
                    'department': self.data.get(f'{action_prefix}-department', ''),
                    'assignees': self._getlist(f'{action_prefix}-assignees'),
                    'due_date': self.data.get(f'{action_prefix}-due_date', ''),
                    'errors': {},
                }
                if not action['comment']:
                    action['errors']['comment'] = 'Укажите корректирующее мероприятие.'
                    valid = False
                department = self._object_from_value(departments, action['department'])
                if department is None:
                    action['errors']['department'] = 'Выберите отдел.'
                    valid = False
                assignees = []
                seen_assignees = set()
                if not action['assignees']:
                    action['errors']['assignees'] = 'Выберите хотя бы одного исполнителя.'
                    valid = False
                for value in action['assignees']:
                    assignee = self._object_from_value(users, value)
                    if assignee is None:
                        action['errors']['assignees'] = 'Выберите активных сотрудников.'
                        valid = False
                        continue
                    if assignee.pk in seen_assignees:
                        action['errors']['assignees'] = 'Исполнители не должны повторяться.'
                        valid = False
                        continue
                    seen_assignees.add(assignee.pk)
                    profile = getattr(assignee, 'userprofile', None)
                    if not assignee.is_active or profile is None or not profile.is_active:
                        action['errors']['assignees'] = 'Исполнитель должен быть активен.'
                        valid = False
                    assignees.append(assignee)
                try:
                    due_date = date.fromisoformat(action['due_date'])
                except (TypeError, ValueError):
                    due_date = None
                    action['errors']['due_date'] = 'Выберите срок.'
                    valid = False
                if due_date and due_date < timezone.localdate():
                    action['errors']['due_date'] = 'Срок не может быть раньше текущей даты.'
                    valid = False

                root['actions'].append(action)
                if not action['errors']:
                    valid_actions.append(
                        {
                            'comment': action['comment'],
                            'department': department,
                            'assignees': assignees,
                            'due_date': due_date,
                        }
                    )
            self.root_rows.append(root)
            if root['root_cause'] and len(valid_actions) == action_count:
                self.analysis_data.append({'root_cause': root['root_cause'], 'actions': valid_actions})

        self._valid = valid and not self.non_field_errors
        return self._valid

    def _parse_count(self, key, label):
        try:
            value = int(self.data.get(key, ''))
        except (TypeError, ValueError):
            self.non_field_errors.append(f'Некорректное количество {label}.')
            return None
        if value > 50:
            self.non_field_errors.append(f'Превышено допустимое количество: {label}.')
            return None
        return value

    @staticmethod
    def _object_from_value(objects, value):
        try:
            return objects.get(int(value))
        except (TypeError, ValueError):
            return None

    def _getlist(self, key):
        if hasattr(self.data, 'getlist'):
            return self.data.getlist(key)
        value = self.data.get(key, [])
        return value if isinstance(value, (list, tuple)) else [value] if value else []


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
