"""The three integration checks that carry the most risk in this module.

The formulas themselves are not retested here: they were ported unchanged
from the source repository and are covered separately.
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import WindingEntry

ENTRY = {
    'name': '100/130-40',
    'd': 100, 'D': 130, 'b': 40,
    'tapeThicknessMm': 0.3, 'heightMm': 15,
    'calibrationEnabled': False,
    'standardCoefficient': 4.4,
    'rawComplexityCoefficient': 3.1,
    'windingTimeSeconds': 66,
    'additionalOperationsTimeSeconds': 138.6,
    'totalTimeSeconds': 204.6,
}


class CalculatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='calc', password='demo12345')
        cls.other = User.objects.create_user(username='calc2', password='demo12345')

    def _create(self, user='calc', **overrides):
        self.client.force_login(User.objects.get(username=user))
        return self.client.post(
            reverse('calculator:entry_create'),
            data=json.dumps({**ENTRY, **overrides}),
            content_type='application/json',
        )

    def test_page_requires_authentication_and_is_reachable(self):
        response = self.client.get(reverse('calculator:page'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response['Location'])

        self.client.force_login(self.user)
        response = self.client.get(reverse('calculator:page'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'calculator/page.html')

    def test_creating_an_entry_persists_it_and_case_key_stays_unique(self):
        response = self._create()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['created'])
        entry = WindingEntry.objects.get()
        self.assertEqual(entry.case_key, '100/130-40')
        self.assertEqual(entry.created_by, self.user)
        # The 0,25 step is applied on the server, not taken from the browser.
        self.assertEqual(entry.complexity_coefficient, 3.0)

        # Another user, the same case written differently: one logical row.
        duplicate = self._create(user='calc2', name=' 100/130-40 ')
        self.assertEqual(duplicate.status_code, 200)
        self.assertFalse(duplicate.json()['created'])
        self.assertEqual(duplicate.json()['entry']['id'], entry.pk)
        self.assertEqual(WindingEntry.objects.count(), 1)

        # The journal is shared: the second user sees the first user's row.
        listed = self.client.get(reverse('calculator:entry_list')).json()['entries']
        self.assertEqual([item['name'] for item in listed], ['100/130-40'])

    def test_confirming_production_derives_unit_time_on_the_server(self):
        entry_id = self._create().json()['entry']['id']
        response = self.client.post(
            reverse('calculator:entry_production', args=[entry_id]),
            # A browser-supplied per-unit time must be ignored entirely.
            data=json.dumps({
                'batchQuantity': 8, 'actualBatchTimeHours': 6, 'actualUnitTimeHours': 999,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        entry = WindingEntry.objects.get(pk=entry_id)
        self.assertTrue(entry.production_confirmed)
        self.assertEqual(entry.actual_unit_time_hours, 0.75)
        self.assertEqual(entry.updated_by, self.user)

        invalid = self.client.post(
            reverse('calculator:entry_production', args=[entry_id]),
            data=json.dumps({'batchQuantity': 0, 'actualBatchTimeHours': 6}),
            content_type='application/json',
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('batchQuantity', invalid.json()['errors'])
