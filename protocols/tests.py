"""The three behaviours the protocol foundation cannot get wrong."""

from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Department
from protocols.models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolHistoryEvent,
    ProtocolParticipant,
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
