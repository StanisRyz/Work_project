"""Real-time for Protocols: events, the revision token and the fragments.

Three tests on purpose, one per half of the contract that can genuinely break:

* the workflow emits the right events, only after a commit, and only for state
  a user can actually observe;
* the `protocols` revision token moves for every kind of change, so a client
  that missed an event still recovers;
* the fragment endpoints render through the ordinary permission-checked views.

Everything else — numbering, who must sign, what a return resets — is already
covered by `protocols/tests.py` and is not restated here.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department
from protocols.models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolAction,
    ProtocolActionAssignee,
    ProtocolAgendaItem,
    ProtocolType,
)
from protocols.services import (
    add_participant,
    add_speech,
    approve_protocol,
    create_protocol,
    delete_draft_protocol,
    save_protocol_draft,
    send_protocol_for_approval,
)
from realtime.events import RESOURCE_PROTOCOL, RealtimeEventType
from realtime.sync import REVISION_PROTOCOLS, build_sync_state
from realtime.testing import capture_realtime_events

from .base import target_keys


def _employee(username, department):
    user = User.objects.create_user(username=username, password='demo12345')
    user.userprofile.department = department
    user.userprofile.save(update_fields=['department'])
    return user


class ProtocolRealtimeMixin:
    @classmethod
    def setUpProtocolData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.department = Department.objects.create(name='ТО', code='RT_PROTO_TO')
        cls.author = _employee('rt_protocol_author', cls.department)
        cls.reviewer = _employee('rt_protocol_reviewer', cls.department)
        cls.reader = _employee('rt_protocol_reader', cls.department)
        cls.due_date = timezone.localdate() + timedelta(days=7)

    def make_protocol(self, *, approvers=(), with_action=False):
        """A structurally complete protocol, as the editor would write one."""
        protocol = create_protocol(self.quality, self.author)
        for order, user in enumerate(approvers, start=1):
            add_participant(
                protocol,
                user,
                department=self.department,
                requires_approval=True,
                display_order=order,
            )
        ProtocolAgendaItem.objects.create(protocol=protocol, text='Вопрос', display_order=0)
        add_speech(protocol, protocol.participants.get(user=self.author), 'Доложил.')
        if with_action:
            action = ProtocolAction.objects.create(
                protocol=protocol,
                task_text='Проверить оснастку',
                department=self.department,
                due_date=self.due_date,
                display_order=0,
            )
            ProtocolActionAssignee.objects.create(action=action, user=self.reviewer)
        return protocol

    def draft_payload(self, agenda='Вопрос'):
        """The document `make_protocol(approvers=[reviewer], with_action=True)`
        already holds, so re-submitting it changes nothing unless `agenda` does."""
        return {
            'participants': [
                {
                    'user': self.reviewer,
                    'department': self.department,
                    'requires_approval': True,
                }
            ],
            'agenda': [agenda],
            'speeches': [{'speaker_user': self.author, 'text': 'Доложил.'}],
            'actions': [
                {
                    'text': 'Проверить оснастку',
                    'department': self.department,
                    'due_date': self.due_date,
                    'assignees': [self.reviewer],
                }
            ],
        }


class ProtocolEventLifecycleTests(ProtocolRealtimeMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpProtocolData()

    def test_the_workflow_emits_committed_observable_state_and_nothing_else(self):
        with capture_realtime_events() as publisher:
            # -- creation ------------------------------------------------
            with self.captureOnCommitCallbacks(execute=True):
                protocol = self.make_protocol(approvers=[self.reviewer], with_action=True)
                self.assertEqual(publisher.events, [], 'опубликовано до commit')

            created = publisher.events[-1]
            self.assertEqual(created.event_type, RealtimeEventType.PROTOCOL_CREATED)
            self.assertEqual(created.resource_type, RESOURCE_PROTOCOL)
            self.assertEqual(created.resource_id, protocol.pk)
            self.assertEqual(created.data['status'], Protocol.Status.DRAFT)
            # Every reader may open a protocol, so every active account is a
            # recipient — and only `user:<id>` targets are ever produced.
            self.assertEqual(
                target_keys(publisher.published[-1][1]),
                sorted(
                    f'user:{pk}'
                    for pk in (self.author.pk, self.reviewer.pk, self.reader.pk)
                ),
            )

            # -- a submission that stored nothing new stays silent ---------
            publisher.clear()
            with self.captureOnCommitCallbacks(execute=True):
                save_protocol_draft(protocol, self.author, self.draft_payload())
            self.assertEqual(publisher.events, [])

            # -- an edit that stored something -----------------------------
            with self.captureOnCommitCallbacks(execute=True):
                save_protocol_draft(
                    protocol, self.author, self.draft_payload('Изменённый вопрос')
                )
            self.assertEqual(
                [event.event_type for event in publisher.events],
                [RealtimeEventType.PROTOCOL_UPDATED],
            )

            # -- submission ------------------------------------------------
            publisher.clear()
            with self.captureOnCommitCallbacks(execute=True):
                send_protocol_for_approval(protocol, self.author)
            sent = publisher.events[-1]
            self.assertEqual(sent.event_type, RealtimeEventType.PROTOCOL_STATUS_CHANGED)
            self.assertEqual(
                (sent.data['from_status'], sent.data['to_status'], sent.data['revision']),
                (Protocol.Status.DRAFT, Protocol.Status.APPROVAL, 1),
            )

            # -- the last approval: one decision *and* one archive ---------
            publisher.clear()
            with self.captureOnCommitCallbacks(execute=True):
                approve_protocol(protocol, self.reviewer)
            protocol_events = [
                event for event in publisher.events if event.resource_type == RESOURCE_PROTOCOL
            ]
            self.assertEqual(
                [event.event_type for event in protocol_events],
                [
                    RealtimeEventType.PROTOCOL_APPROVAL_CHANGED,
                    RealtimeEventType.PROTOCOL_STATUS_CHANGED,
                ],
            )
            self.assertEqual(
                protocol_events[1].data['to_status'], Protocol.Status.ARCHIVED
            )

            # -- a submission that requires nobody -------------------------
            #
            # The same transaction archives it, so `APPROVAL` never existed for
            # any reader: one transition, not two.
            publisher.clear()
            solo = self.make_protocol()
            with self.captureOnCommitCallbacks(execute=True):
                send_protocol_for_approval(solo, self.author)
            transitions = [
                event
                for event in publisher.events
                if event.event_type == RealtimeEventType.PROTOCOL_STATUS_CHANGED
            ]
            self.assertEqual(len(transitions), 1)
            self.assertEqual(
                (transitions[0].data['from_status'], transitions[0].data['to_status']),
                (Protocol.Status.DRAFT, Protocol.Status.ARCHIVED),
            )

            # -- a rolled-back deletion publishes nothing -------------------
            publisher.clear()
            draft = self.make_protocol()
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    delete_draft_protocol(draft, self.author)
                    transaction.set_rollback(True)
            self.assertEqual(
                [event.event_type for event in publisher.events], [],
                'откат опубликовал событие',
            )
            self.assertTrue(Protocol.objects.filter(pk=draft.pk).exists())

            with self.captureOnCommitCallbacks(execute=True):
                delete_draft_protocol(draft, self.author)
            deleted = publisher.events[-1]
            self.assertEqual(deleted.event_type, RealtimeEventType.PROTOCOL_DELETED)
            self.assertEqual(deleted.resource_id, draft.pk)


class ProtocolRevisionTokenTests(ProtocolRealtimeMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpProtocolData()

    def token(self):
        return build_sync_state(self.reader)['revisions'][REVISION_PROTOCOLS]

    def test_the_token_moves_for_every_kind_of_change(self):
        empty = self.token()

        protocol = self.make_protocol(approvers=[self.reviewer], with_action=True)
        after_create = self.token()
        self.assertNotEqual(empty, after_create)

        save_protocol_draft(protocol, self.author, self.draft_payload('Изменённый вопрос'))
        after_edit = self.token()
        self.assertNotEqual(after_create, after_edit)

        send_protocol_for_approval(protocol, self.author)
        after_send = self.token()
        self.assertNotEqual(after_edit, after_send)

        # The decisive case: approving one position leaves the protocol in
        # `APPROVAL` and touches nothing on the protocol row itself, so only
        # the approval aggregate can tell this apart from «ничего не менялось».
        second = self.make_protocol(approvers=[self.reviewer, self.reader])
        send_protocol_for_approval(second, self.author)
        before_decision = self.token()
        approve_protocol(second, self.reviewer)
        second.refresh_from_db()
        self.assertEqual(second.status, Protocol.Status.APPROVAL)
        self.assertNotEqual(before_decision, self.token())

        # A deletion releases a number and removes a row: the registry moved.
        draft = self.make_protocol()
        before_delete = self.token()
        delete_draft_protocol(draft, self.author)
        self.assertNotEqual(before_delete, self.token())


class ProtocolFragmentEndpointTests(ProtocolRealtimeMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpProtocolData()

    def test_the_fragments_render_the_current_state_for_the_session_user(self):
        protocol = self.make_protocol(approvers=[self.reviewer])
        self.client.force_login(self.reader)

        registry = self.client.get(reverse('protocols:list_fragment'), {'tab': 'work'})
        self.assertEqual(registry.status_code, 200)
        payload = registry.json()
        self.assertEqual(payload['tab'], 'work')
        self.assertIn(f'№{protocol.number}', payload['results_html'])
        self.assertEqual(
            registry['Cache-Control'], 'no-cache, no-store, must-revalidate, private'
        )
        # The archive tab is the same server-side queryset as the page, so a
        # draft must not appear in it.
        archive = self.client.get(reverse('protocols:list_fragment'), {'tab': 'archive'})
        self.assertNotIn(f'№{protocol.number}', archive.json()['results_html'])

        # A reader who is not the author never gets the editor from the content
        # fragment, whatever the protocol's status.
        content = self.client.get(
            reverse('protocols:content_fragment', args=[protocol.pk])
        )
        self.assertEqual(content.status_code, 200)
        self.assertFalse(content.json()['can_edit'])
        self.assertNotIn('data-protocol-editor', content.json()['html'])

        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()

        heading = self.client.get(
            reverse('protocols:heading_fragment', args=[protocol.pk])
        ).json()
        self.assertEqual(heading['status'], Protocol.Status.APPROVAL)
        self.assertEqual(heading['revision'], 1)
        approval = self.client.get(
            reverse('protocols:approval_fragment', args=[protocol.pk])
        ).json()
        self.assertIn('protocol-approval-panel', approval['html'])
        history = self.client.get(
            reverse('protocols:history_fragment', args=[protocol.pk])
        ).json()
        self.assertIn('согласование', history['html'].lower())

        # A protocol that no longer exists is a plain 404 — never a hint.
        missing = self.client.get(
            reverse('protocols:heading_fragment', args=[protocol.pk + 10_000])
        )
        self.assertEqual(missing.status_code, 404)

        # No session, no fragment.
        self.client.logout()
        self.assertIn(
            self.client.get(reverse('protocols:list_fragment')).status_code, (401, 403)
        )
