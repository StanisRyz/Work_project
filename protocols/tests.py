"""The behaviours the protocol foundation and its draft editor cannot get wrong."""

from datetime import date, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from ecosystem.workdays import add_working_days
from protocols.models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolAction,
    ProtocolActionAssignee,
    ProtocolAgendaItem,
    ProtocolApproval,
    ProtocolHistoryEvent,
    ProtocolParticipant,
    ProtocolSpeech,
    ProtocolType,
)
from protocols.permissions import can_decide_protocol_approval, can_edit_protocol, is_protocol_admin
from protocols.services import (
    ProtocolWorkflowError,
    add_participant,
    add_speech,
    approve_protocol,
    create_protocol,
    delete_draft_protocol,
    return_protocol_for_revision,
    send_protocol_for_approval,
)
from tasks.models import Task
from tasks.services import TaskWorkflowError, complete_task, create_protocol_action_task


class ProtocolCreationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.meeting = ProtocolType.objects.create(
            code='MEETING', name='Совещание', display_order=20
        )
        cls.department = Department.objects.create(name='ОТК', code='OTK')
        cls.author = User.objects.create_user(username='protocol_author', password='demo12345')
        cls.author.first_name = 'Иван'
        cls.author.last_name = 'Петров'
        cls.author.save(update_fields=['first_name', 'last_name'])
        cls.author.userprofile.department = cls.department
        cls.author.userprofile.position = 'Инженер'
        cls.author.userprofile.save(update_fields=['department', 'position'])

    def test_numbering_is_independent_per_type(self):
        first = create_protocol(self.quality, self.author)
        second = create_protocol(self.quality, self.author)
        other_type = create_protocol(self.meeting, self.author)

        self.assertEqual([first.number, second.number], [1, 2])
        # A second type starts its own series from 1 instead of continuing.
        self.assertEqual(other_type.number, 1)

    def test_deleted_draft_releases_the_smallest_free_number(self):
        numbers = [create_protocol(self.quality, self.author) for _ in range(4)]
        delete_draft_protocol(numbers[2], self.author)

        # 1, 2, 4 taken → the gap is reused, not `max + 1`.
        self.assertEqual(create_protocol(self.quality, self.author).number, 3)
        self.assertEqual(create_protocol(self.quality, self.author).number, 5)

    def test_creation_makes_author_participant_and_history_atomically(self):
        protocol = create_protocol(self.quality, self.author)

        self.assertEqual(protocol.status, Protocol.Status.DRAFT)
        self.assertEqual(protocol.revision, 0)

        participant = ProtocolParticipant.objects.get(protocol=protocol, user=self.author)
        self.assertEqual(participant.display_name, 'Иван Петров')
        self.assertEqual(participant.position, 'Инженер')
        self.assertEqual(participant.department_name, 'ОТК')

        # A later profile change must not rewrite the archived snapshot.
        self.author.userprofile.position = 'Начальник ОТК'
        self.author.userprofile.save(update_fields=['position'])
        participant.refresh_from_db()
        self.assertEqual(participant.position, 'Инженер')

        event = ProtocolHistoryEvent.objects.get(protocol=protocol)
        self.assertEqual(event.event_type, ProtocolHistoryEvent.EventType.CREATED)
        self.assertEqual(event.actor, self.author)
        self.assertEqual(event.revision, 0)


def _employee(username, department, first_name='', last_name='', role=UserProfile.Role.OTK):
    user = User.objects.create_user(
        username=username, password='demo12345', first_name=first_name, last_name=last_name
    )
    profile = user.userprofile
    profile.department = department
    profile.role = role
    profile.position = 'Специалист'
    profile.save(update_fields=['department', 'role', 'position'])
    return user


