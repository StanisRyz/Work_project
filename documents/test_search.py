"""Three focused tests for search and navigation.

Both halves of the library are findable from one query, visibility follows the
existing rules rather than a rule of search's own, and the breadcrumb trail
actually leads back to the folders it names.
"""

import shutil
import tempfile

from django.contrib.auth.models import AnonymousUser, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import UserProfile
from acts.models import Act, ActAttachment
from references.models import ActStatus

from .models import Document, DocumentFolder, DocumentVersion
from .search import search_documents
from .services import get_corporate_root


MEDIA_OVERRIDE = tempfile.mkdtemp(prefix='documents-search-tests-')


@override_settings(MEDIA_ROOT=MEDIA_OVERRIDE)
class DocumentSearchTests(TestCase):
    """One corporate document and one act attachment, both matching «навивка»."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, MEDIA_OVERRIDE, True)

    def setUp(self):
        self.user = User.objects.create_user(username='chief', password='pw-12345')
        UserProfile.objects.update_or_create(
            user=self.user, defaults={'role': UserProfile.Role.MANAGER, 'is_active': True}
        )

        self.folder = DocumentFolder.objects.create(
            name='Инструкции ОТК', parent=get_corporate_root()
        )
        self.document = Document.objects.create(
            folder=self.folder, name='Навивка. Инструкция.pdf'
        )
        DocumentVersion.objects.create(
            document=self.document,
            file=SimpleUploadedFile('winding.pdf', b'%PDF-1.4', content_type='application/pdf'),
            number=1,
            is_current=True,
            original_name='Навивка. Инструкция.pdf',
            file_size=8,
        )

        act_status = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )[0]
        self.act = Act.objects.create(
            number='АОК-2026-00123',
            nomenclature='Корпус',
            created_by=self.user,
            status=act_status,
        )
        self.attachment = ActAttachment.objects.create(
            act=self.act,
            file=SimpleUploadedFile('photo.jpg', b'jpeg', content_type='image/jpeg'),
            original_name='Навивка — дефект.jpg',
            file_size=4,
            uploaded_by=self.user,
        )
        self.client.force_login(self.user)

    def test_search_finds_both_kinds_and_filters_them(self):
        """One query returns the corporate file and the act attachment."""
        response = self.client.get(reverse('documents:search'), {'q': 'Навивка'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Навивка. Инструкция.pdf')
        self.assertContains(response, 'Навивка — дефект.jpg')
        # The system hit names the act it belongs to and is marked read-only.
        self.assertContains(response, 'Акт АОК-2026-00123')
        self.assertContains(response, 'Только чтение')

        # A filter narrows the list without changing what was matched.
        corporate_only = self.client.get(
            reverse('documents:search'), {'q': 'Навивка', 'scope': 'corporate'}
        )
        self.assertContains(corporate_only, 'Навивка. Инструкция.pdf')
        self.assertNotContains(corporate_only, 'Навивка — дефект.jpg')

        # An attachment is also found by the identifier of the act that owns
        # it, which appears in no filename.
        by_act_number = self.client.get(reverse('documents:search'), {'q': 'АОК-2026-00123'})
        self.assertContains(by_act_number, 'Навивка — дефект.jpg')

        # The same two files reach «Недавние документы» on the root page,
        # through the same card.
        root = self.client.get(reverse('documents:browse'))
        self.assertContains(root, 'Недавние документы')
        self.assertContains(root, 'Навивка. Инструкция.pdf')
        self.assertContains(root, 'Навивка — дефект.jpg')

    def test_search_respects_existing_visibility_rules(self):
        """Search grants nothing on its own: no session, no results, no page."""
        self.assertEqual(search_documents(AnonymousUser(), 'Навивка'), [])

        self.client.logout()
        response = self.client.get(reverse('documents:search'), {'q': 'Навивка'})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response['Location'])

    def test_breadcrumbs_link_back_to_every_level(self):
        """Each item of the trail is a link, and each one opens its folder."""
        response = self.client.get(reverse('documents:folder', args=[self.folder.pk]))
        trail = response.context['breadcrumbs']
        self.assertEqual(
            [crumb['name'] for crumb in trail],
            ['Документация', 'Корпоративные документы', 'Инструкции ОТК'],
        )
        self.assertTrue(trail[-1]['is_current'])
        for crumb in trail:
            with self.subTest(crumb=crumb['name']):
                self.assertContains(response, f'href="{crumb["url"]}"')
                self.assertEqual(self.client.get(crumb['url']).status_code, 200)
