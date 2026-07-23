from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActRootAnalysis
from references.models import ActStatus, DefectType, Operation, TaskStatus

from .models import Task


class TaskViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(code='TO', name='ТО')
        cls.other_department = Department.objects.create(code='KO', name='КО')
        cls.status_archived = ActStatus.objects.get(code='ARCHIVED')
        cls.task_status = TaskStatus.objects.get(code='NEW')
        cls.operation = Operation.objects.create(code='OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='DEF', name='Дефект')
        cls.employee = cls._user('employee', UserProfile.Role.TO, cls.department)
        cls.other_employee = cls._user('other', UserProfile.Role.TO, cls.department)
        cls.manager = cls._user('manager', UserProfile.Role.MANAGER, cls.department)
        cls.creator = cls._user('otk', UserProfile.Role.OTK, cls.other_department)
        cls.act = Act.objects.create(
            created_by=cls.creator, party_number='P-1', nomenclature='Изделие', operation=cls.operation,
            defect_type=cls.defect_type, status=cls.status_archived, description='Дефект',
        )

    @classmethod
    def _user(cls, username, role, department):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.department = department
        user.userprofile.save()
        return user

    def _task(self, responsible, due_date):
        root = ActRootAnalysis.objects.create(act=self.act, root_cause=f'Причина {ActRootAnalysis.objects.count()}')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment=f'Мероприятие {root.pk}', department=self.department,
            responsible=responsible, due_date=due_date,
        )
        return Task.objects.create(
            source_action=action, act=self.act, root_analysis=root, task_text=action.comment,
            department=self.department, responsible=responsible, due_date=due_date,
            created_by=self.creator, status=self.task_status,
        )

    def test_employee_sees_only_own_tasks_and_overdue_first(self):
        future = self._task(self.employee, timezone.localdate() + timedelta(days=3))
        overdue = self._task(self.employee, timezone.localdate() - timedelta(days=1))
        hidden = self._task(self.other_employee, timezone.localdate())
        self.client.force_login(self.employee)

        response = self.client.get(reverse('tasks:list'))

        self.assertContains(response, future.task_text)
        self.assertContains(response, overdue.task_text)
        self.assertNotContains(response, hidden.task_text)
        self.assertEqual(list(response.context['tasks'])[0], overdue)
        self.assertContains(response, 'task-row--overdue')

    def test_manager_can_open_every_task_and_employee_cannot_open_other_task(self):
        own_task = self._task(self.employee, timezone.localdate())
        other_task = self._task(self.other_employee, timezone.localdate())
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('tasks:detail', args=[other_task.pk])).status_code, 404)

        self.client.force_login(self.manager)
        response = self.client.get(reverse('tasks:detail', args=[own_task.pk]))
        self.assertContains(response, self.act.number)

    def test_administrator_sees_every_task(self):
        first_task = self._task(self.employee, timezone.localdate())
        second_task = self._task(self.other_employee, timezone.localdate())
        administrator = User.objects.create_superuser(username='admin_user', password='demo12345')
        self.client.force_login(administrator)

        response = self.client.get(reverse('tasks:list'))

        self.assertContains(response, first_task.task_text)
        self.assertContains(response, second_task.task_text)