class ProtocolDraftEditorTests(TestCase):
    """The editor's two hard parts: creation from the UI and the atomic save."""

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        ProtocolType.objects.create(code='INACTIVE', name='Отключён', is_active=False)
        cls.department = Department.objects.create(name='ОТК', code='OTK')
        cls.other_department = Department.objects.create(name='ТО', code='TO')
        cls.author = _employee('draft_author', cls.department, 'Иван', 'Петров')
        cls.member = _employee('draft_member', cls.other_department, 'Пётр', 'Сидоров')
        cls.executor = _employee('draft_executor', cls.other_department, 'Анна', 'Кузнецова')

    def setUp(self):
        self.client.force_login(self.author)

    def _payload(self, **overrides):
        due = (timezone.localdate() + timedelta(days=7)).isoformat()
        payload = {
            'participants-TOTAL_FORMS': '1',
            'participants-0-department': str(self.other_department.pk),
            'participants-0-user': str(self.member.pk),
            'participants-0-requires_approval': 'on',
            'agenda-TOTAL_FORMS': '1',
            'agenda-0-text': 'О качестве партии',
            'speeches-TOTAL_FORMS': '1',
            'speeches-0-speaker': str(self.author.pk),
            'speeches-0-text': 'Доложил о результатах контроля.',
            'actions-TOTAL_FORMS': '1',
            'actions-0-text': 'Проверить оснастку',
            'actions-0-due_date': due,
            'actions-0-assignee_departments': str(self.other_department.pk),
            'actions-0-assignees': str(self.executor.pk),
        }
        payload.update(overrides)
        return payload

    def test_type_selection_creates_the_protocol_through_the_service(self):
        response = self.client.post(
            reverse('protocols:create'), {'protocol_type': self.quality.pk}
        )

        protocol = Protocol.objects.get()
        self.assertRedirects(response, reverse('protocols:detail', args=[protocol.pk]))
        # The service stays responsible for numbering, the author participant
        # and the CREATED event — the view adds nothing of its own.
        self.assertEqual((protocol.number, protocol.status), (1, Protocol.Status.DRAFT))
        self.assertEqual(protocol.author, self.author)
        self.assertTrue(protocol.participants.filter(user=self.author).exists())
        self.assertEqual(
            protocol.history_events.get().event_type, ProtocolHistoryEvent.EventType.CREATED
        )

        # The selection page offers only active types, and never a hard-coded one.
        page = self.client.get(reverse('protocols:create'))
        self.assertContains(page, self.quality.name)
        self.assertNotContains(page, 'Отключён')

    def test_structured_draft_is_saved_atomically_and_keeps_snapshots(self):
        protocol = create_protocol(self.quality, self.author)
        url = reverse('protocols:save_draft', args=[protocol.pk])

        self.client.post(url, self._payload())

        self.assertEqual(protocol.participants.count(), 2)
        self.assertEqual(protocol.agenda_items.get().text, 'О качестве партии')
        speech = protocol.speeches.get()
        self.assertEqual(speech.speaker.user, self.author)
        action = protocol.actions.get()
        self.assertEqual(action.department, self.other_department)
        self.assertEqual([a.user for a in action.assignees.all()], [self.executor])
        self.assertEqual(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.EDITED
            ).count(),
            1,
        )
        # A task draft is a `ProtocolAction` and nothing else: no real task is
        # created from it at this stage.
        self.assertEqual(ProtocolAction.objects.count(), 1)

        # The participant snapshot is frozen when the row is added, so a later
        # unrelated edit must not refresh it from the profile.
        member_participant = protocol.participants.get(user=self.member)
        self.member.userprofile.position = 'Начальник ТО'
        self.member.userprofile.save(update_fields=['position'])
        self.client.post(url, self._payload(**{'agenda-0-text': 'Другой вопрос'}))
        member_participant.refresh_from_db()
        self.assertEqual(member_participant.position, 'Специалист')
        self.assertEqual(protocol.agenda_items.get().text, 'Другой вопрос')

        # A speech whose speaker is no longer a participant is refused, and the
        # refusal persists nothing at all from that submission.
        broken = self._payload(**{
            'participants-TOTAL_FORMS': '0',
            'speeches-0-speaker': str(self.member.pk),
            'agenda-0-text': 'Не должно сохраниться',
        })
        broken.pop('participants-0-department')
        broken.pop('participants-0-user')
        broken.pop('participants-0-requires_approval')
        response = self.client.post(url, broken)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(protocol.participants.count(), 2)
        self.assertEqual(protocol.agenda_items.get().text, 'Другой вопрос')
        self.assertEqual(ProtocolSpeech.objects.count(), 1)
        self.assertEqual(ProtocolAgendaItem.objects.count(), 1)
        self.assertEqual(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.EDITED
            ).count(),
            2,
        )

    def test_changing_only_an_action_execution_flag_counts_as_an_edit(self):
        """`_document_snapshot()` covers every persisted column of a decision.

        Both execution flags are stored on `ProtocolAction` and both decide
        what the real task becomes, so a submission that moves nothing else is
        still an edit: `updated_at`, one `EDITED` event and one realtime
        emission, through the existing path and no new mechanism. An identical
        resubmission still produces none of the three.
        """
        protocol = create_protocol(self.quality, self.author)
        url = reverse('protocols:save_draft', args=[protocol.pk])

        def edits():
            return protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.EDITED
            ).count()

        # Two assignees, so «разбить» is not normalized away below two.
        both = {
            'actions-0-assignee_departments': [
                str(self.other_department.pk), str(self.other_department.pk)
            ],
            'actions-0-assignees': [str(self.executor.pk), str(self.member.pk)],
        }
        self.client.post(url, self._payload(**both))
        self.assertEqual(edits(), 1)

        # An identical resubmission is not an edit — the guard still works.
        with patch('protocols.services.emit_protocol_updated') as unchanged_emit:
            self.client.post(url, self._payload(**both))
        self.assertEqual(edits(), 1)
        unchanged_emit.assert_not_called()

        protocol.refresh_from_db()
        before = protocol.updated_at

        # Only «Обязательно вложение» moves.
        with patch('protocols.services.emit_protocol_updated') as emit:
            self.client.post(
                url, self._payload(**both, **{'actions-0-requires_attachment': 'on'})
            )
        self.assertEqual(edits(), 2)
        emit.assert_called_once()
        self.assertTrue(protocol.actions.get().requires_attachment)
        protocol.refresh_from_db()
        self.assertGreater(protocol.updated_at, before)

        # Only «Разбить задачу для участников» moves, on top of it.
        with patch('protocols.services.emit_protocol_updated') as split_emit:
            self.client.post(url, self._payload(**both, **{
                'actions-0-requires_attachment': 'on',
                'actions-0-split_for_assignees': 'on',
            }))
        self.assertEqual(edits(), 3)
        split_emit.assert_called_once()
        action = protocol.actions.get()
        self.assertTrue(action.split_for_assignees)
        self.assertTrue(action.requires_attachment)

    def test_editor_opens_when_an_assignee_has_no_profile_row(self):
        """A missing `UserProfile` is an empty department, never a 500.

        `UserProfile` is deletable in the admin on its own, while the `User`
        behind it is held by `PROTECT` from `ProtocolActionAssignee`. Reading
        `user.userprofile` then raises `RelatedObjectDoesNotExist`, and the
        editor used to answer 500 every time that protocol was reopened.
        """
        protocol = create_protocol(self.quality, self.author)
        self.client.post(reverse('protocols:save_draft', args=[protocol.pk]), self._payload())
        UserProfile.objects.filter(user=self.executor).delete()

        response = self.client.get(reverse('protocols:detail', args=[protocol.pk]))

        self.assertEqual(response.status_code, 200)
        row = response.context['form'].action_rows[0]
        self.assertEqual(row['assignees'][0]['user'], str(self.executor.pk))
        # The person is still named; only their department came back empty.
        self.assertEqual(row['assignees'][0]['department'], '')

    def test_editor_keeps_a_participant_who_changed_department(self):
        """The saved choice survives being reopened, and the row can warn.

        The employee filtering is the browser's, but it can only preserve what
        the server rendered: the stored user must still come back as the
        selected `<option>` of a row whose department is the stored one, and
        every pair must carry the `[data-pair-warning]` the script reports the
        mismatch in instead of clearing the field.
        """
        protocol = create_protocol(self.quality, self.author)
        self.client.post(reverse('protocols:save_draft', args=[protocol.pk]), self._payload())
        # The participant moves after the draft was saved.
        self.member.userprofile.department = self.department
        self.member.userprofile.save(update_fields=['department'])

        response = self.client.get(reverse('protocols:detail', args=[protocol.pk]))

        self.assertEqual(response.status_code, 200)
        editor = response.context['form']
        row = editor.participant_rows[0]
        self.assertEqual(row['user'], str(self.member.pk))
        # The row still carries the department it was saved with, so the
        # mismatch is visible rather than resolved by dropping the person.
        self.assertEqual(row['department'], str(self.other_department.pk))
        self.assertContains(
            response,
            f'<option value="{self.member.pk}" data-department-id="{self.department.pk}" selected>',
            html=False,
        )
        self.assertContains(response, 'data-pair-warning')


