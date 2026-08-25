"""Parsing and validation of the one structured form the protocol editor posts.

The editor is a single page holding four repeatable blocks — участники,
повестка, «Слушали» and задачи — so a Django `ModelForm` per block would need
four formsets and still could not express the rules that cross them (a speaker
must be one of the participants submitted *in the same request*). This class
does what `acts.forms.ToAnalysisStructureForm` does for the ТО analysis: it
reads the flat `POST`, rebuilds the rows for re-rendering with their errors, and
hands the service a fully resolved structure.

Nothing here writes to the database, and nothing here decides permissions.
"""

from datetime import date

from django import forms
from django.contrib.auth.models import User

from accounts.models import Department
from ecosystem.attachments import validate_attachment_upload

from .models import ProtocolAttachment, ProtocolComment


# A protocol is a meeting record, not a bulk import: this cap only stops an
# absurd or forged `TOTAL_FORMS` from turning into thousands of queries.
MAX_ROWS = 60


class ProtocolDraftForm:
    """Validates the whole protocol draft submitted from the editor page."""

    def __init__(self, protocol, data=None):
        self.protocol = protocol
        self.data = data
        self.is_bound = data is not None
        self.non_field_errors = []
        self.cleaned = None
        self._valid = None
        self.participant_rows = []
        self.agenda_rows = []
        self.speech_rows = []
        self.action_rows = []
        if not self.is_bound:
            self._rows_from_protocol()

    # ------------------------------------------------------------------ initial

    def _rows_from_protocol(self):
        """Rows as they are stored, so the editor opens on the saved draft."""
        self.participant_rows = [
            {
                'index': index,
                'user': str(participant.user_id),
                'department': str(participant.department_id or ''),
                'requires_approval': participant.requires_approval,
                'errors': {},
            }
            # The author is rendered by the template from `Protocol.author` and
            # is never an editable row, so it is not offered here.
            for index, participant in enumerate(
                participant
                for participant in self.protocol.participants.select_related('department')
                if participant.user_id != self.protocol.author_id
            )
        ]
        self.agenda_rows = [
            {'index': index, 'text': item.text, 'errors': {}}
            for index, item in enumerate(self.protocol.agenda_items.all())
        ] or [{'index': 0, 'text': '', 'errors': {}}]
        self.speech_rows = [
            {
                'index': index,
                'speaker': str(speech.speaker.user_id),
                'text': speech.text,
                'errors': {},
            }
            for index, speech in enumerate(
                self.protocol.speeches.select_related('speaker')
            )
        ] or [{'index': 0, 'speaker': '', 'text': '', 'errors': {}}]
        self.action_rows = [
            {
                'index': index,
                'text': action.task_text,
                'due_date': action.due_date.isoformat(),
                'split_for_assignees': action.split_for_assignees,
                'assignees': [
                    {
                        'user': str(assignee.user_id),
                        # The profile row can be missing — it is deletable in
                        # the admin on its own, while the User behind it is
                        # held by PROTECT from ProtocolActionAssignee. The
                        # outer getattr must therefore guard `userprofile`
                        # itself: reading it raises RelatedObjectDoesNotExist,
                        # so a single getattr over `department_id` would never
                        # reach its default and the editor would answer 500.
                        'department': str(
                            getattr(
                                getattr(assignee.user, 'userprofile', None),
                                'department_id', '',
                            ) or ''
                        ),
                    }
                    for assignee in action.assignees.select_related('user__userprofile')
                ],
                'errors': {},
            }
            for index, action in enumerate(
                self.protocol.actions.select_related('department')
            )
        ]

    # ---------------------------------------------------------------- validation

    def is_valid(self):
        if self._valid is not None:
            return self._valid
        if not self.is_bound:
            self._valid = False
            return False

        self._departments = {
            department.pk: department
            for department in Department.objects.filter(is_active=True)
        }
        self._users = {
            user.pk: user
            for user in User.objects.filter(
                is_active=True, userprofile__is_active=True
            ).select_related('userprofile')
        }

        participants = self._clean_participants()
        agenda = self._clean_agenda()
        speeches = self._clean_speeches(participants)
        actions = self._clean_actions()

        self._valid = not self.non_field_errors and not any(
            row['errors']
            for row in (
                *self.participant_rows,
                *self.agenda_rows,
                *self.speech_rows,
                *self.action_rows,
            )
        )
        if self._valid:
            self.cleaned = {
                'participants': participants,
                'agenda': agenda,
                'speeches': speeches,
                'actions': actions,
            }
        return self._valid

    def _clean_participants(self):
        """Additional participants only — the author is not submitted at all."""
        cleaned = []
        seen = {self.protocol.author_id}
        for index in range(self._count('participants')):
            prefix = f'participants-{index}'
            row = {
                'index': index,
                'user': self.data.get(f'{prefix}-user', '').strip(),
                'department': self.data.get(f'{prefix}-department', '').strip(),
                'requires_approval': bool(self.data.get(f'{prefix}-requires_approval')),
                'errors': {},
            }
            self.participant_rows.append(row)
            department = self._department(row['department'])
            user = self._user(row['user'])
            if department is None:
                row['errors']['department'] = 'Выберите подразделение.'
            if user is None:
                row['errors']['user'] = 'Выберите активного сотрудника.'
            elif department is not None:
                profile = getattr(user, 'userprofile', None)
                if profile is None or profile.department_id != department.pk:
                    row['errors']['user'] = (
                        'Сотрудник должен относиться к выбранному подразделению.'
                    )
            if user is not None and user.pk in seen:
                row['errors']['user'] = (
                    'Автор протокола уже участвует в нём.'
                    if user.pk == self.protocol.author_id
                    else 'Этот сотрудник уже добавлен в протокол.'
                )
            if row['errors']:
                continue
            seen.add(user.pk)
            cleaned.append(
                {
                    'user': user,
                    'department': department,
                    'requires_approval': row['requires_approval'],
                }
            )
        return cleaned

    def _clean_agenda(self):
        cleaned = []
        for index in range(self._count('agenda')):
            row = {
                'index': index,
                'text': self.data.get(f'agenda-{index}-text', '').strip(),
                'errors': {},
            }
            self.agenda_rows.append(row)
            if row['text']:
                cleaned.append(row['text'])
        if not self.agenda_rows:
            self.agenda_rows.append({'index': 0, 'text': '', 'errors': {}})
        if not cleaned:
            self.agenda_rows[0]['errors']['text'] = 'Добавьте хотя бы один вопрос повестки.'
        return cleaned

    def _clean_speeches(self, participants):
        """A speaker is one of the participants submitted in this same request.

        That is what turns «участник удалён, но его выступление осталось» into a
        controlled form error instead of a `PROTECT` failure at save time.
        """
        allowed = {self.protocol.author_id, *(item['user'].pk for item in participants)}
        cleaned = []
        for index in range(self._count('speeches')):
            prefix = f'speeches-{index}'
            row = {
                'index': index,
                'speaker': self.data.get(f'{prefix}-speaker', '').strip(),
                'text': self.data.get(f'{prefix}-text', '').strip(),
                'errors': {},
            }
            self.speech_rows.append(row)
            speaker = self._user(row['speaker'])
            if speaker is None or speaker.pk not in allowed:
                row['errors']['speaker'] = 'Выберите выступающего из участников протокола.'
            if not row['text']:
                row['errors']['text'] = 'Заполните текст выступления.'
            if row['errors']:
                continue
            cleaned.append({'speaker_user': speaker, 'text': row['text']})
        if not self.speech_rows:
            self.speech_rows.append({'index': 0, 'speaker': '', 'text': '', 'errors': {}})
        if not cleaned and not any(row['errors'] for row in self.speech_rows):
            self.speech_rows[0]['errors']['text'] = 'Добавьте хотя бы одно выступление.'
        return cleaned

    def _clean_actions(self):
        """Zero tasks is a valid protocol; a task that exists must be complete."""
        cleaned = []
        for index in range(self._count('actions')):
            prefix = f'actions-{index}'
            assignee_users = self._getlist(f'{prefix}-assignees')
            assignee_departments = self._getlist(f'{prefix}-assignee_departments')
            row = {
                'index': index,
                'text': self.data.get(f'{prefix}-text', '').strip(),
                'due_date': self.data.get(f'{prefix}-due_date', '').strip(),
                # Presentation only at this point: whether splitting means
                # anything depends on how many assignees survive validation
                # below, and `_apply_actions()` is what settles it.
                'split_for_assignees': bool(self.data.get(f'{prefix}-split_for_assignees')),
                'assignees': [
                    {'user': user, 'department': department}
                    for user, department in zip(assignee_users, assignee_departments)
                ],
                'errors': {},
            }
            self.action_rows.append(row)
            if not row['text']:
                row['errors']['text'] = 'Укажите задачу.'
            due_date = None
            try:
                due_date = date.fromisoformat(row['due_date'])
            except (TypeError, ValueError):
                row['errors']['due_date'] = 'Выберите срок.'
            assignees = []
            # `ProtocolAction.department` is not asked for separately: it is the
            # department of the first assignee, which is already chosen next to
            # them and already checked against their profile below. Asking twice
            # only made it possible to state two different answers, and the one
            # that reaches the real task is this one.
            department = None
            if len(assignee_users) != len(assignee_departments):
                row['errors']['assignees'] = 'Для каждого исполнителя выберите подразделение.'
            elif not assignee_users:
                row['errors']['assignees'] = 'Выберите хотя бы одного исполнителя.'
            else:
                seen = set()
                for user_value, department_value in zip(assignee_users, assignee_departments):
                    assignee = self._user(user_value)
                    assignee_department = self._department(department_value)
                    if assignee is None:
                        row['errors']['assignees'] = 'Выберите активных сотрудников.'
                        continue
                    if assignee.pk in seen:
                        row['errors']['assignees'] = 'Исполнители не должны повторяться.'
                        continue
                    profile = getattr(assignee, 'userprofile', None)
                    if (
                        assignee_department is None
                        or profile is None
                        or profile.department_id != assignee_department.pk
                    ):
                        row['errors']['assignees'] = (
                            'Исполнитель должен относиться к выбранному подразделению.'
                        )
                        continue
                    seen.add(assignee.pk)
                    assignees.append(assignee)
                    if department is None:
                        department = assignee_department
            if row['errors']:
                continue
            cleaned.append(
                {
                    'text': row['text'],
                    'department': department,
                    'due_date': due_date,
                    'assignees': assignees,
                    'split_for_assignees': row['split_for_assignees'],
                }
            )
        return cleaned

    # ------------------------------------------------------------------ helpers

    def _count(self, block):
        try:
            value = int(self.data.get(f'{block}-TOTAL_FORMS', ''))
        except (TypeError, ValueError):
            self.non_field_errors.append('Форма протокола повреждена, обновите страницу.')
            return 0
        if value < 0 or value > MAX_ROWS:
            self.non_field_errors.append('Превышено допустимое количество строк.')
            return 0
        return value

    def _department(self, value):
        try:
            return self._departments.get(int(value))
        except (TypeError, ValueError):
            return None

    def _user(self, value):
        try:
            return self._users.get(int(value))
        except (TypeError, ValueError):
            return None

    def _getlist(self, key):
        if hasattr(self.data, 'getlist'):
            return [value.strip() for value in self.data.getlist(key)]
        value = self.data.get(key, [])
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value]
        return [value.strip()] if value else []


# --------------------------------------------------------------------------
# Collaboration
#
# Ordinary `ModelForm`s, unlike `ProtocolDraftForm` above: one field each, no
# rules that cross rows, nothing a `ModelForm` cannot already express. The file
# policy is `ecosystem.attachments`, the same one act attachments use — there
# is no second answer to «какой файл можно приложить».
# --------------------------------------------------------------------------


class ProtocolCommentForm(forms.ModelForm):
    class Meta:
        model = ProtocolComment
        fields = ('text',)
        labels = {'text': 'Комментарий'}
        widgets = {
            'text': forms.Textarea(
                attrs={
                    'rows': 2,
                    'placeholder': 'Введите комментарий по протоколу...',
                    'aria-label': 'Комментарий по протоколу',
                }
            ),
        }


class ProtocolAttachmentForm(forms.ModelForm):
    class Meta:
        model = ProtocolAttachment
        fields = ('file', 'description')
        labels = {'file': 'Файл', 'description': 'Описание'}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].required = False
        self.fields['file'].widget.attrs['aria-label'] = 'Выберите файл для загрузки'
        self.fields['description'].widget.attrs['aria-label'] = 'Описание вложения'

    def clean_file(self):
        return validate_attachment_upload(self.cleaned_data['file'])
