"""Three focused tests for the documentation library.

Deliberately small: the two permission levels from both sides, and the fact
that the initial folders exist exactly once. Everything else the module does
is either Django's own behaviour or covered by the checks these three make.
"""

import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import UserProfile

from .models import CORPORATE_FOLDER_CODE, Document, DocumentFolder, DocumentVersion
from .services import DEFAULT_FOLDERS, ensure_default_folders, get_corporate_root


# Uploads go to a throwaway directory: a test must never write into the real
# MEDIA_ROOT next to act and protocol attachments.
MEDIA_OVERRIDE = tempfile.mkdtemp(prefix='documents-tests-')


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-12345')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role, 'is_active': True})
    return user


def _sample_file(name='Инструкция.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test', content_type='application/pdf')


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class DocumentAccessTests(TestCase):
    """A normal user reads the library and cannot change it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    def setUp(self):
        self.user = _make_user('reader', UserProfile.Role.OTK)
        self.folder = DocumentFolder.objects.create(
            name='Инструкции ОТК', parent=get_corporate_root()
        )
        # A document is its versions: created together, as the service does.
        self.document = Document.objects.create(folder=self.folder, name='Инструкция.pdf')
        DocumentVersion.objects.create(
            document=self.document,
            file=_sample_file(),
            number=1,
            is_current=True,
            original_name='Инструкция.pdf',
            file_size=13,
        )
        self.client.force_login(self.user)

    def test_user_can_browse_and_download(self):
        listing = self.client.get(reverse('documents:folder', args=[self.folder.pk]))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, 'Инструкция.pdf')

        download = self.client.get(
            reverse('documents:document_download', args=[self.document.pk])
        )
        self.assertEqual(download.status_code, 200)
        download.close()

    def test_user_cannot_create_upload_or_delete(self):
        """Every management URL answers 403 and changes nothing.

        Checked over POST *and* GET: hiding the buttons is not the rule, the
        server is, and a typed-in URL must be refused just as firmly.
        """
        forbidden = (
            ('post', reverse('documents:folder_create'), {'name': 'Своя папка'}),
            ('post', reverse('documents:document_upload', args=[self.folder.pk]),
             {'file': _sample_file('Прочее.pdf')}),
            ('post', reverse('documents:document_delete', args=[self.document.pk]), {}),
            ('post', reverse('documents:folder_delete', args=[self.folder.pk]), {}),
            ('get', reverse('documents:folder_delete', args=[self.folder.pk]), None),
        )
        for method, url, payload in forbidden:
            with self.subTest(url=url, method=method):
                response = getattr(self.client, method)(url, payload) if payload is not None \
                    else getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 403)

        # The five system folders from the data migration are also present;
        # what matters is that nothing was added or removed by the attempts.
        self.assertEqual(DocumentFolder.objects.filter(is_system=False).count(), 1)
        self.assertEqual(Document.objects.count(), 1)


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class DocumentAdminOperationTests(TestCase):
    """An administrator creates a folder, uploads a document and deletes it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    def setUp(self):
        self.admin = _make_user('librarian', UserProfile.Role.ADMIN)
        self.client.force_login(self.admin)

    def test_admin_manages_corporate_documents(self):
        corporate = get_corporate_root()
        self.client.post(
            reverse('documents:subfolder_create', args=[corporate.pk]),
            {'name': 'Производство'},
        )
        folder = DocumentFolder.objects.get(name='Производство')
        self.assertEqual(folder.parent, corporate)
        self.assertEqual(folder.created_by, self.admin)

        self.client.post(
            reverse('documents:document_upload', args=[folder.pk]),
            {'file': _sample_file()},
        )
        document = Document.objects.get(folder=folder)
        self.assertEqual(document.name, 'Инструкция.pdf')
        self.assertEqual(document.uploaded_by, self.admin)

        self.client.post(reverse('documents:document_delete', args=[document.pk]))
        self.assertFalse(Document.objects.filter(pk=document.pk).exists())

    def test_executable_upload_is_refused(self):
        folder = DocumentFolder.objects.create(name='Обмен', parent=get_corporate_root())
        response = self.client.post(
            reverse('documents:document_upload', args=[folder.pk]),
            {'file': SimpleUploadedFile('setup.exe', b'MZ', content_type='application/octet-stream')},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Document.objects.exists())


class DefaultFolderTests(TestCase):
    """The initial structure exists after migration and is never duplicated."""

    def test_default_folders_exist_once(self):
        corporate = get_corporate_root()
        self.assertIsNotNone(corporate)
        self.assertIsNone(corporate.parent)
        self.assertEqual(
            set(DocumentFolder.objects.filter(parent=corporate).values_list('name', flat=True)),
            {name for _code, name in DEFAULT_FOLDERS},
        )
        # «Корпоративные документы» is the only folder at the root: the other
        # branch, «Вложения», is generated and has no row.
        self.assertEqual(
            list(DocumentFolder.objects.filter(parent__isnull=True).values_list('code', flat=True)),
            [CORPORATE_FOLDER_CODE],
        )
        # Running the setup again — a re-applied migration, a deploy check —
        # must add nothing.
        self.assertEqual(ensure_default_folders(), [])
        self.assertEqual(
            DocumentFolder.objects.filter(is_system=True).count(), len(DEFAULT_FOLDERS) + 1
        )
