"""The behaviours the protocol foundation and its draft editor cannot get wrong."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from protocols.models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolAction,
    ProtocolAgendaItem,
    ProtocolHistoryEvent,
    ProtocolParticipant,
    ProtocolSpeech,
    ProtocolType,
)
from protocols.services import create_protocol, delete_draft_protocol


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
            'actions-0-department': str(self.other_department.pk),
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


