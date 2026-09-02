"""Three focused tests for document versioning.

A new upload adds a version instead of replacing one, only a manager may add
one, and the history records what happened. Everything else — numbering under
a lock, the single-current constraint — is enforced by the database and the
service, and is exercised by these three going through the real endpoints.
"""

import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import UserProfile

from .models import Document, DocumentHistoryEvent, DocumentVersion
from .services import get_corporate_root, upload_document


MEDIA_OVERRIDE = tempfile.mkdtemp(prefix='documents-version-tests-')


def _pdf(name, payload=b'%PDF-1.4 v1'):
    return SimpleUploadedFile(name, payload, content_type='application/pdf')


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-12345')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role, 'is_active': True})
    # Re-read: the instance created above may already have cached a profile
    # from a signal, and the permission helper reads `user.userprofile`.
    return User.objects.get(pk=user.pk)


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class DocumentVersionTests(TestCase):
    """One document with one version, in a corporate folder."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    def setUp(self):
        self.admin = _make_user('librarian', UserProfile.Role.ADMIN)
        self.user = _make_user('inspector', UserProfile.Role.OTK)
        self.folder = get_corporate_root()
        self.document = upload_document(
            self.folder, _pdf('Инструкция.pdf'), self.admin, name='ОТК инструкция'
        )
        self.first = self.document.versions.get(number=1)

    def test_new_upload_adds_a_version_and_keeps_the_old_one(self):
        """v2 becomes current; v1 stays in the list and stays downloadable."""
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('documents:document_version_add', args=[self.document.pk]),
            {'file': _pdf('Инструкция-2.pdf', b'%PDF-1.4 v2'), 'comment': 'Уточнили пункт 4.'},
        )
        self.assertEqual(response.status_code, 302)

        versions = list(self.document.versions.order_by('number'))
        self.assertEqual([version.number for version in versions], [1, 2])
        self.assertEqual([version.is_current for version in versions], [False, True])
        # One document, not two: a new file is a revision, not a new record.
        self.assertEqual(Document.objects.filter(folder=self.folder).count(), 1)

        # The old file was neither overwritten nor renamed.
        self.first.refresh_from_db()
        self.assertNotEqual(self.first.file.name, versions[1].file.name)
        old = self.client.get(
            reverse('documents:document_version_download', args=[self.document.pk, self.first.pk])
        )
        self.assertEqual(old.status_code, 200)
        self.assertEqual(b''.join(old.streaming_content), b'%PDF-1.4 v1')
        old.close()

        # The document download now resolves to the new current version.
        current = self.client.get(
            reverse('documents:document_download', args=[self.document.pk])
        )
        self.assertEqual(b''.join(current.streaming_content), b'%PDF-1.4 v2')
        current.close()

    def test_only_a_manager_may_add_or_restore_a_version(self):
        """A normal user reads and downloads; every write answers 403."""
        self.client.force_login(self.user)

        detail = self.client.get(reverse('documents:document_detail', args=[self.document.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'v1')
        download = self.client.get(
            reverse('documents:document_version_download', args=[self.document.pk, self.first.pk])
        )
        self.assertEqual(download.status_code, 200)
        download.close()

        forbidden = (
            ('post', reverse('documents:document_version_add', args=[self.document.pk]),
             {'file': _pdf('Чужое.pdf')}),
            ('post', reverse('documents:document_version_restore',
                             args=[self.document.pk, self.first.pk]), {}),
            # Typed into the address bar rather than posted from a form.
            ('get', reverse('documents:document_version_add', args=[self.document.pk]), None),
        )
        for method, url, payload in forbidden:
            with self.subTest(url=url, method=method):
                response = (
                    getattr(self.client, method)(url, payload)
                    if payload is not None
                    else getattr(self.client, method)(url)
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(DocumentVersion.objects.filter(document=self.document).count(), 1)

    def test_history_records_creation_and_every_new_version(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse('documents:document_version_add', args=[self.document.pk]),
            {'file': _pdf('Инструкция-2.pdf', b'%PDF-1.4 v2')},
        )
        events = list(
            DocumentHistoryEvent.objects.filter(document=self.document).order_by('pk')
        )
        self.assertEqual(
            [event.action for event in events],
            [
                DocumentHistoryEvent.Action.DOCUMENT_CREATED,
                DocumentHistoryEvent.Action.VERSION_ADDED,
                DocumentHistoryEvent.Action.VERSION_ADDED,
            ],
        )
        self.assertEqual([event.version_number for event in events], [None, 1, 2])
        self.assertEqual({event.user for event in events}, {self.admin})
        # And the «История» tab shows it — the main tab deliberately does not.
        history = self.client.get(
            reverse('documents:document_detail', args=[self.document.pk]), {'tab': 'history'}
        )
        self.assertContains(history, 'Загружена версия')
        document_tab = self.client.get(
            reverse('documents:document_detail', args=[self.document.pk])
        )
        self.assertNotContains(document_tab, 'Файл и комментарий')
