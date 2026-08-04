"""Shared fixture for the real-time integration tests."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActCorrectiveActionAssignee, ActRootAnalysis
from references.models import ActStatus, DefectType, Operation, TaskStatus


class RealtimeFixtureMixin:
    """Roles, statuses and one act, mirroring the acts workflow test setup."""

    @classmethod
    def setUpRealtimeData(cls):
        cls.status_created, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        cls.status_ko, _ = ActStatus.objects.get_or_create(
            code='KO_REVIEW', defaults={'name': 'На рассмотрении КО'}
        )
        cls.status_to, _ = ActStatus.objects.get_or_create(
            code='TO_ANALYSIS', defaults={'name': 'На анализе ТО'}
        )
        # get_or_create, not get: a TransactionTestCase truncates the tables
        # between tests, which also removes the migration-seeded statuses.
        cls.status_otk_review, _ = ActStatus.objects.get_or_create(
            code='OTK_REVIEW', defaults={'name': 'Проверка ОТК'}
        )
        cls.status_archived, _ = ActStatus.objects.get_or_create(
            code='ARCHIVED', defaults={'name': 'Архивирован', 'is_final': True}
        )
        TaskStatus.objects.get_or_create(
            code='IN_PROGRESS', defaults={'name': 'В работе', 'is_final': False}
        )
        TaskStatus.objects.get_or_create(
            code='COMPLETED', defaults={'name': 'Выполнено', 'is_final': True}
        )

        # Codes the act create/edit form actually offers, so the same fixture
        # can drive both the services and the HTTP form.
        cls.operation, _ = Operation.objects.get_or_create(
            code='OPERATIONAL_CONTROL', defaults={'name': 'Операционный контроль'}
        )
        cls.defect_type, _ = DefectType.objects.get_or_create(
            code='SIZE_NONCONFORMITY', defaults={'name': 'Несоответствие размеров'}
        )
        cls.department = Department.objects.create(code='RT_DEP', name='Технологический отдел')

        cls.otk_user = cls.make_user('rt_otk', UserProfile.Role.OTK)
        cls.ko_user = cls.make_user('rt_ko', UserProfile.Role.KO)
        cls.to_user = cls.make_user('rt_to', UserProfile.Role.TO)
        # Never a participant of the act below and never in a notified role for
        # its transitions: used to prove outsiders do not become recipients.
        cls.outsider = cls.make_user('rt_outsider', UserProfile.Role.OTK)

    @classmethod
    def make_user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.department = getattr(cls, 'department', None)
        profile.save()
        return user

    def make_act(self, status=None, created_by=None):
        return Act.objects.create(
            created_by=created_by or self.otk_user,
            party_number='P-001',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=status or self.status_created,
            description='Описание дефекта',
        )

    def make_analysis(self, act, assignees=None):
        """Attach one root cause with one corrective action and its assignees."""
        root = ActRootAnalysis.objects.create(act=act, root_cause='Корневая причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment='Корректирующее мероприятие',
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=7),
        )
        for assignee in assignees or [self.to_user]:
            ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=assignee)
        return root, action


def target_keys(targets):
    return sorted(target.key for target in targets)
