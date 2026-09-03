"""«Вложения и комментарии» and «Связанные мероприятия».

Three behaviours that can genuinely go wrong: a file must be stored and handed
out only by the view that re-checks who may read the protocol; the mandatory
return reason must reach the feed exactly once and only if the return itself
committed; and the related-activities tab must list the real working tasks —
each split one of them separately — and never an approval queue entry.
"""

from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department
from protocols.models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolAction,
    ProtocolActionAssignee,
    ProtocolAgendaItem,
    ProtocolAttachment,
    ProtocolComment,
    ProtocolHistoryEvent,
    ProtocolType,
)
from protocols.services import (
    add_protocol_attachment,
    add_speech,
    approve_protocol,
    create_protocol,
    return_protocol_for_revision,
    send_protocol_for_approval,
)
from protocols.tests import _employee
from tasks.models import Task


class ProtocolCollaborationTests(TestCase):
    """Comments, attachments, and the fact that files are never a media URL."""

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.otk = Department.objects.create(name='ОТК', code='OTK')
        cls.author = _employee('collab_author', cls.otk, 'Иван', 'Петров')
        cls.reader = _employee('collab_reader', cls.otk, 'Пётр', 'Сидоров')

    def test_a_comment_and_a_file_persist_and_the_file_is_served_by_the_view(self):
        protocol = create_protocol(self.quality, self.author)
        self.client.force_login(self.author)
        collaboration = reverse('protocols:detail', args=[protocol.pk]) + '?tab=collaboration'

        added = self.client.post(
            reverse('protocols:add_comment', args=[protocol.pk]),
            {'text': 'Уточните формулировку второго решения.'},
        )

        self.assertRedirects(added, collaboration)
        comment = ProtocolComment.objects.get(protocol=protocol)
        self.assertEqual(comment.author, self.author)
        self.assertEqual(comment.text, 'Уточните формулировку второго решения.')
        # The business event is recorded; the text of the message is not.
        event = protocol.history_events.get(
            event_type=ProtocolHistoryEvent.EventType.COMMENT_ADDED
        )
        self.assertNotIn('формулировку', event.message)

        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            uploaded = self.client.post(
                reverse('protocols:add_attachment', args=[protocol.pk]),
                {
                    'file': SimpleUploadedFile('отчёт.txt', b'evidence', 'text/plain'),
                    'description': 'Протокол испытаний',
                },
            )
            self.assertRedirects(uploaded, collaboration)
            attachment = ProtocolAttachment.objects.get(protocol=protocol)
            self.assertEqual(attachment.original_name, 'отчёт.txt')
            self.assertEqual(attachment.uploaded_by, self.author)
            self.assertEqual(attachment.file_size, len(b'evidence'))
            # The stored path is a UUID under the protocol's own tree: the
            # name the browser sent never becomes part of it.
            self.assertTrue(attachment.file.name.startswith(f'protocols/attachments/{protocol.pk}/'))
            self.assertNotIn('отчёт', attachment.file.name)

            download = reverse(
                'protocols:download_attachment', args=[protocol.pk, attachment.pk]
            )
            # `FileResponse` holds the storage handle open until it is
            # closed, which on Windows is also what lets the temporary media
            # root be removed at the end of the block.
            response = self.client.get(download)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'evidence')
            response.close()

            # Any authenticated reader may take it — every user may read every
            # protocol — but an anonymous one is bounced to the login page and
            # never reaches storage.
            self.client.force_login(self.reader)
            reader_response = self.client.get(download)
            self.assertEqual(reader_response.status_code, 200)
            reader_response.close()
            self.client.logout()
            self.assertEqual(self.client.get(download).status_code, 302)

            # Only the uploader (or an administrator) may remove it.
            self.client.force_login(self.reader)
            refused = self.client.post(
                reverse('protocols:delete_attachment', args=[protocol.pk, attachment.pk])
            )
            self.assertRedirects(refused, collaboration)
            self.assertTrue(ProtocolAttachment.objects.filter(pk=attachment.pk).exists())

            # …while the uploader may.
            self.client.force_login(self.author)
            with self.captureOnCommitCallbacks(execute=True):
                removed = self.client.post(
                    reverse('protocols:delete_attachment', args=[protocol.pk, attachment.pk])
                )
            self.assertRedirects(removed, collaboration)
            self.assertFalse(ProtocolAttachment.objects.filter(pk=attachment.pk).exists())

            # Archiving freezes the feed but not the reading of it: a second
            # file survives, keeps being downloadable, and nothing new is
            # accepted — not even from the uploader who put it there.
            kept = add_protocol_attachment(
                protocol,
                self.author,
                SimpleUploadedFile('исходные.txt', b'data', 'text/plain'),
            )
            protocol.status = Protocol.Status.ARCHIVED
            protocol.save(update_fields=['status', 'updated_at'])

            page = self.client.get(collaboration)
            self.assertEqual(page.status_code, 200)
            self.assertFalse(page.context['can_contribute'])
            self.client.post(
                reverse('protocols:add_comment', args=[protocol.pk]), {'text': 'Поздно.'}
            )
            self.client.post(
                reverse('protocols:delete_attachment', args=[protocol.pk, kept.pk])
            )
            self.assertEqual(ProtocolComment.objects.filter(protocol=protocol).count(), 1)
            self.assertTrue(ProtocolAttachment.objects.filter(pk=kept.pk).exists())
            archived_download = self.client.get(
                reverse('protocols:download_attachment', args=[protocol.pk, kept.pk])
            )
            self.assertEqual(archived_download.status_code, 200)
            archived_download.close()


