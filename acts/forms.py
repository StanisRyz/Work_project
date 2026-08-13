import re
from datetime import date

from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, inlineformset_factory, modelformset_factory
from django.utils import timezone

from accounts.models import Department
from references.models import DefectType, Operation

from .models import (
    ACT_NUMBER_SUFFIX_LENGTH,
    Act,
    ActAttachment,
    ActComment,
    ActDefect,
    act_number_suffix,
    format_act_number,
)


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
D11_OPERATION_CODES = ('OPERATIONAL_CONTROL', 'FINAL_CONTROL')
# Defect types offered by the МП workshop — unchanged.
MP_DEFECT_TYPE_CODES = (
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
# The ПиР workshop reuses three of the same reference records and nothing else.
PIR_DEFECT_TYPE_CODES = (
    'DEFORMATION',
    'OTHER',
    'SIZE_NONCONFORMITY',
)
# Every defect type any workshop may offer; the per-workshop set below is what
# actually gets validated.
DEFECT_TYPE_CODES = tuple(dict.fromkeys(MP_DEFECT_TYPE_CODES + PIR_DEFECT_TYPE_CODES))
DEFECT_TYPE_CODES_BY_WORKSHOP = {
    ActDefect.Workshop.MP_SHOP: MP_DEFECT_TYPE_CODES,
    ActDefect.Workshop.PIR_SHOP: PIR_DEFECT_TYPE_CODES,
}
# Collected by МП only. A ПиР defect never stores them, whatever the POST says.
MP_ONLY_DEFECT_FIELDS = ('party_number', 'operation', 'mp_type', 'description')
# Per-workshop markers rendered on the defect type options for the browser.
DEFECT_TYPE_OPTION_MARKERS = {
    ActDefect.Workshop.MP_SHOP: 'data-workshop-mp',
    ActDefect.Workshop.PIR_SHOP: 'data-workshop-pir',
}


class ActCreateForm(forms.ModelForm):
    """Act data section. The public number is composed on the server.

    The user types a free-form suffix of up to `ACT_NUMBER_SUFFIX_LENGTH`
    characters; the year and the zero padding are added by
    `acts.models.format_act_number`, never by the browser.
    """

    number_suffix = forms.CharField(
        label='Номер акта',
        max_length=ACT_NUMBER_SUFFIX_LENGTH,
        widget=forms.TextInput(
            attrs={
                'maxlength': ACT_NUMBER_SUFFIX_LENGTH,
                'inputmode': 'text',
                'autocomplete': 'off',
            }
        ),
    )

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

    field_order = ('number_suffix', 'customer', 'order_number', 'nomenclature', 'kd_designation')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ('customer', 'order_number', 'nomenclature', 'kd_designation'):
            self.fields[field_name].required = True
        for field_name in ('order_number',):
            self.fields[field_name].widget.attrs['pattern'] = r'[0-9/-]+'

        self.fields['kd_designation'].widget.attrs['pattern'] = r'^[А-Яа-яЁё0-9.-]+$'

        # Editing an existing act: show the suffix of its current number. A
        # legacy number in another shape has no suffix to show, so the field
        # stays optional and an untouched form keeps that number as it is.
        self.existing_number = self.instance.number if self.instance.pk else ''
        suffix = act_number_suffix(self.existing_number)
        if suffix is not None:
            self.fields['number_suffix'].initial = suffix
        elif self.existing_number:
            self.fields['number_suffix'].required = False
            self.fields['number_suffix'].help_text = (
                f'Текущий номер: {self.existing_number}. Оставьте поле пустым, чтобы сохранить его.'
            )

    def clean_number_suffix(self):
        # Any character the user types is allowed; only the length is bounded.
        return self.cleaned_data.get('number_suffix', '').strip()

    def save(self, commit=True):
        act = super().save(commit=False)
        suffix = self.cleaned_data.get('number_suffix', '')
        if self.existing_number and suffix in ('', act_number_suffix(self.existing_number)):
            # Nothing was changed in the number field: the act keeps the number
            # it already has, including the year it was issued in.
            act.number = self.existing_number
        else:
            act.number = format_act_number(suffix)
        if commit:
            act.save()
            self.save_m2m()
        return act

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

    def clean_kd_designation(self):
        return self._clean_product_field('kd_designation')


class DefectTypeSelect(forms.Select):
    """Marks each option with the workshops that may use that defect type.

    The browser uses the markers to narrow the dropdown; the authoritative
    check stays in `ActDefectForm.clean`.
    """

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        code = getattr(getattr(value, 'instance', None), 'code', '')
        for workshop, codes in DEFECT_TYPE_CODES_BY_WORKSHOP.items():
            if code in codes:
                option['attrs'][DEFECT_TYPE_OPTION_MARKERS[workshop]] = '1'
        return option


class ActDefectForm(forms.ModelForm):
    """One defect row. Which fields apply depends on the selected workshop.

    МП keeps its full field set. ПиР collects only цех, ЗНП, вид дефекта, дата
    обнаружения and the two quantities: the МП-only fields are neither required
    nor saved, so switching a form to ПиР cannot carry stale МП values along.
    """

    class Meta:
        model = ActDefect
        fields = (
            'workshop',
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
            'workshop': 'Цех/поставщик',
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
            'description': forms.Textarea(attrs={'rows': 2}),
            'defect_type': DefectTypeSelect,
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
            code__in=DEFECT_TYPE_CODES,
            is_active=True,
        ).order_by('name')
        self.fields['operation'].queryset = Operation.objects.filter(
            code__in=D11_OPERATION_CODES,
            is_active=True,
        ).order_by('sort_order', 'name')
        self.fields['workshop'].required = True
        for field_name in ('znp_number', 'party_number'):
            self.fields[field_name].widget.attrs['pattern'] = r'[0-9/-]+'
        self.fields['znp_number'].required = True
        self.fields['checked_quantity'].required = True
        self.fields['nonconforming_quantity'].required = True
        # МП-only fields are optional at field level and enforced in `clean()`
        # for МП only, so the browser never marks them required under ПиР.
        for field_name in MP_ONLY_DEFECT_FIELDS:
            self.fields[field_name].required = False
            self.fields[field_name].widget.attrs['data-mp-only'] = '1'

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

        workshop = cleaned_data.get('workshop')
        if workshop == ActDefect.Workshop.PIR_SHOP:
            # Whatever the POST carried for the МП-only fields is dropped, so a
            # form switched from МП to ПиР stores no stale values.
            for field_name in MP_ONLY_DEFECT_FIELDS:
                cleaned_data[field_name] = None if field_name == 'operation' else ''
                self.errors.pop(field_name, None)
        elif workshop == ActDefect.Workshop.MP_SHOP:
            for field_name in MP_ONLY_DEFECT_FIELDS:
                if not cleaned_data.get(field_name):
                    self.add_error(field_name, 'Обязательное поле.')

        defect_type = cleaned_data.get('defect_type')
        allowed_codes = DEFECT_TYPE_CODES_BY_WORKSHOP.get(workshop)
        if defect_type and allowed_codes is not None and defect_type.code not in allowed_codes:
            self.add_error('defect_type', 'Этот вид дефекта недоступен для выбранного цеха.')
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
            'ko_comment': forms.Textarea(attrs={'rows': 2}),
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
            'ko_comment': forms.Textarea(attrs={'rows': 2}),
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
                    'rows': 2,
                    'placeholder': 'Введите комментарий по акту...',
                    'aria-label': 'Комментарий по акту',
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
                            'additional_assignees': [
                                {
                                    'user': str(assignee.user_id),
                                    'department': str(assignee.user.userprofile.department_id or ''),
                                }
                                for assignee in list(action.assignees.all())[1:]
                            ],
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
                    'additional_assignees': [],
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

        departments = {department.pk: department for department in Department.objects.filter(is_active=True)}
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
                    'assignee_departments': [
                        self.data.get(f'{action_prefix}-department', ''),
                        *self._getlist(f'{action_prefix}-assignee_departments'),
                    ],
                    'additional_assignees': [
                        {'user': user, 'department': department}
                        for user, department in zip(
                            self._getlist(f'{action_prefix}-assignees')[1:],
                            self._getlist(f'{action_prefix}-assignee_departments'),
                        )
                    ],
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
                if len(action['assignee_departments']) != len(assignees):
                    action['errors']['assignees'] = 'Для каждого исполнителя выберите отдел.'
                    valid = False
                else:
                    for assignee, department_value in zip(assignees, action['assignee_departments']):
                        assignee_department = self._object_from_value(departments, department_value)
                        profile = getattr(assignee, 'userprofile', None)
                        if (
                            assignee_department is None
                            or profile is None
                            or profile.department_id != assignee_department.pk
                        ):
                            action['errors']['assignees'] = 'Исполнитель должен относиться к выбранному отделу.'
                            valid = False
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
        self.fields['file'].widget.attrs['aria-label'] = 'Выберите файл для загрузки'
        self.fields['description'].widget.attrs['aria-label'] = 'Описание вложения'

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
