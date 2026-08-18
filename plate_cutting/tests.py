"""The two things worth pinning down: the agreed numbers and the page itself.

The arithmetic runs in the browser, so what a Python test can protect is the
source the browser reads — the seventeen coefficients and the page that hands
them over. The reference case from the specification is checked against those
same constants, not against a second copy of them.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .constants import HOLE_SECONDS, PLATE_LENGTH_RANGES


class PlateCuttingConstantsTests(TestCase):
    def test_reference_case_matches_the_specification(self):
        """1–170 мм, 500 пластин, 1000 отверстий → 1320 с → 0,37 ч."""
        first = PLATE_LENGTH_RANGES[0]
        self.assertEqual((first.min_mm, first.max_mm), (1, 170))

        seconds = first.seconds * 500 + HOLE_SECONDS * 1000
        self.assertEqual(seconds, Decimal('1320'))
        self.assertEqual(round(seconds / 3600, 2), Decimal('0.37'))

    def test_bands_are_the_seventeen_agreed_contiguous_ranges(self):
        self.assertEqual(len(PLATE_LENGTH_RANGES), 17)
        self.assertEqual(PLATE_LENGTH_RANGES[-1].max_mm, 2890)
        for previous, current in zip(PLATE_LENGTH_RANGES, PLATE_LENGTH_RANGES[1:]):
            self.assertEqual(current.min_mm, previous.max_mm + 1)
            self.assertGreater(current.seconds, previous.seconds)


class PlateCuttingPageTests(TestCase):
    url = '/calculators/plate-cutting/'

    def test_url_name_resolves_to_the_agreed_route(self):
        self.assertEqual(reverse('plate_cutting:page'), self.url)

    def test_page_needs_a_login_and_then_renders_every_coefficient(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

        User.objects.create_user(username='cutter', password='demo12345')
        self.client.login(username='cutter', password='demo12345')
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_page'], 'plate_cutting')
        for band in PLATE_LENGTH_RANGES:
            self.assertContains(response, f'data-seconds="{band.seconds}"')
            self.assertContains(response, band.label)
        # The submenu leaf sits next to the winding calculator.
        self.assertContains(response, 'Калькулятор рубки пластин')