class ProtocolAccessTests(TestCase):
    """Who may read, edit and delete — enforced on the backend, not by hiding."""

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.department = Department.objects.create(name='ОТК', code='OTK')
        cls.author = _employee('access_author', cls.department, 'Иван', 'Петров')
        cls.reader = _employee('access_reader', cls.department, 'Олег', 'Смирнов')
        cls.admin = _employee(
            'access_admin', cls.department, 'Мария', 'Орлова', role=UserProfile.Role.ADMIN
        )

    def test_read_only_admin_edit_and_author_only_delete(self):
        protocol = create_protocol(self.quality, self.author)
        detail = reverse('protocols:detail', args=[protocol.pk])
        delete = reverse('protocols:delete', args=[protocol.pk])
        save = reverse('protocols:save_draft', args=[protocol.pk])

        # Every authenticated user reads every protocol, without edit controls.
        self.client.force_login(self.reader)
        response = self.client.get(detail)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Сохранить черновик')
        self.assertNotContains(response, 'Удалить черновик')
        # Hiding the button is not the check: the route refuses it too.
        self.assertEqual(self.client.post(save, {}).status_code, 403)
        self.assertEqual(self.client.post(delete).status_code, 400)
        self.assertTrue(Protocol.objects.filter(pk=protocol.pk).exists())

        # The administrator may edit someone else's draft but is not offered the
        # user-facing delete for it.
        self.client.force_login(self.admin)
        response = self.client.get(detail)
        self.assertContains(response, 'Сохранить черновик')
        self.assertNotContains(response, 'Удалить черновик')
        self.assertEqual(self.client.post(delete).status_code, 400)
        self.assertTrue(Protocol.objects.filter(pk=protocol.pk).exists())

        # The author deletes their own draft and returns to the registry.
        self.client.force_login(self.author)
        self.assertContains(self.client.get(detail), 'Удалить черновик')
        response = self.client.post(delete)
        self.assertRedirects(response, reverse('protocols:list'))
        self.assertFalse(Protocol.objects.filter(pk=protocol.pk).exists())
        self.assertEqual(ProtocolParticipant.objects.count(), 0)


class MasProtocolPermissionTests(TestCase):
    """MAS follows the ordinary author/approver rules and gains no admin path."""

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.mas_department = Department.objects.get(code='MAS')
        cls.other_department = Department.objects.create(name='ТО', code='MAS_TEST_TO')
        cls.mas = _employee(
            'mas_protocol', cls.mas_department, 'Максим', 'Смирнов', role=UserProfile.Role.MAS
        )
        cls.other_author = _employee(
            'mas_other_author', cls.other_department, 'Анна', 'Иванова'
        )
        cls.reviewer = _employee(
            'mas_reviewer', cls.other_department, 'Олег', 'Петров'
        )

    def _payload(self, author, approver):
        return {
            'participants-TOTAL_FORMS': '1',
            'participants-0-department': str(approver.userprofile.department_id),
            'participants-0-user': str(approver.pk),
            'participants-0-requires_approval': 'on',
            'agenda-TOTAL_FORMS': '1',
            'agenda-0-text': 'План производства',
            'speeches-TOTAL_FORMS': '1',
            'speeches-0-speaker': str(author.pk),
            'speeches-0-text': 'Представлен план производства.',
            'actions-TOTAL_FORMS': '1',
            'actions-0-text': 'Проверить выполнение плана',
            'actions-0-due_date': (timezone.localdate() + timedelta(days=7)).isoformat(),
            'actions-0-assignee_departments': str(approver.userprofile.department_id),
            'actions-0-assignees': str(approver.pk),
        }

    def test_mas_creates_edits_and_submits_only_their_own_protocol(self):
        self.assertFalse(is_protocol_admin(self.mas))
        self.client.force_login(self.mas)
        self.assertEqual(self.client.get(reverse('protocols:create')).status_code, 200)

        created = self.client.post(
            reverse('protocols:create'), {'protocol_type': self.quality.pk}
        )
        protocol = Protocol.objects.get(author=self.mas)
        self.assertRedirects(
            created, reverse('protocols:detail', args=[protocol.pk])
        )

        payload = self._payload(self.mas, self.reviewer)
        saved = self.client.post(
            reverse('protocols:save_draft', args=[protocol.pk]), payload
        )
        self.assertRedirects(saved, reverse('protocols:detail', args=[protocol.pk]))

        submitted = self.client.post(
            reverse('protocols:send_for_approval', args=[protocol.pk]), payload
        )
        self.assertRedirects(submitted, reverse('protocols:detail', args=[protocol.pk]))
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)

        deletable = create_protocol(self.quality, self.mas)
        deleted = self.client.post(reverse('protocols:delete', args=[deletable.pk]))
        self.assertRedirects(deleted, reverse('protocols:list'))
        self.assertFalse(Protocol.objects.filter(pk=deletable.pk).exists())

        other_protocol = create_protocol(self.quality, self.other_author)
        self.assertFalse(can_edit_protocol(other_protocol, self.mas))
        refused = self.client.post(
            reverse('protocols:save_draft', args=[other_protocol.pk]),
            self._payload(self.other_author, self.reviewer),
        )
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(other_protocol.agenda_items.count(), 0)

    def test_mas_decides_only_an_approval_actually_assigned_to_them(self):
        assigned = create_protocol(self.quality, self.other_author)
        self.client.force_login(self.other_author)
        self.client.post(
            reverse('protocols:send_for_approval', args=[assigned.pk]),
            self._payload(self.other_author, self.mas),
        )
        assigned.refresh_from_db()
        self.assertEqual(assigned.status, Protocol.Status.APPROVAL)

        self.client.force_login(self.mas)
        self.assertTrue(can_decide_protocol_approval(assigned, self.mas))
        approved = self.client.post(reverse('protocols:approve', args=[assigned.pk]))
        self.assertRedirects(approved, reverse('protocols:detail', args=[assigned.pk]))
        self.assertEqual(
            ProtocolApproval.objects.get(protocol=assigned, user=self.mas).status,
            ProtocolApproval.Status.APPROVED,
        )

        unassigned = create_protocol(self.quality, self.other_author)
        self.client.force_login(self.other_author)
        self.client.post(
            reverse('protocols:send_for_approval', args=[unassigned.pk]),
            self._payload(self.other_author, self.reviewer),
        )
        unassigned.refresh_from_db()

        self.client.force_login(self.mas)
        self.assertFalse(can_decide_protocol_approval(unassigned, self.mas))
        refused = self.client.post(reverse('protocols:approve', args=[unassigned.pk]))
        self.assertEqual(refused.status_code, 400)
        returned = self.client.post(
            reverse('protocols:return_for_revision', args=[unassigned.pk]),
            {'comment': 'Попытка не назначенного согласующего.'},
        )
        self.assertEqual(returned.status_code, 400)
        self.assertEqual(
            ProtocolApproval.objects.get(protocol=unassigned, user=self.reviewer).status,
            ProtocolApproval.Status.PENDING,
        )


