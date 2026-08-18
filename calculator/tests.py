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

from accounts.models import Department, UserProfile
from realtime.testing import capture_realtime_events

from .expressions import OneCExpressionError, evaluate_one_c
from .models import EntrySource, WindingEntry
from .permissions import can_manage_workup

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


def make_user(username, role=UserProfile.Role.PDO):
    """A user with an active profile; ПДО unless another role is asked for.

    `accounts.signals.ensure_user_profile` already creates the profile, so
    this only sets the role on it.
    """
    user = User.objects.create_user(username=username, password='demo12345')
    UserProfile.objects.filter(user=user).update(role=role, is_active=True)
    # The signal left the freshly created profile cached on the instance; drop
    # it so a direct permission call reads the role that was just written.
    user.refresh_from_db()
    return user


class CalculatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Writing to «Проработка» is a ПДО action, so the fixtures that write
        # carry the role.
        cls.user = make_user('calc')
        cls.other = make_user('calc2')

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
        self.assertIn('<c r="J2"/>', exported)
        self.assertIn('<c r="N2"/>', exported)
        self.assertNotIn('None', exported)

        # «1С, ч» exports the converted hours, not the seconds that were typed.
        WindingEntry.objects.update(one_c_expression='4.4*75*30', one_c_hours=2.75)
        self.assertIn('<c r="M2"><v>2.75</v></c>', sheet())


class WorkupOwnershipTests(TestCase):
    """«Проработка» is read by everyone and changed by ПДО, admin or superuser."""

    @classmethod
    def setUpTestData(cls):
        cls.pdo = make_user('pdo_user')
        cls.admin = make_user('admin_user', role=UserProfile.Role.ADMIN)
        # The administrative fallback: a genuine superuser, without the role.
        cls.root = make_user('root_user', role=UserProfile.Role.OTK)
        User.objects.filter(pk=cls.root.pk).update(is_superuser=True, is_staff=True)
        cls.root.refresh_from_db()
        # The read-only roles: none of them owns the journal.
        cls.otk = make_user('otk_user', role=UserProfile.Role.OTK)
        cls.ko = make_user('ko_user', role=UserProfile.Role.KO)
        cls.to = make_user('to_user', role=UserProfile.Role.TO)
        cls.manager = make_user('manager_user', role=UserProfile.Role.MANAGER)

    def _json(self, url, user, payload=None):
        self.client.force_login(user)
        return self.client.post(
            url, data=json.dumps(payload or {}), content_type='application/json',
        )

    def test_can_manage_workup_is_pdo_admin_or_superuser_only(self):
        for user in (self.pdo, self.admin, self.root):
            self.assertTrue(can_manage_workup(user), user.username)
        for user in (self.otk, self.ko, self.to, self.manager):
            self.assertFalse(can_manage_workup(user), user.username)

        # A ПДО *department* is organisational and still grants nothing, and an
        # inactive profile keeps granting no role.
        department = Department.objects.get_or_create(
            code='PDO', defaults={'name': 'Планово-диспетчерская служба'},
        )[0]
        UserProfile.objects.filter(user=self.otk).update(department=department)
        self.otk.refresh_from_db()
        self.assertFalse(can_manage_workup(self.otk))

        UserProfile.objects.filter(user__in=[self.pdo.pk, self.admin.pk]).update(is_active=False)
        for user in (self.pdo, self.admin):
            user.refresh_from_db()
            self.assertFalse(can_manage_workup(user), user.username)

    def test_admin_role_runs_the_same_mutation_paths_as_pdo(self):
        created = self._json(reverse('calculator:entry_create'), self.admin, ENTRY)
        self.assertEqual(created.status_code, 201)
        entry_id = created.json()['entry']['id']

        confirmed = self._json(
            reverse('calculator:entry_production', args=[entry_id]), self.admin,
            {'batchQuantity': 4, 'actualBatchTimeHours': 2},
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(
            self._json(reverse('calculator:entry_delete', args=[entry_id]), self.admin).status_code,
            200,
        )
        self.assertEqual(WindingEntry.objects.count(), 0)

    def test_mutations_require_pdo_while_reading_stays_open_to_everyone(self):
        created = self._json(reverse('calculator:entry_create'), self.pdo, ENTRY)
        self.assertEqual(created.status_code, 201)
        entry_id = created.json()['entry']['id']

        # Same signature, same row: the ПДО path keeps the existing
        # deduplication exactly as it was.
        repeated = self._json(reverse('calculator:entry_create'), self.pdo, ENTRY)
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.json()['created'])
        self.assertEqual(WindingEntry.objects.count(), 1)

        mutations = (
            (reverse('calculator:entry_create'), {**ENTRY, 'tapeThicknessMm': 0.23}),
            (reverse('calculator:entry_manual_create'), ENTRY),
            (reverse('calculator:entry_production', args=[entry_id]),
             {'batchQuantity': 4, 'actualBatchTimeHours': 2}),
            (reverse('calculator:entry_production_unlock', args=[entry_id]), {}),
            (reverse('calculator:entry_delete', args=[entry_id]), {}),
        )
        for user in (self.otk, self.ko, self.to, self.manager):
            for url, payload in mutations:
                response = self._json(url, user, payload)
                self.assertEqual(response.status_code, 403, url)
                self.assertIn('detail', response.json())
        # Refused requests changed nothing at all.
        self.assertEqual(WindingEntry.objects.count(), 1)
        self.assertFalse(WindingEntry.objects.get(pk=entry_id).production_confirmed)

        # Both may still read the journal, its page and the export.
        for user in (self.pdo, self.otk):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('calculator:page')).status_code, 200)
            listed = self.client.get(reverse('calculator:entry_list'))
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(len(listed.json()['entries']), 1)
            self.assertEqual(self.client.get(reverse('calculator:export')).status_code, 200)

        # ПДО runs the whole production cycle: manual row, confirm, unlock.
        self.assertEqual(
            self._json(reverse('calculator:entry_manual_create'), self.pdo, ENTRY).status_code, 201,
        )
        confirmed = self._json(
            reverse('calculator:entry_production', args=[entry_id]), self.pdo,
            {'batchQuantity': 4, 'actualBatchTimeHours': 2},
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(WindingEntry.objects.get(pk=entry_id).actual_unit_time_hours, 0.5)
        self.assertEqual(
            self._json(reverse('calculator:entry_production_unlock', args=[entry_id]), self.pdo).status_code,
            200,
        )

    def test_deletion_removes_one_row_and_the_export_carries_the_winding_speed(self):
        first = self._json(reverse('calculator:entry_create'), self.pdo, ENTRY).json()['entry']['id']
        second = self._json(
            reverse('calculator:entry_create'), self.pdo, {**ENTRY, 'tapeThicknessMm': 0.23},
        ).json()['entry']['id']

        # «ВН, с/мм» is the stored winding speed, between «Калибровка» and «КС».
        self.client.force_login(self.pdo)
        response = self.client.get(reverse('calculator:export'))
        with zipfile.ZipFile(io.BytesIO(response.content)) as workbook:
            sheet = workbook.read('xl/worksheets/sheet1.xml').decode('utf-8')
        self.assertIn('<t>ВН, с/мм</t>', sheet)
        self.assertIn('<c r="G2"><v>4.4</v></c>', sheet)

        deleted = self._json(reverse('calculator:entry_delete', args=[first]), self.pdo)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()['deleted'])
        # Only the named primary key went; the second case is untouched.
        self.assertEqual(list(WindingEntry.objects.values_list('pk', flat=True)), [second])
        self.assertEqual(
            self._json(reverse('calculator:entry_delete', args=[first]), self.pdo).status_code, 404,
        )


