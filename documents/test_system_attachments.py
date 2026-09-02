"""Three focused tests for the «Вложения» branch.

Visibility and download, immutability for every role, and the fact that a
reference is a projection rather than a copy. Nothing else: the listing rules
themselves belong to `acts`, `protocols` and `tasks` and are tested there.
"""

import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActAttachment
from protocols.models import Protocol, ProtocolAttachment, ProtocolType
from references.models import ActStatus, TaskStatus
from tasks.models import Task, TaskAttachment

from .models import Document, DocumentFolder


MEDIA_OVERRIDE = tempfile.mkdtemp(prefix='documents-system-tests-')


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-12345')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role, 'is_active': True})
    return user


def _upload(name):
    return SimpleUploadedFile(name, b'binary-content', content_type='application/octet-stream')


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class SystemAttachmentTests(TestCase):
    """One act, one protocol and one task, each with a file already attached."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    def setUp(self):
        self.user = _make_user('inspector', UserProfile.Role.OTK)
        self.admin = _make_user('librarian', UserProfile.Role.ADMIN)
        department = Department.objects.create(name='ОТК')

        act_status = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )[0]
        self.act = Act.objects.create(
            number='АОК-2026-00123',
            nomenclature='Корпус',
            created_by=self.user,
            status=act_status,
        )
        self.act_attachment = ActAttachment.objects.create(
            act=self.act,
            file=_upload('Фото дефекта.jpg'),
            original_name='Фото дефекта.jpg',
            file_size=14,
            uploaded_by=self.user,
        )

        # The project ships protocol types as reference data; reuse whichever
        # row is there rather than creating a colliding one.
        protocol_type = ProtocolType.objects.first() or ProtocolType.objects.create(
            name='Качество', code='QUALITY'
        )
        self.protocol = Protocol.objects.create(
            protocol_type=protocol_type, number=7, author=self.user
        )
        self.protocol_attachment = ProtocolAttachment.objects.create(
            protocol=self.protocol,
            file=_upload('Презентация.pdf'),
            original_name='Презентация.pdf',
            file_size=14,
            uploaded_by=self.user,
        )

        task_status = TaskStatus.objects.get_or_create(
            code='IN_PROGRESS', defaults={'name': 'В работе'}
        )[0]
        self.task = Task.objects.create(
            source_type=Task.SourceType.ACT_REJECTION,
            act=self.act,
            department=department,
            task_text='Перепланировать партию',
            due_date=timezone.localdate(),
            created_by=self.user,
            status=task_status,
        )
        self.task_attachment = TaskAttachment.objects.create(
            task=self.task,
            file=_upload('Отчет.pdf'),
            original_name='Отчет.pdf',
            file_size=14,
            uploaded_by=self.user,
        )

    # -- 1. visibility and download ---------------------------------------

    def test_user_sees_and_downloads_every_source(self):
        """The three branches list their records, and the files download."""
        self.client.force_login(self.user)

        area = self.client.get(reverse('documents:system_root'))
        self.assertEqual(area.status_code, 200)
        for label in ('Акты', 'Протоколы', 'Задачи'):
            self.assertContains(area, label)

        cases = (
            ('acts', self.act.pk, 'Акт АОК-2026-00123', self.act_attachment.pk, 'Фото дефекта.jpg'),
            ('protocols', self.protocol.pk, 'Протокол №7', self.protocol_attachment.pk, 'Презентация.pdf'),
            ('tasks', self.task.pk, f'Задача №{self.task.pk}', self.task_attachment.pk, 'Отчет.pdf'),
        )
        for source, object_id, record_label, attachment_id, file_name in cases:
            with self.subTest(source=source):
                listing = self.client.get(reverse('documents:system_source', args=[source]))
                self.assertContains(listing, record_label)

                files = self.client.get(
                    reverse('documents:system_record', args=[source, object_id])
                )
                self.assertContains(files, file_name)
                # The source is named on the row: this is what tells a system
                # attachment apart from a corporate document.
                self.assertContains(files, 'Источник:')

                download = self.client.get(
                    reverse('documents:system_download', args=[source, attachment_id])
                )
                self.assertEqual(download.status_code, 200)
                download.close()

    # -- 2. immutability ---------------------------------------------------

    def test_nobody_can_modify_a_system_attachment(self):
        """Upload, delete and rename are refused for a user *and* an admin."""
        targets = (
            ('post', reverse('documents:system_upload', args=['acts', self.act.pk])),
            ('post', reverse('documents:system_delete', args=['acts', self.act_attachment.pk])),
            ('post', reverse('documents:system_rename', args=['acts', self.act_attachment.pk])),
            # Typed into the address bar, not posted from a form.
            ('get', reverse('documents:system_delete', args=['tasks', self.task_attachment.pk])),
            ('get', reverse('documents:system_rename', args=['protocols', self.protocol_attachment.pk])),
        )
        for actor in (self.user, self.admin):
            self.client.force_login(actor)
            for method, url in targets:
                with self.subTest(user=actor.username, url=url, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertEqual(response.status_code, 403)

        # Every source row is untouched.
        self.assertTrue(ActAttachment.objects.filter(pk=self.act_attachment.pk).exists())
        self.assertTrue(ProtocolAttachment.objects.filter(pk=self.protocol_attachment.pk).exists())
        self.assertTrue(TaskAttachment.objects.filter(pk=self.task_attachment.pk).exists())

    # -- 3. reference integrity -------------------------------------------

    def test_references_point_at_the_original_and_copy_nothing(self):
        """Browsing and downloading create no Document, folder or second file."""
        self.client.force_login(self.admin)
        act_file_path = self.act_attachment.file.name

        for source, object_id, attachment_id in (
            ('acts', self.act.pk, self.act_attachment.pk),
            ('protocols', self.protocol.pk, self.protocol_attachment.pk),
            ('tasks', self.task.pk, self.task_attachment.pk),
        ):
            self.client.get(reverse('documents:system_record', args=[source, object_id]))
            self.client.get(
                reverse('documents:system_download', args=[source, attachment_id])
            ).close()

        # The library's own tables never learned about these files.
        self.assertFalse(Document.objects.exists())
        self.assertFalse(DocumentFolder.objects.filter(is_system=False).exists())
        # One row per file, still where the owning app put it.
        self.assertEqual(ActAttachment.objects.count(), 1)
        self.assertEqual(ProtocolAttachment.objects.count(), 1)
        self.assertEqual(TaskAttachment.objects.count(), 1)
        self.act_attachment.refresh_from_db()
        self.assertEqual(self.act_attachment.file.name, act_file_path)
        self.assertTrue(act_file_path.startswith('acts/'))