class ProtocolApprovalWorkflowTests(TestCase):
    """The approval workflow's three hard parts, one scenario each.

    Not a state matrix: the refusals are single `if` statements, while the
    behaviours that can genuinely go wrong are *who must sign*, *what a return
    resets*, and *what archiving creates*. Those are what is exercised here,
    end to end through the services.
    """

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.otk = Department.objects.create(name='ОТК', code='OTK')
        cls.to = Department.objects.create(name='ТО', code='TO')
        cls.author = _employee('wf_author', cls.otk, 'Иван', 'Петров')
        cls.reviewer = _employee('wf_reviewer', cls.to, 'Пётр', 'Сидоров')
        cls.executor = _employee('wf_executor', cls.to, 'Анна', 'Кузнецова')
        cls.observer = _employee('wf_observer', cls.otk, 'Ольга', 'Смирнова')

    def _build_protocol(self, *, approvers=(), action_assignees=(), with_action=True):
        """A structurally complete protocol, written the way the editor writes one."""
        protocol = create_protocol(self.quality, self.author)
        for order, user in enumerate(approvers, start=1):
            add_participant(protocol, user, department=self.to, requires_approval=True, display_order=order)
        ProtocolAgendaItem.objects.create(protocol=protocol, text='О качестве партии', display_order=0)
        add_speech(
            protocol,
            protocol.participants.get(user=self.author),
            'Доложил о результатах контроля.',
        )
        if with_action:
            action = ProtocolAction.objects.create(
                protocol=protocol,
                task_text='Проверить оснастку',
                department=self.to,
                due_date=timezone.localdate() + timedelta(days=7),
                display_order=0,
            )
            for user in action_assignees:
                ProtocolActionAssignee.objects.create(action=action, user=user)
        return protocol

    def test_required_approvers_deduplicate_exclude_the_author_and_get_a_two_working_day_task(self):
        # The reviewer is required twice over — an approval-marked participant
        # *and* an action assignee — and the author is an assignee too.
        protocol = self._build_protocol(
            approvers=[self.reviewer],
            action_assignees=[self.reviewer, self.executor, self.author],
        )

        with patch('protocols.services.timezone.localdate', return_value=date(2026, 8, 20)):
            # A Thursday: +2 working days steps over the weekend to Monday.
            send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()

        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.APPROVAL, 1))
        approvals = {a.user_id: a for a in ProtocolApproval.objects.filter(protocol=protocol)}
        # One row per person, never one per reason, and never one for the author.
        self.assertEqual(set(approvals), {self.reviewer.pk, self.executor.pk})
        self.assertEqual(
            (approvals[self.reviewer.pk].required_as_participant,
             approvals[self.reviewer.pk].required_as_action_assignee),
            (True, True),
        )
        self.assertEqual(
            (approvals[self.executor.pk].required_as_participant,
             approvals[self.executor.pk].required_as_action_assignee),
            (False, True),
        )
        # The snapshot is frozen at submission and does not follow the profile.
        self.reviewer.userprofile.position = 'Начальник ТО'
        self.reviewer.userprofile.save(update_fields=['position'])
        approvals[self.reviewer.pk].refresh_from_db()
        self.assertEqual(approvals[self.reviewer.pk].position, 'Специалист')

        for approval in approvals.values():
            task = approval.task
            self.assertEqual(task.source_type, Task.SourceType.PROTOCOL_APPROVAL)
            self.assertEqual((task.protocol_id, task.protocol_action_id), (protocol.pk, None))
            self.assertIsNone(task.act_id)
            self.assertEqual(task.created_by, self.author)
            self.assertEqual(task.status.code, 'IN_PROGRESS')
            self.assertEqual(task.due_date, date(2026, 8, 24))
            self.assertEqual(task.task_text, 'Согласовать протокол Качество №1')
            self.assertEqual([a.user_id for a in task.assignees.all()], [approval.user_id])
        # Every weekday, in one place, so the rule cannot drift per caller.
        self.assertEqual(
            [add_working_days(date(2026, 8, 17) + timedelta(days=offset), 2) for offset in range(7)],
            [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24),
             date(2026, 8, 25), date(2026, 8, 25), date(2026, 8, 25)],
        )
        # And an approval task is still not completable the ordinary way.
        with self.assertRaises(TaskWorkflowError):
            complete_task(approvals[self.reviewer.pk].task, self.reviewer, 'Готово')

        # A protocol nobody must approve — only the author, and the author as
        # the single assignee — must not park in `APPROVAL` waiting for a
        # signature that can never arrive.
        solo = self._build_protocol(action_assignees=[self.author])
        send_protocol_for_approval(solo, self.author)
        solo.refresh_from_db()
        self.assertEqual((solo.status, solo.revision), (Protocol.Status.ARCHIVED, 1))
        self.assertFalse(ProtocolApproval.objects.filter(protocol=solo).exists())
        self.assertEqual(
            Task.objects.get(protocol=solo).source_type, Task.SourceType.PROTOCOL_ACTION
        )
        self.assertEqual(
            [e.event_type for e in solo.history_events.order_by('pk')][-3:],
            [
                ProtocolHistoryEvent.EventType.SENT_FOR_APPROVAL,
                ProtocolHistoryEvent.EventType.ARCHIVED,
                ProtocolHistoryEvent.EventType.TASKS_CREATED,
            ],
        )

    def test_a_return_reopens_editing_and_the_resubmission_requires_every_signature_again(self):
        protocol = self._build_protocol(
            approvers=[self.reviewer, self.observer], action_assignees=[self.executor]
        )
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        first = {a.user_id: a for a in ProtocolApproval.objects.filter(protocol=protocol, revision=1)}

        # The executor approves, then the reviewer sends the whole round back
        # while the observer is still pending.
        approve_protocol(protocol, self.executor)
        return_protocol_for_revision(protocol, self.reviewer, 'Уточнить формулировку решения.')
        protocol.refresh_from_db()

        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.REVISION, 1))
        for approval in first.values():
            approval.refresh_from_db()
        # An approval already given stays a historical fact; the returner's row
        # keeps the comment. Nothing from revision 1 is deleted.
        self.assertEqual(first[self.executor.pk].status, ProtocolApproval.Status.APPROVED)
        self.assertEqual(first[self.reviewer.pk].status, ProtocolApproval.Status.RETURNED)
        self.assertEqual(
            first[self.reviewer.pk].return_comment, 'Уточнить формулировку решения.'
        )
        self.assertEqual(first[self.observer.pk].status, ProtocolApproval.Status.CANCELLED)
        for approval in first.values():
            approval.task.refresh_from_db()
            self.assertEqual(approval.task.status.code, 'COMPLETED')
        # The cancelled task is closed without naming an approver: nobody is
        # going to pretend the observer decided anything.
        self.assertIsNone(first[self.observer.pk].task.completed_by)
        self.assertEqual(first[self.reviewer.pk].task.completed_by, self.reviewer)
        self.assertTrue(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.RETURNED_FOR_REVISION,
                revision=1,
                message__contains='Уточнить формулировку решения.',
            ).exists()
        )
        # `REVISION` is editable by exactly the same people as a draft.
        self.assertTrue(can_edit_protocol(protocol, self.author))
        self.assertFalse(can_edit_protocol(protocol, self.reviewer))

        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()

        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.APPROVAL, 2))
        self.assertTrue(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.RESENT_FOR_APPROVAL, revision=2
            ).exists()
        )
        second = ProtocolApproval.objects.filter(protocol=protocol, revision=2)
        # Everyone signs again — including the executor, who approved revision 1.
        self.assertEqual(
            sorted(second.values_list('user_id', flat=True)),
            sorted([self.reviewer.pk, self.observer.pk, self.executor.pk]),
        )
        self.assertEqual(
            set(second.values_list('status', flat=True)), {ProtocolApproval.Status.PENDING}
        )
        self.assertEqual(ProtocolApproval.objects.filter(protocol=protocol).count(), 6)
        # Revision 1's approval never counts towards revision 2.
        self.assertNotEqual(
            second.get(user=self.executor).task_id, first[self.executor.pk].task_id
        )

    def test_the_last_approval_archives_the_protocol_and_creates_one_task_per_action(self):
        protocol = self._build_protocol(
            approvers=[self.reviewer], action_assignees=[self.executor, self.author]
        )
        second_action = ProtocolAction.objects.create(
            protocol=protocol,
            task_text='Обновить инструкцию',
            department=self.otk,
            due_date=timezone.localdate() + timedelta(days=14),
            display_order=1,
        )
        ProtocolActionAssignee.objects.create(action=second_action, user=self.reviewer)
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()

        approve_protocol(protocol, self.reviewer)
        protocol.refresh_from_db()
        # Still one pending approval, so nothing is archived and no task exists.
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)
        self.assertFalse(Task.objects.filter(source_type=Task.SourceType.PROTOCOL_ACTION).exists())

        approve_protocol(protocol, self.executor)
        protocol.refresh_from_db()

        self.assertEqual(protocol.status, Protocol.Status.ARCHIVED)
        tasks = {t.protocol_action_id: t for t in Task.objects.filter(protocol=protocol,
                                                                     source_type=Task.SourceType.PROTOCOL_ACTION)}
        action = protocol.actions.get(display_order=0)
        self.assertEqual(set(tasks), {action.pk, second_action.pk})
        self.assertEqual(tasks[action.pk].task_text, 'Проверить оснастку')
        self.assertEqual(tasks[action.pk].department, self.to)
        self.assertEqual(tasks[action.pk].due_date, action.due_date)
        self.assertEqual(tasks[action.pk].created_by, self.author)
        self.assertEqual(tasks[action.pk].status.code, 'IN_PROGRESS')
        # Assignees are copied verbatim, author included: the author is excluded
        # from *approving*, never from *doing*.
        self.assertEqual(
            sorted(a.user_id for a in tasks[action.pk].assignees.all()),
            sorted([self.executor.pk, self.author.pk]),
        )
        # A repeated or stale approval changes nothing and creates no duplicate.
        with self.assertRaises(ProtocolWorkflowError):
            approve_protocol(protocol, self.reviewer)
        self.assertEqual(
            Task.objects.filter(protocol=protocol, source_type=Task.SourceType.PROTOCOL_ACTION).count(),
            2,
        )
        self.assertEqual(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.ARCHIVED
            ).count(),
            1,
        )
        # The shared-task semantics are the ordinary ones: one assignee finishes it.
        completed = complete_task(tasks[action.pk], self.executor, 'Оснастка проверена.')
        self.assertEqual(completed.status.code, 'COMPLETED')


