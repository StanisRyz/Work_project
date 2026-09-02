"""Three focused tests for the archive conveniences.

Favourites are private per user, folder administration is a manager's and
refuses to destroy content, and a card in a personal block leads to the
document it names.
"""

import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import UserProfile

from .models import DocumentFavorite, DocumentFolder
from .services import DocumentError, create_folder, delete_folder, get_corporate_root, upload_document


MEDIA_OVERRIDE = tempfile.mkdtemp(prefix='documents-archive-tests-')


def _pdf(name='Инструкция.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test', content_type='application/pdf')


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-12345')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role, 'is_active': True})
    # Re-read: the permission helper reads `user.userprofile`, which the
    # instance above may already have cached from a signal.
    return User.objects.get(pk=user.pk)


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class ArchiveTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    def setUp(self):
        self.admin = _make_user('librarian', UserProfile.Role.ADMIN)
        self.user = _make_user('inspector', UserProfile.Role.OTK)
        self.other = _make_user('welder', UserProfile.Role.OTK)
        self.corporate = get_corporate_root()
        self.document = upload_document(
            self.corporate, _pdf(), self.admin, name='ОТК инструкция'
        )

    def test_favorites_are_personal_and_isolated(self):
        """Starring is per user: one person's list never reaches another's."""
        toggle = reverse('documents:favorite_toggle', args=[self.document.pk])

        self.client.force_login(self.user)
        self.client.post(toggle)
        self.assertTrue(
            DocumentFavorite.objects.filter(user=self.user, document=self.document).exists()
        )
        root = self.client.get(reverse('documents:browse'))
        self.assertContains(root, 'Избранное')
        self.assertContains(root, 'ОТК инструкция')

        # The other user sees no favourites at all, and starring is a no-op
        # for the first user's row.
        self.client.force_login(self.other)
        other_root = self.client.get(reverse('documents:browse'))
        self.assertNotContains(other_root, 'Избранное')
        self.client.post(toggle)
        self.assertEqual(DocumentFavorite.objects.filter(document=self.document).count(), 2)

        # And unstarring removes only the caller's own row.
        self.client.post(toggle)
        self.assertEqual(
            list(DocumentFavorite.objects.filter(document=self.document).values_list(
                'user_id', flat=True
            )),
            [self.user.pk],
        )

    def test_only_a_manager_administers_folders_and_content_is_protected(self):
        """A manager creates, renames and deletes empty folders; a user cannot."""
        self.client.force_login(self.admin)
        self.client.post(
            reverse('documents:subfolder_create', args=[self.corporate.pk]),
            {'name': 'Производство'},
        )
        folder = DocumentFolder.objects.get(name='Производство')
        self.client.post(
            reverse('documents:folder_rename', args=[folder.pk]), {'name': 'Цех навивки'}
        )
        folder.refresh_from_db()
        self.assertEqual(folder.name, 'Цех навивки')

        # Nested folders are allowed; a folder that holds one is not deletable.
        child = create_folder(folder, 'Пресс', self.admin)
        with self.assertRaises(DocumentError):
            delete_folder(folder, self.admin)
        # Nor is one that holds documents.
        with self.assertRaises(DocumentError):
            delete_folder(self.corporate, self.admin)
        # An empty one goes.
        delete_folder(child, self.admin)
        self.client.post(reverse('documents:folder_delete', args=[folder.pk]))
        self.assertFalse(DocumentFolder.objects.filter(pk=folder.pk).exists())

        # A normal user is refused every one of those, over POST and GET.
        self.client.force_login(self.user)
        for method, url, payload in (
            ('post', reverse('documents:subfolder_create', args=[self.corporate.pk]),
             {'name': 'Своя папка'}),
            ('post', reverse('documents:folder_rename', args=[self.corporate.pk]),
             {'name': 'Чужое'}),
            ('get', reverse('documents:folder_delete', args=[self.corporate.pk]), None),
        ):
            with self.subTest(url=url, method=method):
                response = (
                    getattr(self.client, method)(url, payload)
                    if payload is not None
                    else getattr(self.client, method)(url)
                )
                self.assertEqual(response.status_code, 403)
        self.assertFalse(DocumentFolder.objects.filter(name='Своя папка').exists())

    def test_a_card_opens_the_document_it_names(self):
        """The link on a favourite/recent card reaches the document page."""
        self.client.force_login(self.user)
        self.client.post(reverse('documents:favorite_toggle', args=[self.document.pk]))

        detail_url = reverse('documents:document_detail', args=[self.document.pk])
        root = self.client.get(reverse('documents:browse'))
        # Both personal blocks render the same card, pointing at the same page.
        self.assertContains(root, 'Недавние документы')
        self.assertContains(root, f'href="{detail_url}"')

        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'ОТК инструкция')
        self.assertContains(detail, '★ В избранном')
