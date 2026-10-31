"""An existing installation must gain РЛ types through migrate, without seeding."""

from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from acts.forms import ActDefectForm
from acts.workshops import RL_PROFILE
from references.models import DefectType


EXPECTED_RL_TYPES = {
    'SIZE_NONCONFORMITY': 'Несоответствие размеров',
    'BURRS': 'Заусенцы',
    'MECHANICAL_DAMAGE': 'Механическое повреждение',
    'ROD_YOKE_MISALIGNMENT': 'Перекос стержней и ярм',
    'WAVE': 'Волна',
    'COATING_DELAMINATION': 'Отслоение покрытия',
    'RUST': 'Ржавчина',
    'PAINT_DEFECT': 'Дефект покраски',
    'MAGNETIC_PROPERTIES': 'Магнитные свойства',
    'OTHER': 'Другое',
}
NEW_CODES = set(EXPECTED_RL_TYPES) - {'SIZE_NONCONFORMITY', 'OTHER'}


class RlReferenceMigrationTests(TestCase):
    def test_migration_adds_the_eight_new_types_without_bootstrap(self):
        # Test database setup runs migrate, but never seed_references.
        self.assertEqual(
            dict(DefectType.objects.filter(code__in=NEW_CODES, is_active=True)
                 .values_list('code', 'name')),
            {code: EXPECTED_RL_TYPES[code] for code in NEW_CODES},
        )

    def test_repeated_migration_preserves_existing_ids_names_and_activation(self):
        migration = import_module('references.migrations.0006_rl_defect_types')
        DefectType.objects.filter(code='BURRS').update(name='Заусенцы (уточнено)', is_active=False)
        existing = dict(DefectType.objects.filter(code__in=NEW_CODES).values_list('code', 'pk'))
        DefectType.objects.filter(code='RUST').delete()
        for _repeat in range(2):
            migration.seed_rl_defect_types(apps, SimpleNamespace(connection=connection))
        burrs = DefectType.objects.get(code='BURRS')
        self.assertEqual((burrs.pk, burrs.name, burrs.is_active),
                         (existing['BURRS'], 'Заусенцы (уточнено)', False))
        self.assertTrue(DefectType.objects.get(code='RUST').is_active)
        self.assertEqual(DefectType.objects.filter(code__in=NEW_CODES).count(), 8)


class RlReferenceUpgradeTests(TransactionTestCase):
    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrading_a_pre_rl_dictionary_makes_all_ten_types_available(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('references', '0005_deviation_reasons')])
        # Reverse deliberately preserves references that acts might use. A
        # pre-release dictionary, however, has none of these eight new rows.
        DefectType.objects.filter(code__in=NEW_CODES).delete()
        existing = {}
        for code in ('SIZE_NONCONFORMITY', 'OTHER'):
            record, _created = DefectType.objects.get_or_create(
                code=code, defaults={'name': EXPECTED_RL_TYPES[code], 'is_active': True},
            )
            existing[code] = record.pk
        # This reproduces the reported one-option list under the old РЛ filter.
        old_allowed = set(EXPECTED_RL_TYPES) - {'OTHER'}
        self.assertEqual(
            list(DefectType.objects.filter(code__in=old_allowed, is_active=True)
                 .values_list('code', flat=True)),
            ['SIZE_NONCONFORMITY'],
        )

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

        self.assertEqual(set(RL_PROFILE.defect_type_codes), set(EXPECTED_RL_TYPES))
        choices = ActDefectForm().fields['defect_type'].queryset.filter(
            code__in=RL_PROFILE.defect_type_codes,
        )
        self.assertEqual(dict(choices.values_list('code', 'name')), EXPECTED_RL_TYPES)
        self.assertEqual(dict(DefectType.objects.filter(code__in=existing)
                              .values_list('code', 'pk')), existing)
