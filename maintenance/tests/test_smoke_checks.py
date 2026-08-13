import unittest
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings

from accounts.models import Department, UserProfile
from acts.models import (
    Act,
    ActComment,
    ActCorrectiveAction,
    ActDefect,
    ActHistoryEvent,
    ActRootAnalysis,
)
from maintenance import database_transfer as dt
from maintenance import smoke_checks
from notifications.models import Notification
from references.models import ActStatus, DefectType, Operation, TaskStatus
from tasks.models import Task


class SmokeCheckGuardTests(TestCase):
    def test_smoke_checks_are_refused_on_a_non_postgresql_backend(self):
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with self.assertRaisesMessage(dt.TransferError, 'только на PostgreSQL'):
                smoke_checks.run_smoke_checks()

    def test_smoke_checks_are_refused_while_email_is_enabled(self):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(EMAIL_NOTIFICATIONS_ENABLED=True):
                with self.assertRaisesMessage(dt.TransferError, 'EMAIL_NOTIFICATIONS_ENABLED'):
                    smoke_checks.run_smoke_checks()


class SmokeCheckDataMixin:
    """Minimum reference data a migrated database is expected to contain."""

    def setUpSmokeData(self):
        ActStatus.objects.update_or_create(code='CREATED_OTK', defaults={'name': 'Создан ОТК'})
        TaskStatus.objects.update_or_create(
            code='IN_PROGRESS', defaults={'name': 'В работе', 'is_final': False}
        )
        TaskStatus.objects.update_or_create(
            code='COMPLETED', defaults={'name': 'Выполнено', 'is_final': True}
        )
        self.operation = Operation.objects.create(code='SMOKE_OP', name='Операция')
        self.defect_type = DefectType.objects.create(code='SMOKE_DEFECT', name='Дефект')
        self.department = Department.objects.create(code='SMOKE_DEP', name='Отдел')
        self.user = User.objects.create_user(username='smoke_existing', password='demo12345')
        profile = UserProfile.objects.get(user=self.user)
        profile.department = self.department
        profile.save(update_fields=['department'])
        self.act = Act.objects.create(
            created_by=self.user,
            party_number='SMOKE-EXISTING',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=ActStatus.objects.get(code='CREATED_OTK'),
            description='Существующий акт',
        )


class ReadOnlySmokeCheckTests(SmokeCheckDataMixin, TestCase):
    def setUp(self):
        self.setUpSmokeData()

    def test_read_only_checks_pass_on_consistent_data(self):
        report = smoke_checks.run_read_only_checks()

        self.assertTrue(report['ok'], report['failures'])
        names = {check['name'] for check in report['checks']}
        self.assertEqual(
            names,
            {
                'users_and_profiles',
                'acts_registry',
                'defects_history_comments',
                'to_analysis',
                'tasks',
                'notifications',
                'permissions',
                'attachment_files',
                'view_queries',
            },
        )

    def test_read_only_checks_report_a_missing_attachment_file(self):
        from acts.models import ActAttachment

        ActAttachment.objects.create(
            act=self.act,
            uploaded_by=self.user,
            file='acts/attachments/1/absent.txt',
            original_name='absent.txt',
            file_size=4,
        )

        report = smoke_checks.run_read_only_checks()

        self.assertFalse(report['ok'])
        self.assertIn('attachment_files', report['failures'])

    def test_read_only_checks_report_a_user_without_a_profile(self):
        UserProfile.objects.filter(user=self.user).delete()

        report = smoke_checks.run_read_only_checks()

        self.assertFalse(report['ok'])
        self.assertIn('users_and_profiles', report['failures'])


class WriteSmokeCheckTests(SmokeCheckDataMixin, TestCase):
    """The write suite must exercise the full round trip and leave nothing."""

    def setUp(self):
        self.setUpSmokeData()

    def test_write_checks_pass_and_leave_no_data_behind(self):
        acts_before = Act.objects.count()
        users_before = User.objects.count()

        report = smoke_checks.run_write_checks()

        self.assertTrue(report['ok'], [check for check in report['checks'] if check['status'] != 'ok'])
        performed = {check['name'] for check in report['checks']}
        for expected in (
            'create_user',
            'user_profile_created',
            'create_act',
            'create_defect',
            'add_comment',
            'create_history',
            'create_corrective_action',
            'create_task',
            'create_notification',
            'complete_task',
            'constraints',
            'relations',
            'rollback',
        ):
            self.assertIn(expected, performed)

        self.assertEqual(Act.objects.count(), acts_before)
        self.assertEqual(User.objects.count(), users_before)
        self.assertFalse(User.objects.filter(username=smoke_checks.SMOKE_USERNAME).exists())
        self.assertFalse(
            UserProfile.objects.filter(user__username=smoke_checks.SMOKE_USERNAME).exists()
        )
        self.assertFalse(Department.objects.filter(code=smoke_checks.SMOKE_DEPARTMENT_CODE).exists())
        for model, field in (
            (Act, 'nomenclature'),
            (ActDefect, 'description'),
            (ActComment, 'text'),
            (ActHistoryEvent, 'message'),
            (ActRootAnalysis, 'root_cause'),
            (ActCorrectiveAction, 'comment'),
            (Task, 'task_text'),
            (Notification, 'title'),
        ):
            self.assertFalse(
                model.objects.filter(**{field: smoke_checks.SMOKE_MARKER}).exists(),
                f'{model.__name__} сохранил тестовые данные.',
            )

    def test_a_failing_step_is_reported_and_still_rolled_back(self):
        TaskStatus.objects.filter(code='IN_PROGRESS').delete()

        report = smoke_checks.run_write_checks()

        self.assertFalse(report['ok'])
        self.assertIn('create_task', report['failures'])
        self.assertFalse(Act.objects.filter(nomenclature=smoke_checks.SMOKE_MARKER).exists())
        self.assertFalse(User.objects.filter(username=smoke_checks.SMOKE_USERNAME).exists())

    def test_no_email_is_sent_during_the_write_suite(self):
        from django.core import mail

        with override_settings(
            EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
            EMAIL_NOTIFICATIONS_ENABLED=False,
        ):
            mail.outbox = []
            smoke_checks.run_write_checks()

            self.assertEqual(mail.outbox, [])


@unittest.skipUnless(
    connection.vendor == 'postgresql',
    'Полный прогон smoke-проверок выполняется только на PostgreSQL.',
)
class FullSmokeCheckTests(SmokeCheckDataMixin, TestCase):
    def setUp(self):
        self.setUpSmokeData()

    def test_the_entry_point_runs_both_suites(self):
        with override_settings(EMAIL_NOTIFICATIONS_ENABLED=False):
            report = smoke_checks.run_smoke_checks()

        self.assertTrue(report['ok'], report)
        self.assertTrue(report['read']['checks'])
        self.assertTrue(report['write']['checks'])
        self.assertFalse(report['email_notifications_enabled'])
