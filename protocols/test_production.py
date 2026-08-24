"""The three production scenarios the finished Protocol module must not get wrong.

Deliberately three end-to-end tests rather than a state matrix: the per-rule
refusals are already covered by `protocols/tests.py`, and what a released
module has to guarantee is that the whole cycle, the tasks it produces and the
official document all hold together.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from tasks.models import Task

from .models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolAction,
    ProtocolActionAssignee,
    ProtocolAgendaItem,
    ProtocolApproval,
    ProtocolHistoryEvent,
    ProtocolType,
)
from .permissions import can_edit_protocol
from .selectors import build_protocol_document
from .services import (
    ProtocolWorkflowError,
    add_participant,
    add_speech,
    approve_protocol,
    create_protocol,
    delete_draft_protocol,
    return_protocol_for_revision,
    save_protocol_draft,
    send_protocol_for_approval,
)


def _employee(username, department, first_name, last_name):
    user = User.objects.create_user(
        username=username, password='demo12345', first_name=first_name, last_name=last_name
    )
    profile = user.userprofile
    profile.department = department
    profile.role = UserProfile.Role.OTK
    profile.position = 'Специалист'
    profile.save(update_fields=['department', 'role', 'position'])
    return user


class ProtocolProductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.to = Department.objects.create(name='ТО', code='PROD_TO')
        cls.author = _employee('prod_author', cls.to, 'Иван', 'Петров')
        cls.reviewer = _employee('prod_reviewer', cls.to, 'Пётр', 'Сидоров')
        cls.executor = _employee('prod_executor', cls.to, 'Анна', 'Кузнецова')
        cls.reader = _employee('prod_reader', cls.to, 'Ольга', 'Смирнова')

    def _build(self, *, approvers=(), assignees=()):
        """A structurally complete protocol, written the way the editor writes one."""
        protocol = create_protocol(self.quality, self.author)
        for order, user in enumerate(approvers, start=1):
            add_participant(
                protocol, user, department=self.to, requires_approval=True, display_order=order
            )
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
            task_text='Проверить оснастку',
            department=self.to,
            due_date=timezone.localdate() + timedelta(days=7),
            display_order=0,
        )
        for user in assignees:
            ProtocolActionAssignee.objects.create(action=action, user=user)
        return protocol

    def test_the_full_lifecycle_draft_approval_revision_archive(self):
        protocol = self._build(approvers=[self.reviewer], assignees=[self.executor])
        self.assertEqual(protocol.status, Protocol.Status.DRAFT)

        # -- draft → approval, revision 1
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.APPROVAL, 1))
        # Both people were required; the author is never one of them.
        self.assertEqual(
            set(
                ProtocolApproval.objects.filter(protocol=protocol, revision=1).values_list(
                    'user_id', flat=True
                )
            ),
            {self.reviewer.pk, self.executor.pk},
        )
        with self.assertRaises(ProtocolWorkflowError):
            # An author does not sign their own document, even as its owner.
            approve_protocol(protocol, self.author)
        # A document being signed is read-only for everyone, author included.
        self.assertFalse(can_edit_protocol(protocol, self.author))

        # -- approval → revision
        with self.assertRaises(ProtocolWorkflowError):
            # A return without a reason tells the author nothing.
            return_protocol_for_revision(protocol, self.reviewer, '   ')
        return_protocol_for_revision(protocol, self.reviewer, 'Уточните формулировку решения.')
        protocol.refresh_from_db()
        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.REVISION, 1))
        # The round is closed: the returner is RETURNED, the rest CANCELLED.
        statuses = dict(
            ProtocolApproval.objects.filter(protocol=protocol, revision=1).values_list(
                'user_id', 'status'
            )
        )
        self.assertEqual(statuses[self.reviewer.pk], ProtocolApproval.Status.RETURNED)
        self.assertEqual(statuses[self.executor.pk], ProtocolApproval.Status.CANCELLED)
        # Returned means editable again — that is the whole point of returning.
        self.assertTrue(can_edit_protocol(protocol, self.author))

        # -- revision → approval again, as a full fresh round
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        self.assertEqual((protocol.status, protocol.revision), (Protocol.Status.APPROVAL, 2))
        self.assertEqual(
            ProtocolApproval.objects.filter(
                protocol=protocol, revision=2, status=ProtocolApproval.Status.PENDING
            ).count(),
            2,
            'новая редакция открывает круг согласования заново',
        )

        # -- approval → archive, only once the last approver has signed
        approve_protocol(protocol, self.reviewer)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)
        approve_protocol(protocol, self.executor)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.ARCHIVED)

        # -- an archived protocol is frozen
        self.assertFalse(can_edit_protocol(protocol, self.author))
        with self.assertRaises(ProtocolWorkflowError):
            save_protocol_draft(
                protocol,
                self.author,
                {'participants': [], 'agenda': ['Другое'], 'speeches': [], 'actions': []},
            )
        with self.assertRaises(ProtocolWorkflowError):
            delete_draft_protocol(protocol, self.author)

        # The history is the audit trail of exactly that cycle.
        self.assertEqual(
            list(protocol.history_events.order_by('pk').values_list('event_type', flat=True)),
            [
                ProtocolHistoryEvent.EventType.CREATED,
                ProtocolHistoryEvent.EventType.SENT_FOR_APPROVAL,
                ProtocolHistoryEvent.EventType.RETURNED_FOR_REVISION,
                ProtocolHistoryEvent.EventType.RESENT_FOR_APPROVAL,
                ProtocolHistoryEvent.EventType.APPROVED_BY_USER,
                ProtocolHistoryEvent.EventType.APPROVED_BY_USER,
                ProtocolHistoryEvent.EventType.ARCHIVED,
                ProtocolHistoryEvent.EventType.TASKS_CREATED,
            ],
        )

    def test_the_final_approval_creates_the_protocol_tasks_and_nothing_else(self):
        protocol = self._build(approvers=[], assignees=[self.executor])
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        # Being named in a decision makes the executor an approver, so the
        # submission does not archive the protocol by itself.
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)

        approval_tasks = Task.objects.filter(
            protocol=protocol, source_type=Task.SourceType.PROTOCOL_APPROVAL
        )
        self.assertEqual(approval_tasks.count(), 1)
        # A queue entry, not an ordinary assignment: it carries no decision.
        self.assertIsNone(approval_tasks.get().protocol_action_id)

        approve_protocol(protocol, self.executor)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.ARCHIVED)

        action = protocol.actions.get()
        task = Task.objects.get(protocol=protocol, source_type=Task.SourceType.PROTOCOL_ACTION)
        self.assertEqual(task.protocol_action_id, action.pk)
        self.assertEqual(task.task_text, action.task_text)
        self.assertEqual(task.department_id, self.to.pk)
        self.assertEqual(task.due_date, action.due_date)
        self.assertEqual(
            list(task.assignees.values_list('user_id', flat=True)), [self.executor.pk]
        )
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        # The approval queue entry is closed by the decision, not left open.
        self.assertEqual(approval_tasks.get().status.code, 'COMPLETED')

        self.client.force_login(self.executor)
        # An approval task never reaches the ordinary task page: it redirects
        # to the protocol, which is where the decision is actually made.
        self.assertRedirects(
            self.client.get(reverse('tasks:detail', args=[approval_tasks.get().pk])),
            reverse('protocols:detail', args=[protocol.pk]),
        )
        # The real task links back to its protocol from its own page.
        self.assertContains(
            self.client.get(reverse('tasks:detail', args=[task.pk])),
            reverse('protocols:detail', args=[protocol.pk]),
        )

    def test_the_official_document_renders_for_every_reader_as_page_and_pdf(self):
        protocol = self._build(approvers=[self.reviewer], assignees=[self.executor])
        send_protocol_for_approval(protocol, self.author)
        approve_protocol(protocol, self.reviewer)
        approve_protocol(protocol, self.executor)
        protocol.refresh_from_db()

        print_url = reverse('protocols:print', args=[protocol.pk])
        pdf_url = reverse('protocols:pdf', args=[protocol.pk])

        # Anonymous access is refused on both, like every other protocol page.
        for url in (print_url, pdf_url):
            self.assertEqual(self.client.get(url).status_code, 302)

        # Any authenticated user may read the document — including someone who
        # took no part in the protocol at all.
        self.client.force_login(self.reader)
        page = self.client.get(print_url)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        for expected in (
            f'{self.quality.name} №{protocol.number}',      # header
            'Иван Петров',                                  # author
            'О качестве партии',                            # agenda
            'Доложил о результатах контроля.',              # listened
            'Проверить оснастку',                           # decision and task
            'Пётр Сидоров',                                 # approver
            'Согласовано',                                  # approval outcome
            'В архиве',                                     # final workflow state
        ):
            self.assertIn(expected, body)

        response = self.client.get(pdf_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment; filename=', response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))

        # The page and the PDF render one structure, so the document cannot
        # differ between them.
        document = build_protocol_document(protocol)
        self.assertEqual(document['status_label'], 'В архиве')
        self.assertEqual(len(document['tasks']), 1)
        self.assertEqual(
            [row['status_label'] for row in document['approvals']],
            ['Согласовано', 'Согласовано'],
        )