class ProtocolReturnCommentTests(TestCase):
    """The mandatory return reason reaches the feed — with the return, or not at all."""

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.otk = Department.objects.create(name='ОТК', code='OTK')
        cls.to = Department.objects.create(name='ТО', code='TO')
        cls.author = _employee('return_author', cls.otk, 'Иван', 'Петров')
        cls.reviewer = _employee('return_reviewer', cls.to, 'Пётр', 'Сидоров')

    def _under_approval(self):
        protocol = create_protocol(self.quality, self.author)
        ProtocolAgendaItem.objects.create(protocol=protocol, text='Вопрос', display_order=0)
        add_speech(
            protocol, protocol.participants.get(user=self.author), 'Доложил.'
        )
        action = ProtocolAction.objects.create(
            protocol=protocol,
            task_text='Проверить оснастку',
            department=self.to,
            due_date=timezone.localdate() + timedelta(days=7),
            display_order=0,
        )
        ProtocolActionAssignee.objects.create(action=action, user=self.reviewer)
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        return protocol

    def test_the_return_reason_reaches_the_feed_once_and_only_on_success(self):
        protocol = self._under_approval()

        # The notification is the last thing the transaction does; a failure
        # there must take the whole return with it, comment included.
        with patch(
            'notifications.services.notify_protocol_returned',
            side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                return_protocol_for_revision(protocol, self.reviewer, 'Уточните сроки.')

        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)
        self.assertFalse(ProtocolComment.objects.filter(protocol=protocol).exists())

        return_protocol_for_revision(protocol, self.reviewer, '  Уточните сроки.  ')
        protocol.refresh_from_db()

        self.assertEqual(protocol.status, Protocol.Status.REVISION)
        comment = ProtocolComment.objects.get(protocol=protocol)
        self.assertEqual(comment.text, 'Уточните сроки.')
        self.assertEqual(comment.author, self.reviewer)
        # The other two records keep their own purposes, unchanged.
        approval = protocol.approvals.get(user=self.reviewer, revision=protocol.revision)
        self.assertEqual(approval.return_comment, 'Уточните сроки.')
        self.assertEqual(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.RETURNED_FOR_REVISION
            ).count(),
            1,
        )
        # …and the return is not also announced as a comment: one workflow
        # event describes it, not two.
        self.assertFalse(
            protocol.history_events.filter(
                event_type=ProtocolHistoryEvent.EventType.COMMENT_ADDED
            ).exists()
        )


class ProtocolRelatedActivitiesTests(TestCase):
    """Only real `PROTOCOL_ACTION` tasks, and split ones as separate rows."""

    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.otk = Department.objects.create(name='ОТК', code='OTK')
        cls.to = Department.objects.create(name='ТО', code='TO')
        cls.author = _employee('rel_author', cls.otk, 'Иван', 'Петров')
        cls.first = _employee('rel_first', cls.to, 'Пётр', 'Сидоров')
        cls.second = _employee('rel_second', cls.to, 'Анна', 'Кузнецова')

    def test_approval_tasks_are_excluded_and_split_tasks_are_separate_rows(self):
        protocol = create_protocol(self.quality, self.author)
        ProtocolAgendaItem.objects.create(protocol=protocol, text='Вопрос', display_order=0)
        add_speech(protocol, protocol.participants.get(user=self.author), 'Доложил.')
        action = ProtocolAction.objects.create(
            protocol=protocol,
            task_text='Изучение интерфейса',
            department=self.to,
            due_date=timezone.localdate() + timedelta(days=7),
            split_for_assignees=True,
            display_order=0,
        )
        for user in (self.first, self.second):
            ProtocolActionAssignee.objects.create(action=action, user=user)

        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        self.client.force_login(self.author)
        activities = reverse('protocols:detail', args=[protocol.pk]) + '?tab=activities'

        # Under approval there are two `PROTOCOL_APPROVAL` tasks and no work.
        self.assertEqual(
            Task.objects.filter(
                protocol=protocol, source_type=Task.SourceType.PROTOCOL_APPROVAL
            ).count(),
            2,
        )
        pending = self.client.get(activities)
        self.assertEqual(list(pending.context['related_tasks']), [])
        self.assertContains(pending, 'Они появятся после полного согласования протокола.')

        for user in (self.first, self.second):
            approve_protocol(protocol, user)
            protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.ARCHIVED)

        page = self.client.get(activities)
        related = list(page.context['related_tasks'])

        # One decision, split for two people: two independent rows, and the
        # approval queue entries are still nowhere in sight.
        self.assertEqual(len(related), 2)
        self.assertEqual({task.source_type for task in related}, {Task.SourceType.PROTOCOL_ACTION})
        self.assertEqual(
            sorted(task.individual_assignee_id for task in related),
            sorted([self.first.pk, self.second.pk]),
        )
        for task in related:
            self.assertContains(page, f'#{task.pk}')
            self.assertContains(page, reverse('tasks:detail', args=[task.pk]))
        # Display limits only: the мероприятие and the исполнители are clamped
        # to three lines by the browser, each name on a line of its own, and the
        # text still reaches the page in full.
        self.assertContains(page, 'text-clamp-3')
        self.assertContains(page, 'related-activities__assignee')
        self.assertContains(page, related[0].task_text)
        # The decision itself stays single wherever the document is rendered.
        self.assertEqual(protocol.actions.count(), 1)
