"""The integration checks that carry the most risk in this module.

The formulas themselves are not retested here: they were ported unchanged
from the source repository and are covered separately.
"""
import io
import json
import zipfile

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .expressions import OneCExpressionError, evaluate_one_c
from .models import EntrySource, WindingEntry

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

    def _post(self, url_name, user='calc', **overrides):
        self.client.force_login(User.objects.get(username=user))
        return self.client.post(
            reverse(url_name),
            data=json.dumps({**ENTRY, **overrides}),
            content_type='application/json',
        )

    def _create(self, user='calc', **overrides):
        return self._post('calculator:entry_create', user=user, **overrides)

    def test_page_requires_authentication_and_is_reachable(self):
        response = self.client.get(reverse('calculator:page'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response['Location'])

        self.client.force_login(self.user)
        response = self.client.get(reverse('calculator:page'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'calculator/page.html')

    def test_calculator_deduplicates_whole_cases_while_manual_rows_repeat(self):
        first = self._create()
        self.assertEqual(first.status_code, 201)
        entry = WindingEntry.objects.get()
        self.assertEqual(entry.source, EntrySource.CALCULATOR)
        # The 0,25 step is applied on the server, not taken from the browser.
        self.assertEqual(entry.complexity_coefficient, 3.0)

        # The very same calculation, from another user and written with
        # stray whitespace: still one logical row.
        repeated = self._create(user='calc2', name=' 100/130-40 ')
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.json()['created'])
        self.assertEqual(repeated.json()['entry']['id'], entry.pk)

        # The same core with another tape, and with calibration: new cases.
        self.assertTrue(self._create(tapeThicknessMm=0.23).json()['created'])
        self.assertTrue(self._create(
            calibrationEnabled=True, calibrationDiameterMm=15,
        ).json()['created'])
        self.assertEqual(WindingEntry.objects.count(), 3)

        # Added by hand, twice, with parameters that already exist: two more
        # rows, told apart by their primary keys alone.
        manual_ids = set()
        for _ in range(2):
            response = self._post('calculator:entry_manual_create')
            self.assertEqual(response.status_code, 201)
            manual_ids.add(response.json()['entry']['id'])
        self.assertEqual(len(manual_ids), 2)
        self.assertNotIn(entry.pk, manual_ids)
        self.assertEqual(WindingEntry.objects.filter(source=EntrySource.MANUAL).count(), 2)
        self.assertEqual(WindingEntry.objects.count(), 5)

    def test_one_c_expressions_are_seconds_and_are_stored_as_hours(self):
        # The arithmetic is in seconds; the journal column is in hours.
        self.assertEqual(evaluate_one_c('4.4*75*30'), ('4.4*75*30', 2.75))
        self.assertEqual(evaluate_one_c('3600')[1], 1)
        self.assertAlmostEqual(evaluate_one_c('4,4*62')[1], 272.8 / 3600)
        self.assertEqual(evaluate_one_c('  '), ('', None))
        for bad in ('100/0', '__import__("os")', '2**8', '2 3', '(1+2', '5;9'):
            with self.assertRaises(OneCExpressionError):
                evaluate_one_c(bad)

        entry_id = self._create().json()['entry']['id']
        response = self.client.post(
            reverse('calculator:entry_production', args=[entry_id]),
            # A browser-supplied per-unit time must be ignored entirely.
            data=json.dumps({
                'batchQuantity': 8, 'actualBatchTimeHours': 6, 'actualUnitTimeHours': 999,
                'oneCExpression': '4.4*75*30', 'employeeName': 'Иванов',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        entry = WindingEntry.objects.get(pk=entry_id)
        self.assertTrue(entry.production_confirmed)
        self.assertEqual(entry.actual_unit_time_hours, 0.75)
        # The expression survives confirmation so ✎ can bring it back.
        self.assertEqual((entry.one_c_expression, entry.one_c_hours), ('4.4*75*30', 2.75))
        self.assertEqual(entry.employee_name, 'Иванов')

        rejected = self.client.post(
            reverse('calculator:entry_production', args=[entry_id]),
            data=json.dumps({
                'batchQuantity': 8, 'actualBatchTimeHours': 6, 'oneCExpression': 'os.system("x")',
            }),
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('oneCExpression', rejected.json()['errors'])

    def test_export_covers_unconfirmed_rows_and_an_empty_journal(self):
        self.client.force_login(self.user)

        def sheet():
            response = self.client.get(reverse('calculator:export'))
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(response.content)) as workbook:
                return workbook.read('xl/worksheets/sheet1.xml').decode('utf-8')

        # No rows at all: a valid workbook carrying only the header.
        empty = sheet()
        self.assertIn('<t>δ, мм</t>', empty)
        self.assertIn('<t>Сотрудник</t>', empty)
        self.assertEqual(empty.count('<row '), 1)

        # An unconfirmed row with no production data still exports, and its
        # missing numbers stay genuinely empty cells.
        self._create()
        exported = sheet()
        self.assertEqual(exported.count('<row '), 2)
        self.assertIn('<t>Нет</t>', exported)
        self.assertIn('<c r="I2"/>', exported)
        self.assertIn('<c r="M2"/>', exported)
        self.assertNotIn('None', exported)

        # «1С, ч» exports the converted hours, not the seconds that were typed.
        WindingEntry.objects.update(one_c_expression='4.4*75*30', one_c_hours=2.75)
        self.assertIn('<c r="L2"><v>2.75</v></c>', sheet())
