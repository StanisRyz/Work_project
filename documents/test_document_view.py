"""Two focused tests for the redesigned document page.

The page opens on the current version with a viewer, and the version selector
reaches an earlier revision for both reading and downloading. Who may upload a
new version is covered next door, in
`test_versions.DocumentVersionTests.test_only_a_manager_may_add_or_restore_a_version`,
and is not repeated here.
"""

import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import UserProfile

from .services import add_document_version, get_corporate_root, upload_document


MEDIA_OVERRIDE = tempfile.mkdtemp(prefix='documents-view-tests-')


def _pdf(name, payload):
    return SimpleUploadedFile(name, payload, content_type='application/pdf')


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-12345')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role, 'is_active': True})
    return User.objects.get(pk=user.pk)


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class DocumentViewTests(TestCase):
    """A PDF document with two versions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    def setUp(self):
        self.admin = _make_user('librarian', UserProfile.Role.ADMIN)
        self.user = _make_user('inspector', UserProfile.Role.OTK)
        self.document = upload_document(
            get_corporate_root(),
            _pdf('Инструкция.pdf', b'%PDF-1.4 v1'),
            self.admin,
            name='ОТК инструкция',
        )
        self.first = self.document.versions.get(number=1)
        self.second = add_document_version(
            self.document, _pdf('Инструкция-2.pdf', b'%PDF-1.4 v2'), self.admin
        )
        self.detail_url = reverse('documents:document_detail', args=[self.document.pk])
        self.client.force_login(self.user)

    def test_page_opens_on_the_current_version_with_a_viewer(self):
        """The header describes v2, and the PDF is embedded, not listed."""
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_version'], self.second)
        self.assertTrue(response.context['is_viewing_current'])
        self.assertEqual(response.context['preview']['kind'], 'pdf')

        preview_url = reverse(
            'documents:document_version_preview', args=[self.document.pk, self.second.pk]
        )
        self.assertContains(response, f'src="{preview_url}"')
        # The version table belongs to the «История» tab, not to this one.
        self.assertNotContains(response, 'Файл и комментарий')

        # The viewer streams the file itself, inline and sandboxed.
        preview = self.client.get(preview_url)
        self.assertEqual(preview['Content-Type'], 'application/pdf')
        self.assertIn('inline', preview['Content-Disposition'])
        self.assertEqual(preview['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(preview['Content-Security-Policy'], 'sandbox')
        self.assertEqual(b''.join(preview.streaming_content), b'%PDF-1.4 v2')
        preview.close()

    def test_version_selector_opens_and_downloads_an_earlier_version(self):
        """`?version=` switches the viewer; the menu's download link saves it."""
        response = self.client.get(self.detail_url, {'version': self.first.pk})
        self.assertEqual(response.context['selected_version'], self.first)
        self.assertFalse(response.context['is_viewing_current'])
        self.assertContains(response, 'не текущая')

        older_preview = self.client.get(
            reverse('documents:document_version_preview', args=[self.document.pk, self.first.pk])
        )
        self.assertEqual(b''.join(older_preview.streaming_content), b'%PDF-1.4 v1')
        older_preview.close()

        download = self.client.get(
            reverse('documents:document_version_download', args=[self.document.pk, self.first.pk])
        )
        self.assertIn('attachment', download['Content-Disposition'])
        self.assertEqual(b''.join(download.streaming_content), b'%PDF-1.4 v1')
        download.close()

        # An unknown version is a view that falls back, not a 404: the
        # parameter selects a display, it does not address a file.
        fallback = self.client.get(self.detail_url, {'version': '999999'})
        self.assertEqual(fallback.context['selected_version'], self.second)
