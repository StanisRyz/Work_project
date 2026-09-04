"""Parsing and validation of the one structured form the СМК page posts.

Two repeatable blocks — выявленные несоответствия and корректирующие
мероприятия, the latter with its own repeatable assignee rows — so a
`ModelForm` per block would need formsets and still could not express the rule
that crosses them. This does what `protocols.forms.ProtocolDraftForm` does: it
reads the flat `POST`, rebuilds the rows for re-rendering with their errors,
and hands the service a fully resolved structure.

Every existing finding and measure carries its primary key through the form in
a hidden `-id` field. That identity is what lets `update_smk_source()` compare
a мероприятие with itself before and after the edit and regenerate only the
tasks that really changed — row order alone would not survive a row being added
or removed above it. Ids are resolved against the record being edited, so a
foreign or stale one is simply read as a new row.

Nothing here writes to the database, and nothing here decides permissions.
"""

from datetime import date

from django.contrib.auth.models import User

from accounts.models import Department

from .models import SmkSource


# An audit record, not a bulk import: this cap only stops an absurd or forged
# `TOTAL_FORMS` from turning into thousands of queries. The same number
# `protocols.forms` uses, for the same reason.
MAX_ROWS = 60


class SmkSourceForm:
    """Validates the whole СМК record submitted from the creation page.

    The same form serves the correction page: passing `instance` fills the
    unbound rows from the record as it currently reads. Validation is
    unchanged, and so is the structure handed to the service — an edit is the
    same shape of answer, written by `update_smk_source()` instead of
    `create_smk_source()`.
    """

    def __init__(self, data=None, instance=None):
        self.data = data
        self.instance = instance
        self.is_bound = data is not None
        self.non_field_errors = []
        self.cleaned = None
        self._valid = None
        self.origin = ''
        self.origin_error = ''
        self.audit_date = ''
        self.audit_date_error = ''
        self.non_conformity_rows = []
        self.action_rows = []
        if not self.is_bound:
            if instance is not None:
                self._load_instance(instance)
            else:
                self.non_conformity_rows = [self._empty_non_conformity_row(0)]
                self.action_rows = [self._empty_action_row(0)]

    # ------------------------------------------------------------------ initial

    def _load_instance(self, source):
        """Fill the rows from the record as it reads *now*.

        Only the current findings and measures — a superseded set belongs to
        the cancelled tasks that answer it, and re-offering it for editing
        would resurrect wording the record has already left behind.

        The measure's «связано с несоответствием» is written as the finding's
        *row index*, never its primary key, because that is what the template,
        `smk_form.js` and `_clean_actions()` all speak: the browser renumbers
        rows freely, and an id would stop meaning anything the moment one was
        added or removed.
        """
        self.origin = source.origin
        self.audit_date = source.audit_date.isoformat() if source.audit_date else ''
        positions = {}
        for index, finding in enumerate(source.current_non_conformities):
            positions[finding.pk] = index
            self.non_conformity_rows.append(
                {
                    'index': index,
                    'id': str(finding.pk),
                    'text': finding.text,
                    'errors': {},
                }
            )
        if not self.non_conformity_rows:
            self.non_conformity_rows.append(self._empty_non_conformity_row(0))
        actions = source.current_actions.prefetch_related('assignees__user__userprofile')
        for index, action in enumerate(actions):
            position = positions.get(action.non_conformity_id)
            self.action_rows.append(
                {
                    'index': index,
                    # The measure's own identity, carried through the form so a
                    # correction can tell «это же мероприятие» from «новое».
                    'id': str(action.pk),
                    'text': action.task_text,
                    'due_date': action.due_date.isoformat(),
                    'non_conformity': '' if position is None else str(position),
                    'requires_attachment': action.requires_attachment,
                    'split_for_assignees': action.split_for_assignees,
                    # Strings, because the option partials compare against
                    # `pk|stringformat:'s'` — the same values a POST carries.
                    # The department is each исполнитель's own, so a measure
                    # whose people have since moved still redisplays them.
                    'assignees': [
                        {
                            'user': str(item.user_id),
                            'department': str(
                                getattr(
                                    getattr(item.user, 'userprofile', None),
                                    'department_id', '',
                                ) or ''
                            ),
                        }
                        for item in action.assignees.all()
                    ]
                    or [{'user': '', 'department': ''}],
                    'errors': {},
                }
            )
        if not self.action_rows:
            self.action_rows.append(self._empty_action_row(0))

    @staticmethod
    def _empty_non_conformity_row(index):
        return {'index': index, 'id': '', 'text': '', 'errors': {}}

    @staticmethod
    def _empty_action_row(index):
        return {
            'index': index,
            'id': '',
            'text': '',
            'due_date': '',
            'non_conformity': '',
            'requires_attachment': False,
            'split_for_assignees': False,
            'assignees': [{'user': '', 'department': ''}],
            'errors': {},
        }

    @property
    def origin_choices(self):
        return SmkSource.Origin.choices

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
        # The rows the record being corrected currently has. A submitted `-id`
        # counts only if it is one of these: a foreign, deleted or already
        # superseded key is read as a new row rather than accepted as identity,
        # so a forged POST can never attach itself to somebody else's record.
        self._known_non_conformity_ids = set()
        self._known_action_ids = set()
        if self.instance is not None:
            self._known_non_conformity_ids = set(
                self.instance.current_non_conformities.values_list('pk', flat=True)
            )
            self._known_action_ids = set(
                self.instance.current_actions.values_list('pk', flat=True)
            )

        origin = self._clean_origin()
        audit_date = self._clean_audit_date()
        # The findings are cleaned first because a measure may name one of
        # them: `_clean_actions()` resolves that name against the rows this
        # very request kept, never against what is stored.
        non_conformities = self._clean_non_conformities()
        actions = self._clean_actions()

        self._valid = (
            not self.non_field_errors
            and not self.origin_error
            and not self.audit_date_error
            and not any(
                row['errors']
                for row in (*self.non_conformity_rows, *self.action_rows)
            )
        )
        if self._valid:
            self.cleaned = {
                'origin': origin,
                'audit_date': audit_date,
                'non_conformities': non_conformities,
                'actions': actions,
            }
        return self._valid

    def _clean_origin(self):
        self.origin = self.data.get('origin', '').strip()
        if self.origin not in SmkSource.Origin.values:
            self.origin_error = 'Выберите источник.'
            return ''
        return self.origin

    def _clean_audit_date(self):
        """When the audit happened — required, and never `created_at`."""
        self.audit_date = self.data.get('audit_date', '').strip()
        try:
            return date.fromisoformat(self.audit_date)
        except (TypeError, ValueError):
            self.audit_date_error = 'Укажите дату аудита.'
            return None

    def _clean_non_conformities(self):
        """At least one finding: an audit record with none states nothing.

        Also records, in `self._kept_non_conformities`, which submitted row
        became which position in the kept list. A measure names a finding by
        its row on screen, and blank rows are dropped here — so without that
        map «мероприятие №2 отвечает на несоответствие №2» would silently point
        at the wrong finding.
        """
        cleaned = []
        self._kept_non_conformities = {}
        for index in range(self._count('nonconformities')):
            row = {
                'index': index,
                'id': self.data.get(f'nonconformities-{index}-id', '').strip(),
                'text': self.data.get(f'nonconformities-{index}-text', '').strip(),
                'errors': {},
            }
            self.non_conformity_rows.append(row)
            if row['text']:
                self._kept_non_conformities[index] = len(cleaned)
                cleaned.append({
                    'id': self._known_id(row['id'], self._known_non_conformity_ids),
                    'text': row['text'],
                })
        if not self.non_conformity_rows:
            self.non_conformity_rows.append(self._empty_non_conformity_row(0))
        if not cleaned:
            self.non_conformity_rows[0]['errors']['text'] = (
                'Добавьте хотя бы одно выявленное несоответствие.'
            )
        return cleaned

    def _clean_actions(self):
        """At least one measure — each measure becomes exactly one real task."""
        cleaned = []
        for index in range(self._count('actions')):
            prefix = f'actions-{index}'
            assignee_users = self._getlist(f'{prefix}-assignees')
            assignee_departments = self._getlist(f'{prefix}-assignee_departments')
            row = {
                'index': index,
                # Identity, not data: the row the record already holds, so an
                # unchanged мероприятие keeps the task its исполнитель has.
                'id': self.data.get(f'{prefix}-id', '').strip(),
                'text': self.data.get(f'{prefix}-text', '').strip(),
                'due_date': self.data.get(f'{prefix}-due_date', '').strip(),
                # The finding this measure answers, as the row index on screen.
                # Optional: a measure that answers several findings, or the
                # record as a whole, simply names none.
                'non_conformity': self.data.get(f'{prefix}-non_conformity', '').strip(),
                # A plain answer, never normalized away: a required file means
                # the same on a measure with one исполнитель as on one with
                # five, and an СМК measure is never split between them anyway.
                'requires_attachment': bool(self.data.get(f'{prefix}-requires_attachment')),
                # Presentation only at this point — whether splitting means
                # anything depends on how many исполнителя survive validation,
                # so the answer is normalized against them below.
                'split_for_assignees': bool(self.data.get(f'{prefix}-split_for_assignees')),
                'assignees': [
                    {'user': user, 'department': department}
                    for user, department in zip(assignee_users, assignee_departments)
                ],
                'errors': {},
            }
            self.action_rows.append(row)
            if not row['text']:
                row['errors']['text'] = 'Укажите корректирующее мероприятие.'
            # Resolved against the findings *this request* kept, so a link can
            # never survive the row it pointed at being emptied.
            non_conformity = None
            if row['non_conformity']:
                try:
                    non_conformity = self._kept_non_conformities[int(row['non_conformity'])]
                except (KeyError, TypeError, ValueError):
                    row['errors']['non_conformity'] = (
                        'Выберите несоответствие из добавленных выше.'
                    )
            due_date = None
            try:
                due_date = date.fromisoformat(row['due_date'])
            except (TypeError, ValueError):
                row['errors']['due_date'] = 'Выберите срок.'
            assignees = []
            # The measure's department is the department of its first assignee,
            # chosen right next to them and already checked against their
            # profile below. Asking for it twice would only make it possible to
            # state two different answers — the same rule `protocols.forms`
            # settled on.
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
                    'id': self._known_id(row['id'], self._known_action_ids),
                    'text': row['text'],
                    'department': department,
                    'due_date': due_date,
                    # A position in `non_conformities`, or `None` — never a
                    # primary key: a newly added finding does not exist yet.
                    'non_conformity': non_conformity,
                    'requires_attachment': row['requires_attachment'],
                    # Stored normalized, exactly as `protocols/services.py`
                    # normalizes its own: splitting a measure between one
                    # исполнитель has no meaning, and an un-normalized answer
                    # would make an unchanged measure look changed.
                    'split_for_assignees': row['split_for_assignees'] and len(assignees) > 1,
                    'assignees': assignees,
                }
            )
        if not self.action_rows:
            self.action_rows.append(self._empty_action_row(0))
        if not cleaned and not any(row['errors'] for row in self.action_rows):
            self.action_rows[0]['errors']['text'] = (
                'Добавьте хотя бы одно корректирующее мероприятие.'
            )
        return cleaned

    # ------------------------------------------------------------------ helpers

    def _count(self, block):
        try:
            value = int(self.data.get(f'{block}-TOTAL_FORMS', ''))
        except (TypeError, ValueError):
            self.non_field_errors.append('Форма повреждена, обновите страницу.')
            return 0
        if value < 0 or value > MAX_ROWS:
            self.non_field_errors.append('Превышено допустимое количество строк.')
            return 0
        return value

    @staticmethod
    def _known_id(value, known):
        """The submitted `-id` if the record really holds that row, else `None`.

        `None` means «new row», which is what an added мероприятие, a creation
        and a forged key all are. Identity is never taken on trust.
        """
        try:
            pk = int(value)
        except (TypeError, ValueError):
            return None
        return pk if pk in known else None

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
