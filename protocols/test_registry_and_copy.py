"""The protocol registry's «Тема»/«Согласование» columns and «На основе»."""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, UserProfile
from protocols.models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolAgendaItem,
    ProtocolApproval,
    ProtocolHistoryEvent,
    ProtocolSpeech,
    ProtocolType,
)
from protocols.selectors import build_protocol_list_state
from protocols.services import ProtocolWorkflowError, add_participant, create_protocol, create_protocol_based_on


def _user(username, first='', last=''):
    department, _ = Department.objects.get_or_create(code='REG_DEP', defaults={'name': 'Отдел'})
    user = User.objects.create_user(username, password='pw-12345', first_name=first, last_name=last)
    profile = user.userprofile
    profile.role = UserProfile.Role.TO
    profile.department = department
    profile.save(update_fields=['role', 'department'])
    return User.objects.get(pk=user.pk)


class ProtocolRegistryAndCopyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())
        cls.author = _user('reg_author', 'Анна', 'Автор')
        cls.peer = _user('reg_peer', 'Пётр', 'Петров')
        cls.gone = _user('reg_gone', 'Ушедший', 'Сотрудник')
        cls.type = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)

    def make_source(self):
        protocol = create_protocol(self.type, self.author)
        add_participant(protocol, self.peer, requires_approval=True, display_order=1)
        speaker = add_participant(protocol, self.gone, display_order=2)
        ProtocolAgendaItem.objects.create(protocol=protocol, text='Качество намотки', display_order=0)
        ProtocolAgendaItem.objects.create(protocol=protocol, text='Разное', display_order=1)
        ProtocolSpeech.objects.create(protocol=protocol, speaker=speaker, text='Доклад', display_order=0)
        return protocol

    def test_the_registry_names_the_topic_and_who_is_still_awaited(self):
        protocol = self.make_source()
        protocol.status = Protocol.Status.APPROVAL
        protocol.revision = 1
        protocol.save(update_fields=['status', 'revision'])
        ProtocolApproval.objects.create(
            protocol=protocol, revision=1, user=self.peer, display_name='Петров Пётр',
            status=ProtocolApproval.Status.APPROVED,
        )
        ProtocolApproval.objects.create(
            protocol=protocol, revision=1, user=self.gone, display_name='Сотрудник Ушедший',
        )
        # An earlier round never speaks for the current one.
        ProtocolApproval.objects.create(
            protocol=protocol, revision=0, user=self.author, display_name='Старый раунд',
        )

        row = build_protocol_list_state({'tab': 'work'})['rows'][0]

        self.assertEqual(row['subject'], 'Качество намотки')
        self.assertEqual(row['approval'], {'approved': 1, 'total': 2, 'pending_names': ['Сотрудник Ушедший']})
        self.client.force_login(self.author)
        page = self.client.get(reverse('protocols:list'))
        self.assertContains(page, '1 из 2')
        self.assertContains(page, 'ждём: Сотрудник Ушедший')
        self.assertContains(page, 'class="tab-count">1</span>')

    def test_a_draft_has_no_approval_progress(self):
        self.make_source()

        self.assertIsNone(build_protocol_list_state({})['rows'][0]['approval'])

    def test_based_on_copies_people_and_agenda_but_nothing_that_happened(self):
        source = self.make_source()
        self.gone.is_active = False
        self.gone.save(update_fields=['is_active'])

        copy = create_protocol_based_on(source, self.author)

        self.assertNotEqual(copy.pk, source.pk)
        self.assertEqual(copy.status, Protocol.Status.DRAFT)
        self.assertEqual(copy.protocol_type, self.type)
        participants = {(p.user_id, p.requires_approval) for p in copy.participants.all()}
        self.assertEqual(participants, {(self.author.pk, False), (self.peer.pk, True)})
        self.assertEqual(
            list(copy.agenda_items.values_list('text', flat=True)), ['Качество намотки', 'Разное']
        )
        self.assertFalse(copy.speeches.exists())
        created = copy.history_events.get(event_type=ProtocolHistoryEvent.EventType.CREATED)
        self.assertIn(f'на основе «{self.type.name} №{source.number}»', created.message)

    def test_a_colleague_may_base_their_own_draft_on_it(self):
        source = self.make_source()
        self.client.force_login(self.peer)

        response = self.client.post(reverse('protocols:create_based_on', args=[source.pk]))

        copy = Protocol.objects.exclude(pk=source.pk).get()
        self.assertRedirects(response, reverse('protocols:detail', args=[copy.pk]), fetch_redirect_response=False)
        self.assertEqual(copy.author, self.peer)
        self.assertEqual(self.client.get(reverse('protocols:create_based_on', args=[source.pk])).status_code, 302)
        self.assertEqual(Protocol.objects.count(), 2, 'a GET creates nothing')

    def test_an_inactive_type_is_refused(self):
        source = self.make_source()
        self.type.is_active = False
        self.type.save(update_fields=['is_active'])

        with self.assertRaises(ProtocolWorkflowError):
            create_protocol_based_on(source, self.author)