class WorkupRealtimeTests(TestCase):
    """«Проработка» announces every persisted change, and only those."""

    @classmethod
    def setUpTestData(cls):
        cls.pdo = make_user('rt_pdo')
        # A reader who never touches the journal: the events still reach them,
        # because everyone authenticated may open «Проработку».
        cls.reader = make_user('rt_reader', role=UserProfile.Role.OTK)
        cls.inactive = make_user('rt_inactive', role=UserProfile.Role.OTK)
        UserProfile.objects.filter(user=cls.inactive).update(is_active=False)

    def _json(self, url, payload=None):
        self.client.force_login(self.pdo)
        return self.client.post(
            url, data=json.dumps(payload or {}), content_type='application/json',
        )

    def test_every_persisted_change_publishes_one_invalidation_event(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                entry_id = self._json(reverse('calculator:entry_create'), ENTRY).json()['entry']['id']
            with self.captureOnCommitCallbacks(execute=True):
                self._json(
                    reverse('calculator:entry_production', args=[entry_id]),
                    {'batchQuantity': 4, 'actualBatchTimeHours': 2},
                )
            with self.captureOnCommitCallbacks(execute=True):
                self._json(reverse('calculator:entry_production_unlock', args=[entry_id]))
            with self.captureOnCommitCallbacks(execute=True):
                self._json(reverse('calculator:entry_delete', args=[entry_id]))

            types = [event.event_type.value for event in publisher.events]
            self.assertEqual(
                types, ['workup.created', 'workup.updated', 'workup.updated', 'workup.deleted'],
            )
            # The row identifier and technical metadata only — never geometry,
            # production time, the 1С expression or the employee name.
            for event in publisher.events:
                self.assertEqual(event.resource_type, 'workup')
                self.assertEqual(event.resource_id, entry_id)
                self.assertLessEqual(set(event.data), {'source', 'change', 'production_confirmed'})

    def test_a_repeated_calculation_announces_nothing(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self._json(reverse('calculator:entry_create'), ENTRY)
            publisher.clear()

            # The same case again: `get_or_create` hands back the existing row,
            # so nothing changed and nothing may be announced.
            with self.captureOnCommitCallbacks(execute=True):
                repeated = self._json(reverse('calculator:entry_create'), ENTRY)

            self.assertEqual(repeated.status_code, 200)
            self.assertFalse(repeated.json()['created'])
            self.assertEqual(publisher.events, [])

    def test_the_audience_is_every_active_account_as_per_user_targets(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self._json(reverse('calculator:entry_manual_create'), ENTRY)

            targets = publisher.published[0][1]
            self.assertEqual({target.kind for target in targets}, {'user'})
            identifiers = {target.identifier for target in targets}
            self.assertIn(self.pdo.pk, identifiers)
            self.assertIn(self.reader.pk, identifiers)
            self.assertNotIn(self.inactive.pk, identifiers)