class ProtocolActionSplitTests(TestCase):
    """«Разбить задачу для участников»: execution only, one decision either way.

    The shared mode is already covered end to end by
    `ProtocolApprovalWorkflowTests`; what is exercised here is only what the
    flag adds — where it may be stored, how many tasks archiving then creates,
    and that those tasks are genuinely independent of one another.
    """

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.otk = Department.objects.create(name='ОТК', code='OTK')
        cls.to = Department.objects.create(name='ТО', code='TO')
        cls.author = _employee('split_author', cls.otk, 'Иван', 'Петров')
        cls.first = _employee('split_first', cls.to, 'Пётр', 'Сидоров')
        cls.second = _employee('split_second', cls.to, 'Анна', 'Кузнецова')
        cls.third = _employee('split_third', cls.to, 'Ольга', 'Смирнова')

    def _archived_protocol(self, *, split, assignees, requires_attachment=False):
        """A protocol whose single decision names `assignees`, taken to ARCHIVED.

        Every assignee has to approve — that is the ordinary formula, and it is
        deliberately not adjusted for the split flag.
        """
        protocol = create_protocol(self.quality, self.author)
        ProtocolAgendaItem.objects.create(
            protocol=protocol, text='О качестве партии', display_order=0
        )
        add_speech(
            protocol,
            protocol.participants.get(user=self.author),
            'Доложил о результатах контроля.',
        )
        action = ProtocolAction.objects.create(
            protocol=protocol,
            task_text='Изучение интерфейса',
            department=self.to,
            due_date=timezone.localdate() + timedelta(days=7),
            split_for_assignees=split,
            requires_attachment=requires_attachment,
            display_order=0,
        )
        for user in assignees:
            ProtocolActionAssignee.objects.create(action=action, user=user)
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        for user in assignees:
            approve_protocol(protocol, user)
            protocol.refresh_from_db()
        return protocol, action

    def test_the_draft_stores_the_flag_only_where_splitting_can_mean_anything(self):
        """Two assignees keep the choice; one normalizes it away on the server.

        The browser disables the checkbox below two исполнителя, but that is
        presentation: this posts it ticked in both cases, the way a stale page
        or a hand-made request would, and `_apply_actions()` is what decides.
        """
        protocol = create_protocol(self.quality, self.author)
        self.client.force_login(self.author)
        due = (timezone.localdate() + timedelta(days=7)).isoformat()

        response = self.client.post(
            reverse('protocols:save_draft', args=[protocol.pk]),
            {
                'participants-TOTAL_FORMS': '0',
                'agenda-TOTAL_FORMS': '1',
                'agenda-0-text': 'О качестве партии',
                'speeches-TOTAL_FORMS': '1',
                'speeches-0-speaker': str(self.author.pk),
                'speeches-0-text': 'Доложил о результатах контроля.',
                'actions-TOTAL_FORMS': '2',
                'actions-0-text': 'Изучение интерфейса',
                'actions-0-due_date': due,
                'actions-0-assignee_departments': [str(self.to.pk), str(self.to.pk)],
                'actions-0-assignees': [str(self.first.pk), str(self.second.pk)],
                'actions-0-split_for_assignees': 'on',
                'actions-1-text': 'Обновить инструкцию',
                'actions-1-due_date': due,
                'actions-1-assignee_departments': str(self.to.pk),
                'actions-1-assignees': str(self.third.pk),
                'actions-1-split_for_assignees': 'on',
            },
        )

        self.assertRedirects(response, reverse('protocols:detail', args=[protocol.pk]))
        split_action, single_action = list(protocol.actions.order_by('display_order'))
        self.assertTrue(split_action.split_for_assignees)
        # One assignee: splitting the work between them alone means nothing.
        self.assertFalse(single_action.split_for_assignees)
        # Reopening the editor offers the stored answer back.
        rows = self.client.get(
            reverse('protocols:detail', args=[protocol.pk])
        ).context['form'].action_rows
        self.assertEqual([row['split_for_assignees'] for row in rows], [True, False])

    def test_a_required_attachment_is_stored_on_the_draft_and_copied_to_every_task(self):
        """«Обязательно вложение»: stored on the decision, snapshotted per task.

        Unlike splitting it is never normalized away, so both execution modes
        carry it: one shared task, or one per assignee each enforcing it on its
        own. The editor offers the stored answer back, which is what a protocol
        returned for revision depends on.
        """
        protocol = create_protocol(self.quality, self.author)
        self.client.force_login(self.author)
        due = (timezone.localdate() + timedelta(days=7)).isoformat()

        response = self.client.post(
            reverse('protocols:save_draft', args=[protocol.pk]),
            {
                'participants-TOTAL_FORMS': '0',
                'agenda-TOTAL_FORMS': '1',
                'agenda-0-text': 'О качестве партии',
                'speeches-TOTAL_FORMS': '1',
                'speeches-0-speaker': str(self.author.pk),
                'speeches-0-text': 'Доложил о результатах контроля.',
                'actions-TOTAL_FORMS': '2',
                'actions-0-text': 'Изучение интерфейса',
                'actions-0-due_date': due,
                'actions-0-assignee_departments': str(self.to.pk),
                'actions-0-assignees': str(self.first.pk),
                'actions-0-requires_attachment': 'on',
                'actions-1-text': 'Обновить инструкцию',
                'actions-1-due_date': due,
                'actions-1-assignee_departments': str(self.to.pk),
                'actions-1-assignees': str(self.second.pk),
            },
        )

        self.assertRedirects(response, reverse('protocols:detail', args=[protocol.pk]))
        required, plain = list(protocol.actions.order_by('display_order'))
        # One assignee keeps the requirement: unlike splitting, it means the
        # same however many people the decision names.
        self.assertTrue(required.requires_attachment)
        self.assertFalse(plain.requires_attachment)
        # Reopening the editor offers the stored answer back, which is what a
        # return for revision preserves.
        rows = self.client.get(
            reverse('protocols:detail', args=[protocol.pk])
        ).context['form'].action_rows
        self.assertEqual([row['requires_attachment'] for row in rows], [True, False])

        # Shared: one task, carrying the decision's requirement.
        shared, _action = self._archived_protocol(
            split=False, assignees=[self.first, self.second], requires_attachment=True
        )
        shared_task = Task.objects.get(
            protocol=shared, source_type=Task.SourceType.PROTOCOL_ACTION
        )
        self.assertTrue(shared_task.requires_attachment)

        # Split: every independent task carries it, and each enforces it alone.
        split, _action = self._archived_protocol(
            split=True, assignees=[self.first, self.second], requires_attachment=True
        )
        split_tasks = list(
            Task.objects.filter(protocol=split, source_type=Task.SourceType.PROTOCOL_ACTION)
        )
        self.assertEqual(len(split_tasks), 2)
        self.assertTrue(all(task.requires_attachment for task in split_tasks))

        # The approval tasks the same protocols opened never carry it.
        self.assertFalse(
            Task.objects.filter(
                source_type=Task.SourceType.PROTOCOL_APPROVAL, requires_attachment=True
            ).exists()
        )

    def test_archiving_splits_a_marked_decision_into_one_task_per_assignee(self):
        assignees = [self.first, self.second, self.third]
        protocol, action = self._archived_protocol(split=True, assignees=assignees)

        self.assertEqual(protocol.status, Protocol.Status.ARCHIVED)
        tasks = list(
            Task.objects.filter(protocol=protocol, source_type=Task.SourceType.PROTOCOL_ACTION)
            .order_by('individual_assignee_id')
            .prefetch_related('assignees')
        )
        self.assertEqual(len(tasks), 3)
        for task, user in zip(tasks, sorted(assignees, key=lambda item: item.pk)):
            # Same decision, same wording, department, deadline and author…
            self.assertEqual(task.protocol_action_id, action.pk)
            self.assertEqual(task.task_text, 'Изучение интерфейса')
            self.assertEqual(task.department, self.to)
            self.assertEqual(task.due_date, action.due_date)
            self.assertEqual(task.created_by, self.author)
            # …and exactly one assignee, who is the person the task names.
            self.assertEqual(task.individual_assignee_id, user.pk)
            self.assertEqual([a.user_id for a in task.assignees.all()], [user.pk])

        # One decision, one row, one history event — only execution was split.
        self.assertEqual(protocol.actions.count(), 1)
        self.assertEqual(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.TASKS_CREATED
            ).count(),
            1,
        )
        # And the approval formula is untouched: three assignees, three
        # signatures, whether or not their execution was split.
        self.assertEqual(
            ProtocolApproval.objects.filter(protocol=protocol, revision=protocol.revision).count(),
            3,
        )

        # The shared mode is unchanged beside it: one task, every assignee on it.
        shared_protocol, _ = self._archived_protocol(split=False, assignees=assignees)
        shared_task = Task.objects.get(
            protocol=shared_protocol, source_type=Task.SourceType.PROTOCOL_ACTION
        )
        self.assertIsNone(shared_task.individual_assignee_id)
        self.assertEqual(
            sorted(a.user_id for a in shared_task.assignees.all()),
            sorted(user.pk for user in assignees),
        )

    def test_a_split_task_finishes_alone_and_can_never_be_created_twice(self):
        assignees = [self.first, self.second, self.third]
        protocol, action = self._archived_protocol(split=True, assignees=assignees)
        tasks = {
            task.individual_assignee_id: task
            for task in Task.objects.filter(
                protocol=protocol, source_type=Task.SourceType.PROTOCOL_ACTION
            )
        }

        completed = complete_task(tasks[self.first.pk], self.first, 'Интерфейс изучен.')

        self.assertEqual(completed.status.code, 'COMPLETED')
        for user in (self.second, self.third):
            tasks[user.pk].refresh_from_db()
            # Nobody else's work was closed by someone else finishing theirs.
            self.assertEqual(tasks[user.pk].status.code, 'IN_PROGRESS')
            self.assertIsNone(tasks[user.pk].completed_by_id)

        # A repeated or faulty generation cannot hand the same person a second
        # copy, nor add a shared task beside the split ones. The database says
        # so, not a service check a concurrent caller could race past.
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_protocol_action_task(
                protocol,
                action,
                [self.first.pk],
                created_by=self.author,
                individual_assignee_id=self.first.pk,
            )
        shared_protocol, shared_action = self._archived_protocol(
            split=False, assignees=assignees
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_protocol_action_task(
                shared_protocol,
                shared_action,
                [user.pk for user in assignees],
                created_by=self.author,
            )


class ProtocolApprovalUiTests(TestCase):
    """The approval workflow as it is actually reached: through the pages.

    Two scenarios, both end to end. The services already have their own tests
    above; what is checked here is the part only the UI stage can get wrong —
    that «Отправить на согласование» submits *the form's current content*, that
    the protocol then really is read-only with its round exposed, and that an
    approval task is a queue entry which leads to the protocol and is never
    completable as an ordinary task.
    """

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.otk = Department.objects.create(name='ОТК', code='OTK')
        cls.to = Department.objects.create(name='ТО', code='TO')
        cls.author = _employee('ui_author', cls.otk, 'Иван', 'Петров')
        cls.reviewer = _employee('ui_reviewer', cls.to, 'Пётр', 'Сидоров')
        cls.executor = _employee('ui_executor', cls.to, 'Анна', 'Кузнецова')

    def _payload(self, **overrides):
        """Exactly what the editor form posts, for either endpoint."""
        payload = {
            'participants-TOTAL_FORMS': '1',
            'participants-0-department': str(self.to.pk),
            'participants-0-user': str(self.reviewer.pk),
            'participants-0-requires_approval': 'on',
            'agenda-TOTAL_FORMS': '1',
            'agenda-0-text': 'О качестве партии',
            'speeches-TOTAL_FORMS': '1',
            'speeches-0-speaker': str(self.author.pk),
            'speeches-0-text': 'Доложил о результатах контроля.',
            'actions-TOTAL_FORMS': '1',
            'actions-0-text': 'Проверить оснастку',
            'actions-0-due_date': (timezone.localdate() + timedelta(days=7)).isoformat(),
            'actions-0-assignee_departments': str(self.to.pk),
            'actions-0-assignees': str(self.executor.pk),
        }
        payload.update(overrides)
        return payload

    def test_author_submits_the_form_content_and_the_protocol_becomes_read_only(self):
        protocol = create_protocol(self.quality, self.author)
        self.client.force_login(self.author)
        send_url = reverse('protocols:send_for_approval', args=[protocol.pk])

        # An invalid form submits nothing at all: no draft, no approval round.
        refused = self.client.post(send_url, self._payload(**{'agenda-0-text': ''}))
        self.assertEqual(refused.status_code, 400)
        protocol.refresh_from_db()
        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.DRAFT, 0))
        self.assertFalse(ProtocolApproval.objects.filter(protocol=protocol).exists())

        # A GET never mutates: the endpoint is POST-only.
        self.client.get(send_url)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.DRAFT)

        # The real submission: the content typed *now* is what is saved and
        # what goes for approval — the page was never saved before this.
        response = self.client.post(
            send_url, self._payload(**{'agenda-0-text': 'Итоговая повестка'})
        )
        self.assertRedirects(response, reverse('protocols:detail', args=[protocol.pk]))
        protocol.refresh_from_db()
        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.APPROVAL, 1))
        self.assertEqual(protocol.agenda_items.get().text, 'Итоговая повестка')

        page = self.client.get(reverse('protocols:detail', args=[protocol.pk]))
        # `APPROVAL` is read-only even for the author, and the round is shown.
        self.assertFalse(page.context['can_edit'])
        self.assertFalse(page.context['can_send_for_approval'])
        self.assertNotContains(page, 'Сохранить черновик')
        self.assertContains(page, 'Согласовано: 0 из 2')
        self.assertContains(page, 'Редакция 1')
        # The snapshot says who signs and why, from the stored flags.
        self.assertContains(page, 'Пётр Сидоров')
        self.assertContains(page, 'Участник, отмеченный «Требует согласования»')
        self.assertContains(page, 'Исполнитель задачи протокола')

    def test_an_approval_task_leads_to_the_protocol_and_decides_only_there(self):
        protocol = create_protocol(self.quality, self.author)
        self.client.force_login(self.author)
        self.client.post(
            reverse('protocols:send_for_approval', args=[protocol.pk]), self._payload()
        )
        approval = ProtocolApproval.objects.get(protocol=protocol, user=self.reviewer)

        # The queue entry exists, but it is only that: opening it lands on the
        # protocol, and the ordinary completion endpoint refuses it outright.
        self.client.force_login(self.reviewer)
        self.assertRedirects(
            self.client.get(reverse('tasks:detail', args=[approval.task_id])),
            reverse('protocols:detail', args=[protocol.pk]),
        )
        refused = self.client.post(
            reverse('tasks:complete', args=[approval.task_id]),
            {'execution_comment': 'Согласовано.'},
        )
        self.assertEqual(refused.status_code, 400)
        approval.refresh_from_db()
        self.assertEqual(approval.status, ProtocolApproval.Status.PENDING)

        # A return needs a comment, and refusing it keeps the page usable.
        return_url = reverse('protocols:return_for_revision', args=[protocol.pk])
        empty = self.client.post(return_url, {'comment': '   '})
        self.assertEqual(empty.status_code, 400)
        self.assertContains(empty, 'Укажите причину возврата на доработку.', status_code=400)

        page = self.client.get(reverse('protocols:detail', args=[protocol.pk]))
        self.assertTrue(page.context['can_decide_approval'])
        self.assertContains(page, 'Вернуть на доработку')
        self.assertContains(page, 'Согласовать')

        self.client.post(return_url, {'comment': 'Уточните сроки.'})
        protocol.refresh_from_db()
        approval.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.REVISION)
        self.assertEqual(approval.status, ProtocolApproval.Status.RETURNED)
        # The executor's round was cancelled — never presented as approved.
        cancelled = ProtocolApproval.objects.get(protocol=protocol, user=self.executor)
        self.assertEqual(cancelled.status, ProtocolApproval.Status.CANCELLED)

        # Resubmission opens revision 2; revision 1 stays visible as history
        # and no longer counts towards the live round.
        self.client.force_login(self.author)
        self.client.post(
            reverse('protocols:send_for_approval', args=[protocol.pk]), self._payload()
        )
        protocol.refresh_from_db()
        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.APPROVAL, 2))
        page = self.client.get(reverse('protocols:detail', args=[protocol.pk]))
        self.assertContains(page, 'Согласовано: 0 из 2')
        self.assertContains(page, 'Редакция 2')
        history = self.client.get(
            reverse('protocols:detail', args=[protocol.pk]), {'tab': 'history'}
        )
        self.assertEqual(
            [group['revision'] for group in history.context['approval_revisions']], [2, 1]
        )
        self.assertContains(history, 'Уточните сроки.')

    def test_cancelling_the_send_modal_leaves_the_editor_posting_to_save(self):
        """The editor form must never keep the submission endpoint after «Отмена».

        A source-level check rather than a browser one: the project has no
        JavaScript test runner and this stage is not the place to add one. What
        it pins down is the shape of the fix — the trigger's action override is
        applied only when the user confirms, and closing the dialog puts the
        form's own action back — so a regression to «rewrite the action when the
        modal opens» is caught here instead of by a user losing a draft.
        """
        protocol = create_protocol(self.quality, self.author)
        self.client.force_login(self.author)
        page = self.client.get(reverse('protocols:detail', args=[protocol.pk]))

        # The editor posts to «сохранить» as rendered; the submission endpoint
        # only ever lives on the trigger button.
        self.assertContains(page, f'action="{reverse("protocols:save_draft", args=[protocol.pk])}"')
        self.assertContains(
            page,
            f'data-confirm-form-action="{reverse("protocols:send_for_approval", args=[protocol.pk])}"',
        )

        script = (settings.BASE_DIR / 'static' / 'js' / 'confirm_modal.js').read_text(
            encoding='utf-8'
        )
        open_block = script.split("const open = (button)")[1].split("document.addEventListener")[0]
        self.assertNotIn('confirmFormAction', open_block)
        submit_block = script.split("form.addEventListener('submit'")[1]
        self.assertIn('confirmFormAction', submit_block)
        self.assertIn('restoreTargetAction();', script.split("dialog.addEventListener('close'")[1])
