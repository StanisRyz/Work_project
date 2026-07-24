from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActCorrectiveActionAssignee, ActRootAnalysis
from references.models import ActStatus, DefectType, Operation, TaskStatus

from .models import Task, TaskAssignee
from .services import TaskWorkflowError, complete_task


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

    def _task(self, responsible, due_date, extra_assignees=()):
        root = ActRootAnalysis.objects.create(act=self.act, root_cause=f'Причина {ActRootAnalysis.objects.count()}')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment=f'Мероприятие {root.pk}', department=self.department,
            due_date=due_date,
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=responsible)
        for user in extra_assignees:
            ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=user)
        task = Task.objects.create(
            source_action=action, act=self.act, root_analysis=root, task_text=action.comment,
            department=self.department, due_date=due_date,
            created_by=self.creator, status=self.task_status,
        )
        TaskAssignee.objects.create(task=task, user=responsible)
        for user in extra_assignees:
            TaskAssignee.objects.create(task=task, user=user)
        return task

    def test_employee_sees_only_own_tasks_and_overdue_first(self):
        future = self._task(self.employee, timezone.localdate() + timedelta(days=3))
        overdue = self._task(self.employee, timezone.localdate() - timedelta(days=1))
        hidden = self._task(self.other_employee, timezone.localdate())
        self.client.force_login(self.employee)

        response = self.client.get(reverse('tasks:list'))

        self.assertContains(response, str(future.pk))
        self.assertContains(response, str(overdue.pk))
        self.assertNotContains(response, reverse('tasks:detail', args=[hidden.pk]))
        self.assertEqual(list(response.context['tasks'])[0], overdue)
        self.assertContains(response, 'task-row--overdue')
        self.assertContains(response, 'По акту')
        self.assertContains(response, reverse('tasks:detail', args=[overdue.pk]))
        self.assertContains(response, reverse('acts:detail', args=[self.act.pk]))
        self.assertNotContains(response, future.task_text)
        self.assertNotContains(response, 'Исполнители</th>')
        self.assertContains(response, '№ задачи</th><th>Статус</th><th>Источник</th><th>Срок <a class="task-sort-link"')

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

        response = self.client.get(reverse('tasks:list'), {'tab': 'all'})

        self.assertContains(response, reverse('tasks:detail', args=[first_task.pk]))
        self.assertContains(response, reverse('tasks:detail', args=[second_task.pk]))

    def test_each_assignee_can_view_shared_task_and_unrelated_employee_cannot(self):
        task = self._task(self.employee, timezone.localdate(), [self.other_employee])
        self.client.force_login(self.other_employee)
        self.assertEqual(self.client.get(reverse('tasks:detail', args=[task.pk])).status_code, 200)
        unrelated = self._user('unrelated', UserProfile.Role.TO, self.department)
        self.client.force_login(unrelated)
        self.assertEqual(self.client.get(reverse('tasks:detail', args=[task.pk])).status_code, 404)

    def test_assignee_completes_shared_task_once(self):
        task = self._task(self.employee, timezone.localdate(), [self.other_employee])
        complete_task(task, self.other_employee)
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.completed_by, self.other_employee)
        self.assertIsNotNone(task.completed_at)
        with self.assertRaises(TaskWorkflowError):
            complete_task(task, self.employee)

    def test_tabs_respect_permissions_and_archive(self):
        own = self._task(self.employee, timezone.localdate())
        other = self._task(self.other_employee, timezone.localdate())
        completed = self._task(self.employee, timezone.localdate() - timedelta(days=3))
        complete_task(completed, self.employee)

        self.client.force_login(self.employee)
        my_response = self.client.get(reverse('tasks:list'))
        self.assertContains(my_response, reverse('tasks:detail', args=[own.pk]))
        self.assertNotContains(my_response, reverse('tasks:detail', args=[other.pk]))
        self.assertNotContains(my_response, reverse('tasks:detail', args=[completed.pk]))
        employee_all_response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        self.assertContains(employee_all_response, reverse('tasks:detail', args=[own.pk]))
        self.assertNotContains(employee_all_response, reverse('tasks:detail', args=[other.pk]))
        archive_response = self.client.get(reverse('tasks:list'), {'tab': 'archive'})
        self.assertContains(archive_response, reverse('tasks:detail', args=[completed.pk]))
        self.assertNotContains(archive_response, 'task-row--overdue')

        self.client.force_login(self.manager)
        all_response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        self.assertContains(all_response, reverse('tasks:detail', args=[own.pk]))
        self.assertContains(all_response, reverse('tasks:detail', args=[other.pk]))
        self.assertNotContains(all_response, reverse('tasks:detail', args=[completed.pk]))

    def test_registry_filters_combine_and_reset_preserves_tab(self):
        matching = self._task(self.employee, timezone.localdate() - timedelta(days=1))
        hidden = self._task(self.employee, timezone.localdate() + timedelta(days=4))
        self.client.force_login(self.employee)
        response = self.client.get(reverse('tasks:list'), {
            'tab': 'my', 'number': matching.pk, 'source': self.act.number,
            'status': 'act', 'due': 'overdue', 'sort': 'nearest',
        })
        self.assertContains(response, reverse('tasks:detail', args=[matching.pk]))
        self.assertNotContains(response, reverse('tasks:detail', args=[hidden.pk]))
        self.assertContains(response, '?tab=my')
        empty = self.client.get(reverse('tasks:list'), {'number': 'not-a-number'})
        self.assertContains(empty, 'Задачи не найдены')

    def test_due_date_sorting_and_links(self):
        nearest = self._task(self.employee, timezone.localdate() + timedelta(days=1))
        farthest = self._task(self.employee, timezone.localdate() + timedelta(days=5))
        overdue = self._task(self.employee, timezone.localdate() - timedelta(days=1))
        self.client.force_login(self.employee)

        default_response = self.client.get(reverse('tasks:list'))
        self.assertEqual(list(default_response.context['tasks'])[0], overdue)
        nearest_response = self.client.get(reverse('tasks:list'), {'sort': 'nearest'})
        self.assertEqual(list(nearest_response.context['tasks'])[0], overdue)
        farthest_response = self.client.get(reverse('tasks:list'), {'sort': 'farthest'})
        self.assertEqual(list(farthest_response.context['tasks'])[0], farthest)
        self.assertContains(farthest_response, reverse('tasks:detail', args=[nearest.pk]))
        self.assertContains(farthest_response, reverse('acts:detail', args=[self.act.pk]))
