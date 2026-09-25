"""The markup contract of «Восстановить черновик» (`static/js/form_drafts.js`).

The script keeps nothing but what the templates tell it: which forms are
drafted, under which key, whose drafts they are, and which pages mean «the
session was lost» and «the user is leaving». Those attributes are asserted
here; the behaviour itself runs in the browser.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, UserProfile
from protocols.models import QUALITY_PROTOCOL_TYPE_CODE, ProtocolType
from protocols.services import create_protocol


class FormDraftMarkupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        department = Department.objects.create(name='Отдел', code='DRAFT_DEP')
        cls.user = User.objects.create_user('draft_owner', password='pw-12345')
        profile = cls.user.userprofile
        profile.role = UserProfile.Role.ADMIN
        profile.department = department
        profile.save(update_fields=['role', 'department'])

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_page_loads_the_script_and_marks_the_logout_form(self):
        page = self.client.get(reverse('acts:list'))

        self.assertContains(page, 'js/form_drafts.js')
        self.assertContains(page, f'data-logout-form data-draft-owner="{self.user.pk}"')

    def test_the_login_page_is_recognisable(self):
        self.client.logout()

        self.assertContains(self.client.get(reverse('accounts:login')), 'data-login-form')

    def test_the_act_form_is_drafted_per_user(self):
        page = self.client.get(reverse('acts:create'))

        self.assertContains(page, f'data-draft-key="{self.user.pk}:act:new"')

    def test_the_smk_form_is_drafted_per_user(self):
        page = self.client.get(reverse('smk:create'))

        self.assertContains(page, f'data-draft-key="{self.user.pk}:smk:new"')

    def test_the_protocol_editor_is_drafted_per_protocol(self):
        protocol = create_protocol(
            ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE), self.user
        )

        page = self.client.get(reverse('protocols:detail', args=[protocol.pk]))

        self.assertContains(page, f'data-draft-key="{self.user.pk}:protocol:{protocol.pk}"')
        self.assertContains(page, 'data-draft-base="')
