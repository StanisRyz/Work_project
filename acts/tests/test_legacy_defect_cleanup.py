"""The safety net protecting acts that have no `ActDefect` of their own."""

from importlib import import_module
from io import StringIO

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from acts.models import Act, ActDefect
from references.models import ActStatus, DefectType


# The migration module cannot be imported by name — it starts with a digit.
refuse_legacy_only_acts = import_module(
    'acts.migrations.0024_remove_act_defect_summary'
).refuse_legacy_only_acts


class LegacyOnlyActGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        cls.defect_type = DefectType.objects.create(code='CLEAN_DEF', name='Дефект')
        cls.user = User.objects.create_user(username='cleanup_otk', password='demo12345')

    def _create_act(self, number):
        return Act.objects.create(
            number=number,
            created_by=self.user,
            nomenclature='Катушка',
            status=self.status,
        )

    def test_the_audit_reports_legacy_only_acts_and_clears_the_rest(self):
        legacy_only = self._create_act('АОК-2026-00001')
        with_defect = self._create_act('АОК-2026-00002')
        ActDefect.objects.create(
            act=with_defect,
            workshop=ActDefect.Workshop.MP_SHOP,
            defect_type=self.defect_type,
            detected_at=timezone.localdate(),
        )

        output = StringIO()
        call_command('audit_legacy_act_defects', stdout=output)
        reported = output.getvalue()

        self.assertIn('Всего актов: 2', reported)
        self.assertIn('Актов с дефектами: 1', reported)
        self.assertIn('Актов без дефектов: 1', reported)
        self.assertIn(str(legacy_only.pk), reported)

        # Once the last legacy-only act is gone the audit reports it is safe.
        legacy_only.delete()
        output = StringIO()
        call_command('audit_legacy_act_defects', stdout=output)
        self.assertIn('Актов без дефектов нет', output.getvalue())

    def test_the_migration_guard_refuses_while_a_legacy_only_act_exists(self):
        legacy_only = self._create_act('АОК-2026-00003')

        with self.assertRaises(RuntimeError) as refusal:
            refuse_legacy_only_acts(apps, None)
        self.assertIn(str(legacy_only.pk), str(refusal.exception))
        self.assertIn('audit_legacy_act_defects', str(refusal.exception))

        ActDefect.objects.create(
            act=legacy_only,
            workshop=ActDefect.Workshop.MP_SHOP,
            defect_type=self.defect_type,
            detected_at=timezone.localdate(),
        )
        # With every act covered the guard lets the schema change through.
        self.assertIsNone(refuse_legacy_only_acts(apps, None))
