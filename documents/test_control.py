"""Controlled documents: the card, approval, acknowledgement, review, the
trash, moving, links, subscriptions and the text search."""

import io
import shutil
import tempfile
import zipfile
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act
from notifications.models import Notification
from notifications.services import get_notification_url
from references.models import ActStatus
from tasks.models import Task
from tasks.services import TaskWorkflowError, complete_task

from .models import (
    Document,
    DocumentFolder,
    DocumentHistoryEvent,
    DocumentLink,
    DocumentVersion,
    DocumentVersionApproval,
)
from .search import search_documents
from .services import (
    DocumentError,
    acknowledge_document,
    add_document_version,
    approve_version,
    create_review_tasks,
    get_corporate_root,
    purge_document,
    purge_expired_trash,
    request_acknowledgement,
    return_version,
    toggle_subscription,
    trash_document,
    update_document_card,
    upload_document,
)
from .text_extraction import extract_text


MEDIA_OVERRIDE = tempfile.mkdtemp(prefix='documents-control-tests-')


def _pdf(name='Инструкция.pdf', payload=b'%PDF-1.4 v1'):
    return SimpleUploadedFile(name, payload, content_type='application/pdf')


def _docx(text, name='Инструкция.docx'):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(
            'word/document.xml',
            '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>'
            + text + '</w:t></w:r></w:p><w:p><w:r><w:t>Второй абзац</w:t></w:r></w:p></w:body></w:document>',
        )
    return SimpleUploadedFile(
        name, buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class ControlledDocumentTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())
        cls.department, _ = Department.objects.get_or_create(code='CTRL', defaults={'name': 'Отдел качества'})
        cls.admin = cls._user('keeper', UserProfile.Role.ADMIN)
        cls.otk = cls._user('otk_one', UserProfile.Role.OTK)
        cls.otk2 = cls._user('otk_two', UserProfile.Role.OTK)
        cls.to = cls._user('to_one', UserProfile.Role.TO)
        cls.manager = cls._user('boss', UserProfile.Role.MANAGER)

    @classmethod
    def _user(cls, username, role):
        user = User.objects.create_user(username=username, password='pw-12345', first_name=username)
        UserProfile.objects.update_or_create(
            user=user, defaults={'role': role, 'is_active': True, 'department': cls.department},
        )
        return User.objects.get(pk=user.pk)

    def setUp(self):
        self.folder = DocumentFolder.objects.create(name='Инструкции ОТК', parent=get_corporate_root())
        self.document = upload_document(self.folder, _pdf(), self.admin, name='Контроль изоляции')

    # ------------------------------------------------------------ the card

    def test_the_card_is_saved_and_cancelling_needs_a_reason(self):
        update_document_card(
            self.document, self.admin, name='Контроль изоляции', designation='ИОТ-015',
            effective_date=timezone.localdate(), owner_department=self.department, responsible=self.otk,
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.title, 'ИОТ-015 Контроль изоляции')
        self.assertTrue(
            DocumentHistoryEvent.objects.filter(document=self.document, action='CARD_UPDATED').exists()
        )
        with self.assertRaises(DocumentError):
            update_document_card(self.document, self.admin, status=Document.Status.CANCELLED)
        update_document_card(self.document, self.admin, status=Document.Status.CANCELLED,
                             cancellation_reason='Заменён СТО 3.2')
        self.document.refresh_from_db()
        # Withdrawn, not deleted: still listed, still readable.
        self.assertEqual(self.document.status, Document.Status.CANCELLED)
        self.assertEqual(self.document.cancelled_by, self.admin)
        self.client.force_login(self.otk)
        self.assertContains(
            self.client.get(reverse('documents:document_detail', args=[self.document.pk])), 'Заменён СТО 3.2',
        )
        # Nobody but a manager edits the card.
        with self.assertRaises(DocumentError):
            update_document_card(self.document, self.otk, name='Чужое')

    # ------------------------------------------------------- acknowledgement

    def test_acknowledgement_tasks_are_personal_and_closed_by_the_reader(self):
        count = request_acknowledgement(self.document, self.admin, roles=[UserProfile.Role.OTK])
        self.assertEqual(count, 2)
        # A repeated request asks nobody twice.
        self.assertEqual(request_acknowledgement(self.document, self.admin, roles=[UserProfile.Role.OTK]), 0)
        task = Task.objects.get(source_type=Task.SourceType.DOCUMENT_ACK, individual_assignee=self.otk)
        self.assertTrue(task.is_routing_task)
        self.assertEqual(list(task.assignees.values_list('user_id', flat=True)), [self.otk.pk])
        notification = Notification.objects.get(recipient=self.otk, event_type='DOCUMENT_ACK_REQUIRED')
        self.assertEqual(notification.source_type, Notification.SourceType.DOCUMENT)
        self.assertEqual(
            get_notification_url(notification), reverse('documents:document_detail', args=[self.document.pk]),
        )

        # «Задачи» sends the reader to the document, and never completes it.
        self.client.force_login(self.otk)
        response = self.client.get(reverse('tasks:detail', args=[task.pk]))
        self.assertRedirects(response, reverse('documents:document_detail', args=[self.document.pk]),
                             fetch_redirect_response=False)
        with self.assertRaises(TaskWorkflowError):
            complete_task(task, self.otk, 'прочитал')

        page = self.client.get(reverse('documents:document_detail', args=[self.document.pk]))
        self.assertContains(page, 'Ознакомлен')
        self.client.post(reverse('documents:document_acknowledge', args=[self.document.pk]))
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.completed_by, self.otk)
        with self.assertRaises(DocumentError):
            acknowledge_document(self.document, self.otk)
        with self.assertRaises(DocumentError):
            acknowledge_document(self.document, self.to)

    def test_upload_with_ack_and_a_new_version_withdraws_unread_ones(self):
        document = upload_document(self.folder, _pdf('Второй.pdf'), self.admin,
                                   ack_roles=[UserProfile.Role.OTK], ack_department_ids=[])
        open_ack = Task.objects.filter(document_version__document=document, source_type='DOCUMENT_ACK')
        self.assertEqual(open_ack.count(), 2)
        acknowledge_document(document, self.otk)
        add_document_version(document, _pdf('Второй-2.pdf', b'%PDF v2'), self.admin, comment='Раздел 2')
        states = dict(open_ack.values_list('individual_assignee__username', 'status__code'))
        # What was read stays read; what was not is withdrawn — nobody
        # confirms an outdated text.
        self.assertEqual(states, {'otk_one': 'COMPLETED', 'otk_two': 'CANCELLED'})

    def test_a_closed_folder_is_closed_for_acknowledgement_and_search(self):
        self.folder.allowed_roles = [UserProfile.Role.TO]
        self.folder.save(update_fields=['allowed_roles'])
        self.assertEqual(request_acknowledgement(self.document, self.admin, roles=[UserProfile.Role.OTK]), 0)
        self.assertEqual(search_documents(self.otk, 'Контроль'), [])
        self.assertEqual(len(search_documents(self.to, 'Контроль')), 1)

    # ------------------------------------------------------------- approval

    def test_a_version_on_approval_comes_into_force_after_the_last_approval(self):
        version = add_document_version(
            self.document, _pdf('v2.pdf', b'%PDF v2'), self.admin, comment='Новый раздел',
            revision_label='2', approver_ids=[self.manager.pk, self.to.pk, self.admin.pk],
            ack_roles=[UserProfile.Role.OTK],
        )
        # The uploader never approves their own version.
        self.assertEqual(
            set(version.approvals.values_list('user_id', flat=True)), {self.manager.pk, self.to.pk},
        )
        self.assertFalse(version.is_current)
        self.assertEqual(self.document.current_version.number, 1)
        self.assertEqual(Task.objects.filter(source_type='DOCUMENT_APPROVAL', status__code='IN_PROGRESS').count(), 2)
        self.assertTrue(Notification.objects.filter(recipient=self.to, event_type='DOCUMENT_APPROVAL_REQUIRED').exists())
        # Nobody else signs, and one pending version at a time.
        with self.assertRaises(DocumentError):
            approve_version(version, self.otk)
        with self.assertRaises(DocumentError):
            add_document_version(self.document, _pdf('v3.pdf'), self.admin, comment='ещё')

        self.assertFalse(approve_version(version, self.manager))
        self.assertFalse(Task.objects.filter(source_type='DOCUMENT_ACK').exists())
        self.assertTrue(approve_version(version, self.to))
        version.refresh_from_db()
        self.assertTrue(version.is_current)
        self.assertEqual(version.approval_status, DocumentVersion.Approval.APPROVED)
        self.assertEqual(version.full_label, 'v2 · 2')
        # In force → the uploader's «ознакомить» is issued now, not at upload.
        self.assertEqual(Task.objects.filter(source_type='DOCUMENT_ACK', document_version=version).count(), 2)
        self.assertFalse(Task.objects.filter(source_type='DOCUMENT_APPROVAL', status__is_final=False).exists())

    def test_a_return_needs_a_reason_and_withdraws_the_round(self):
        version = add_document_version(
            self.document, _pdf('v2.pdf', b'%PDF v2'), self.admin, comment='Правка',
            approver_ids=[self.manager.pk, self.to.pk],
        )
        with self.assertRaises(DocumentError):
            return_version(version, self.manager, '  ')
        return_version(version, self.manager, 'Нет ссылки на ГОСТ')
        version.refresh_from_db()
        self.assertEqual(version.approval_status, DocumentVersion.Approval.RETURNED)
        self.assertFalse(version.is_current)
        self.assertEqual(
            dict(version.approvals.values_list('user__username', 'status')),
            {'boss': 'RETURNED', 'to_one': 'CANCELLED'},
        )
        self.assertEqual(
            Task.objects.get(source_type='DOCUMENT_APPROVAL', individual_assignee=self.to).status.code, 'CANCELLED',
        )
        self.assertTrue(
            Notification.objects.filter(recipient=self.admin, event_type='DOCUMENT_VERSION_RETURNED').exists()
        )
        # The document still reads as v1, and the next version may be uploaded.
        self.assertEqual(self.document.current_version.number, 1)
        add_document_version(self.document, _pdf('v3.pdf', b'%PDF v3'), self.admin, comment='Исправлено')

    def test_approving_through_the_page(self):
        version = add_document_version(
            self.document, _pdf('v2.pdf', b'%PDF v2'), self.admin, comment='Правка', approver_ids=[self.to.pk],
        )
        self.client.force_login(self.to)
        page = self.client.get(reverse('documents:document_detail', args=[self.document.pk]))
        self.assertContains(page, 'Согласовать')
        approval_task = Task.objects.get(source_type='DOCUMENT_APPROVAL')
        self.assertRedirects(
            self.client.get(reverse('tasks:detail', args=[approval_task.pk])),
            reverse('documents:document_detail', args=[self.document.pk]), fetch_redirect_response=False,
        )
        self.client.post(reverse('documents:document_version_approve', args=[self.document.pk, version.pk]))
        version.refresh_from_db()
        self.assertTrue(version.is_current)
        self.assertEqual(DocumentVersionApproval.objects.get().status, 'APPROVED')

    # --------------------------------------------------------------- review

    def test_review_reminders_raise_one_task_for_the_responsible(self):
        review_date = timezone.localdate() + timedelta(days=10)
        update_document_card(self.document, self.admin, review_date=review_date, responsible=self.otk)
        created = create_review_tasks()
        self.assertEqual(len(created), 1)
        task = created[0]
        self.assertEqual(task.source_type, Task.SourceType.DOCUMENT_REVIEW)
        self.assertEqual(task.due_date, review_date)
        self.assertFalse(task.is_routing_task)
        self.assertTrue(Notification.objects.filter(recipient=self.otk, event_type='DOCUMENT_REVIEW_DUE').exists())
        # Daily runs raise nothing new; the command says what it did.
        out = StringIO()
        call_command('document_review_reminders', stdout=out)
        self.assertIn('0', out.getvalue())
        # Review is real work: completed with a comment like any task.
        complete_task(task, self.otk, 'Документ актуален, дата перенесена')
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        # A date far away raises nothing.
        update_document_card(self.document, self.admin, review_date=timezone.localdate() + timedelta(days=200))
        self.assertEqual(create_review_tasks(), [])

    # ---------------------------------------------------------------- trash

    def test_the_trash_hides_restores_and_keeps_records(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('documents:document_delete', args=[self.document.pk]))
        self.assertFalse(Document.objects.filter(pk=self.document.pk).exists())
        self.assertNotContains(self.client.get(reverse('documents:folder', args=[self.folder.pk])), 'Контроль изоляции')
        self.assertContains(self.client.get(reverse('documents:trash')), 'Контроль изоляции')
        # An ordinary reader does not reach the trash or a trashed document.
        self.client.force_login(self.otk)
        self.assertEqual(self.client.get(reverse('documents:trash')).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('documents:document_detail', args=[self.document.pk])).status_code, 404,
        )

        self.client.force_login(self.admin)
        self.client.post(reverse('documents:document_restore', args=[self.document.pk]))
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

        # A document tasks were issued on is a record: trashed, never purged.
        request_acknowledgement(self.document, self.admin, roles=[UserProfile.Role.OTK])
        trash_document(self.document, self.admin)
        self.assertFalse(Task.objects.filter(source_type='DOCUMENT_ACK', status__is_final=False).exists())
        self.document.refresh_from_db()
        with self.assertRaises(DocumentError):
            purge_document(self.document, self.admin)

        plain = upload_document(self.folder, _pdf('Черновик.pdf'), self.admin)
        trash_document(plain, self.admin)
        Document.all_objects.filter(pk__in=[plain.pk, self.document.pk]).update(
            deleted_at=timezone.now() - timedelta(days=31),
        )
        self.assertEqual(purge_expired_trash(), (1, 1))
        self.assertFalse(Document.all_objects.filter(pk=plain.pk).exists())
        self.assertTrue(Document.all_objects.filter(pk=self.document.pk).exists())

    # ---------------------------------------------------- move and download

    def test_bulk_move_and_zip(self):
        second = upload_document(self.folder, _pdf('Второй.pdf', b'%PDF two'), self.admin)
        target = DocumentFolder.objects.create(name='Архив ОТК', parent=get_corporate_root())
        self.client.force_login(self.otk)
        zipped = self.client.post(reverse('documents:bulk'), {'action': 'zip', 'ids': [self.document.pk, second.pk]})
        self.assertEqual(zipped['Content-Type'], 'application/zip')
        with zipfile.ZipFile(io.BytesIO(b''.join(zipped.streaming_content))) as archive:
            self.assertEqual(sorted(archive.namelist()), ['Второй.pdf', 'Инструкция.pdf'])
        # Moving is management.
        self.assertEqual(
            self.client.post(reverse('documents:bulk'), {'action': 'move', 'ids': [second.pk], 'target': target.pk}).status_code,
            403,
        )
        self.client.force_login(self.admin)
        self.client.post(reverse('documents:bulk'), {'action': 'move', 'ids': [self.document.pk, second.pk], 'target': target.pk})
        self.assertEqual(Document.objects.filter(folder=target).count(), 2)
        self.assertTrue(DocumentHistoryEvent.objects.filter(document=second, action='MOVED').exists())

    # ------------------------------------------------ links and subscriptions

    def test_an_act_cites_a_document_and_the_document_says_where(self):
        act = Act.objects.create(
            number='АОК-2026-00077', created_by=self.otk, status=ActStatus.objects.get(code='CREATED_OTK'),
        )
        self.client.force_login(self.otk)
        next_url = reverse('acts:detail', args=[act.pk]) + '?tab=attachments'
        page = self.client.get(next_url)
        self.assertContains(page, 'Нормативные документы')
        self.client.post(reverse('documents:link_create'), {'document': self.document.pk, 'act': act.pk, 'next': next_url})
        link = DocumentLink.objects.get(act=act)
        self.assertContains(self.client.get(next_url), 'Контроль изоляции')
        self.assertContains(
            self.client.get(reverse('documents:document_detail', args=[self.document.pk])), 'Акт АОК-2026-00077',
        )
        # A reader outside the act's working scope cannot cite on it.
        self.client.force_login(self.to)
        self.client.post(reverse('documents:link_delete', args=[link.pk]))
        self.assertTrue(DocumentLink.objects.filter(pk=link.pk).exists())
        self.client.force_login(self.otk)
        self.client.post(reverse('documents:link_delete', args=[link.pk]))
        self.assertFalse(DocumentLink.objects.exists())

    def test_subscribers_hear_about_new_documents_and_versions(self):
        self.assertTrue(toggle_subscription(self.otk, folder=self.folder))
        self.assertTrue(toggle_subscription(self.to, document=self.document))
        upload_document(self.folder, _pdf('Новый.pdf'), self.admin)
        self.assertEqual(Notification.objects.filter(event_type='DOCUMENT_UPDATED', recipient=self.otk).count(), 1)
        self.assertFalse(Notification.objects.filter(event_type='DOCUMENT_UPDATED', recipient=self.to).exists())
        add_document_version(self.document, _pdf('v2.pdf', b'%PDF v2'), self.admin, comment='Правка')
        self.assertTrue(Notification.objects.filter(event_type='DOCUMENT_UPDATED', recipient=self.to).exists())
        self.assertEqual(Notification.objects.filter(event_type='DOCUMENT_UPDATED', recipient=self.otk).count(), 2)
        self.assertFalse(toggle_subscription(self.to, document=self.document))

    # ---------------------------------------------------------- text search

    def test_words_inside_a_file_are_found_and_highlighted(self):
        self.assertIn('Проверка сопротивления', extract_text(_docx('Проверка сопротивления').read(), 'docx'))
        upload_document(self.folder, _docx('Проверка сопротивления изоляции мегаомметром'), self.admin,
                        name='Методика')
        self.client.force_login(self.otk)
        response = self.client.get(reverse('documents:search'), {'q': 'мегаомметром'})
        self.assertContains(response, 'Методика')
        self.assertContains(response, '<mark>мегаомметром</mark>')
        # Filters narrow the corporate half.
        self.assertNotContains(
            self.client.get(reverse('documents:search'), {'q': 'мегаомметром', 'status': 'DRAFT'}), 'Методика',
        )
        self.assertContains(
            self.client.get(reverse('documents:search'), {'q': 'мегаомметром', 'type': 'word'}), 'Методика',
        )

    def test_the_folder_table_filters_and_sorts(self):
        update_document_card(self.document, self.admin, designation='ИОТ-015', status=Document.Status.DRAFT)
        upload_document(self.folder, _pdf('Другой.pdf'), self.admin)
        self.client.force_login(self.otk)
        url = reverse('documents:folder', args=[self.folder.pk])
        self.assertContains(self.client.get(url, {'status': 'DRAFT'}), 'Контроль изоляции')
        self.assertNotContains(self.client.get(url, {'status': 'DRAFT'}), 'Другой.pdf')
        self.assertContains(self.client.get(url, {'q': 'ИОТ'}), 'Контроль изоляции')
        rows = self.client.get(url, {'sort': '-name'}).context['document_rows']
        self.assertEqual([row['document'].name for row in rows], ['Контроль изоляции', 'Другой.pdf'])
