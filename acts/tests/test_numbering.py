import importlib
from datetime import date
from unittest import mock

from django.apps import apps as global_apps
from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import UserProfile
from acts.models import Act, ActNumberSequence
from acts.services import clear_all_acts
from references.models import ActStatus, DefectType, Operation

migration_0020 = importlib.import_module('acts.migrations.0020_actnumbersequence')


class ActNumberingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        cls.operation = Operation.objects.create(code='NUM_OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='NUM_DEFECT', name='Дефект')
        cls.otk_user = User.objects.create_user(username='num_otk', password='demo12345')
        cls.otk_user.userprofile.role = UserProfile.Role.OTK
        cls.otk_user.userprofile.save(update_fields=['role'])

    def _create_act(self, **overrides):
        values = {
            'created_by': self.otk_user,
            'party_number': 'P-001',
            'nomenclature': 'Катушка',
            'operation': self.operation,
            'defect_type': self.defect_type,
            'status': self.status_created,
            'description': 'Описание',
        }
        values.update(overrides)
        return Act.objects.create(**values)

    def test_explicit_number_is_preserved_and_does_not_consume_the_sequence(self):
        act = self._create_act(number='АОК-СПЕЦ-42')

        self.assertEqual(act.number, 'АОК-СПЕЦ-42')
        self.assertFalse(ActNumberSequence.objects.exists())

    def test_generated_numbers_increment_within_the_current_year(self):
        first = self._create_act()
        second = self._create_act()

        year = date.today().year
        self.assertEqual(first.number, f'АОК-{year}-001')
        self.assertEqual(second.number, f'АОК-{year}-002')
        self.assertEqual(ActNumberSequence.objects.get(year=year).last_value, 2)

    def test_new_number_follows_the_highest_existing_value(self):
        year = date.today().year
        ActNumberSequence.objects.create(year=year, last_value=7)

        act = self._create_act()

        self.assertEqual(act.number, f'АОК-{year}-008')

    def test_year_sequences_are_independent(self):
        self.assertEqual(ActNumberSequence.allocate(2025), 1)
        self.assertEqual(ActNumberSequence.allocate(2026), 1)
        self.assertEqual(ActNumberSequence.allocate(2025), 2)
        self.assertEqual(ActNumberSequence.allocate(2026), 2)
        self.assertEqual(ActNumberSequence.allocate(2027), 1)

        self.assertEqual(ActNumberSequence.objects.get(year=2025).last_value, 2)
        self.assertEqual(ActNumberSequence.objects.get(year=2026).last_value, 2)
        self.assertEqual(ActNumberSequence.objects.get(year=2027).last_value, 1)

    def test_new_year_restarts_numbering_without_touching_the_previous_year(self):
        with mock.patch('acts.models.timezone.localdate', return_value=date(2025, 12, 31)):
            last_of_2025 = self._create_act()
        with mock.patch('acts.models.timezone.localdate', return_value=date(2026, 1, 1)):
            first_of_2026 = self._create_act()

        self.assertEqual(last_of_2025.number, 'АОК-2025-001')
        self.assertEqual(first_of_2026.number, 'АОК-2026-001')
        self.assertEqual(ActNumberSequence.objects.get(year=2025).last_value, 1)
        self.assertEqual(ActNumberSequence.objects.get(year=2026).last_value, 1)

    def test_deleting_a_single_act_does_not_rewind_the_sequence(self):
        year = date.today().year
        first = self._create_act()
        self._create_act()
        first.delete()

        self.assertEqual(ActNumberSequence.objects.get(year=year).last_value, 2)
        self.assertEqual(self._create_act().number, f'АОК-{year}-003')

    def test_full_administrative_cleanup_resets_sequences(self):
        year = date.today().year
        self._create_act()
        self._create_act()
        self.assertEqual(ActNumberSequence.objects.get(year=year).last_value, 2)

        clear_all_acts()

        self.assertFalse(ActNumberSequence.objects.exists())
        self.assertEqual(self._create_act().number, f'АОК-{year}-001')


class ActNumberSequenceMigrationTests(TestCase):
    """Covers the 0020 data migration logic against the current model state."""

    @classmethod
    def setUpTestData(cls):
        cls.status_created, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        cls.operation = Operation.objects.create(code='MIG_OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='MIG_DEFECT', name='Дефект')
        cls.otk_user = User.objects.create_user(username='mig_otk', password='demo12345')

    def _create_act(self, number):
        return Act.objects.create(
            number=number,
            created_by=self.otk_user,
            party_number='P-001',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=self.status_created,
            description='Описание',
        )

    def _run_initialisation(self):
        ActNumberSequence.objects.all().delete()
        migration_0020.initialize_sequences(global_apps, None)

    def test_initialisation_uses_the_highest_suffix_per_year(self):
        self._create_act('АОК-2025-001')
        self._create_act('АОК-2025-014')
        self._create_act('АОК-2025-009')
        self._create_act('АОК-2026-003')

        self._run_initialisation()

        self.assertEqual(ActNumberSequence.objects.get(year=2025).last_value, 14)
        self.assertEqual(ActNumberSequence.objects.get(year=2026).last_value, 3)

    def test_non_standard_historical_numbers_are_ignored(self):
        self._create_act('АОК-DEMO-001')
        self._create_act('АОК-DEMO-002')
        self._create_act('ACT/2025/77')
        self._create_act('АОК-25-4')
        self._create_act('АОК-2025-005')

        self._run_initialisation()

        self.assertEqual(list(ActNumberSequence.objects.values_list('year', flat=True)), [2025])
        self.assertEqual(ActNumberSequence.objects.get(year=2025).last_value, 5)

    def test_initialisation_does_not_modify_existing_act_numbers(self):
        legacy = self._create_act('АОК-DEMO-001')
        standard = self._create_act('АОК-2025-002')

        self._run_initialisation()

        legacy.refresh_from_db()
        standard.refresh_from_db()
        self.assertEqual(legacy.number, 'АОК-DEMO-001')
        self.assertEqual(standard.number, 'АОК-2025-002')

    def test_initialisation_without_any_standard_numbers_creates_no_rows(self):
        self._create_act('АОК-DEMO-001')

        self._run_initialisation()

        self.assertFalse(ActNumberSequence.objects.exists())

    def test_next_generated_number_continues_after_initialisation(self):
        with mock.patch('acts.models.timezone.localdate', return_value=date(2025, 6, 1)):
            self._create_act('АОК-2025-012')
            self._run_initialisation()
            follow_up = Act.objects.create(
                created_by=self.otk_user,
                party_number='P-002',
                nomenclature='Катушка',
                operation=self.operation,
                defect_type=self.defect_type,
                status=self.status_created,
                description='Описание',
            )

        self.assertEqual(follow_up.number, 'АОК-2025-013')
