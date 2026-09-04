"""The second protocol kind is offered and numbers independently.

Deliberately one test: «Web-система» needed no code, so what is worth pinning
is that the type-agnostic machinery really covers it — the creation page offers
both kinds, and the new one starts its own series at №1 rather than continuing
«Качество»'s.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, UserProfile

from .models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    WEB_SYSTEM_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolType,
)


class WebSystemProtocolTypeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        department = Department.objects.create(code='DEP', name='Отдел')
        cls.author = User.objects.create_user(username='author', password='demo12345')
        cls.author.userprofile.role = UserProfile.Role.OTK
        cls.author.userprofile.department = department
        cls.author.userprofile.save()

    def test_both_kinds_are_offered_and_numbered_independently(self):
        quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        web = ProtocolType.objects.get(code=WEB_SYSTEM_PROTOCOL_TYPE_CODE)
        self.assertTrue(web.is_active)
        self.assertEqual(web.name, 'Web-система')
        # «Качество» first, «Web-система» after it — the seeded display order.
        self.assertEqual(
            [item.code for item in ProtocolType.objects.filter(is_active=True)],
            [QUALITY_PROTOCOL_TYPE_CODE, WEB_SYSTEM_PROTOCOL_TYPE_CODE],
        )

        self.client.force_login(self.author)
        page = self.client.get(reverse('protocols:create'))
        self.assertEqual(
            [item.code for item in page.context['protocol_types']],
            [QUALITY_PROTOCOL_TYPE_CODE, WEB_SYSTEM_PROTOCOL_TYPE_CODE],
        )
        self.assertContains(page, 'Web-система')

        # Each series is its own: the first protocol of either kind is №1.
        for protocol_type in (quality, web):
            response = self.client.post(
                reverse('protocols:create'), {'protocol_type': protocol_type.pk},
            )
            protocol = Protocol.objects.get(protocol_type=protocol_type)
            self.assertRedirects(
                response, reverse('protocols:detail', args=[protocol.pk]),
            )
            self.assertEqual(protocol.number, 1)
            self.assertEqual(protocol.author, self.author)

        # And the registry lists both, each row named by its own kind.
        listing = self.client.get(reverse('protocols:list'))
        self.assertEqual(
            sorted(
                item.protocol_type.name for item in listing.context['protocols']
            ),
            ['Web-система', 'Качество'],
        )
        self.assertContains(listing, 'Web-система')
