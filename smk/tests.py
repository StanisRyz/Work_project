"""The three rules the СМК module must not lose.

Deliberately small: everything else here is already covered where it lives —
task completion by `tasks.tests`, the assignee/department pairing by the
protocol editor's own tests. What is new, and therefore tested, is who may
create an СМК record, that a measure really becomes a `Task`, and that the
task reaches the common registry with its СМК source named.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from tasks.models import Task

from .models import SmkSource
from .permissions import can_create_smk_task
from .services import SmkWorkflowError, create_smk_source


class SmkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The migration already seeded «Отдел СМК»; reuse it rather than
        # creating a second row with the same unique code.
        cls.department = Department.objects.get(code='SMK')
        cls.smk = cls._user('smk_user', UserProfile.Role.SMK)
        cls.manager = cls._user('manager_user', UserProfile.Role.MANAGER)
        cls.admin = cls._user('admin_user', UserProfile.Role.ADMIN)
        cls.employee = cls._user('otk_user', UserProfile.Role.OTK)

    @classmethod
    def _user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.department = cls.department
        user.userprofile.save()
        return user

    def _actions(self, assignees=None):
        return [
            {
                'text': 'Провести обучение персонала',
                'department': self.department,
                'due_date': timezone.localdate() + timedelta(days=7),
                'assignees': list(assignees or [self.employee]),
            }
        ]

    # ------------------------------------------------------------ permissions

    def test_only_smk_manager_and_admin_may_create(self):
        for user in (self.smk, self.manager, self.admin):
            with self.subTest(user=user.username):
                self.assertTrue(can_create_smk_task(user))
        self.assertFalse(can_create_smk_task(self.employee))

    def test_unauthorized_user_cannot_create_smk_task(self):
        """Refused by the service, not only by the view that calls it."""
        with self.assertRaises(SmkWorkflowError):
            create_smk_source(
                origin=SmkSource.Origin.INTERNAL_AUDIT,
                non_conformities=['Несоответствие'],
                actions=self._actions(),
                created_by=self.employee,
            )
        self.assertFalse(SmkSource.objects.exists())
        # And the form page is not even reachable for them.
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('smk:create')).status_code, 404)

    def test_smk_user_skips_the_task_type_choice(self):
        """One kind of task is not a choice; a manager still gets the menu."""
        self.client.force_login(self.smk)
        self.assertRedirects(self.client.get(reverse('tasks:create')), reverse('smk:create'))
        self.client.force_login(self.manager)
        response = self.client.get(reverse('tasks:create'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('smk:create'))

    # ----------------------------------------------------------- task creation

    def test_each_corrective_action_creates_one_linked_task(self):
        source = create_smk_source(
            origin=SmkSource.Origin.EXTERNAL_AUDIT,
            non_conformities=['Первое', 'Второе'],
            actions=self._actions([self.employee, self.smk]),
            created_by=self.smk,
        )
        self.assertEqual(source.non_conformities.count(), 2)
        action = source.actions.get()
        task = Task.objects.get(smk_action=action)
        self.assertEqual(task.source_type, Task.SourceType.SMK)
        self.assertEqual(task.smk_source_id, source.pk)
        self.assertEqual(task.task_text, action.task_text)
        self.assertEqual(task.due_date, action.due_date)
        self.assertEqual(task.department_id, self.department.pk)
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        self.assertCountEqual(
            task.assignees.values_list('user_id', flat=True),
            [self.employee.pk, self.smk.pk],
        )

    # -------------------------------------------------------------- visibility

    def test_smk_task_appears_in_the_task_registry(self):
        source = create_smk_source(
            origin=SmkSource.Origin.INTERNAL_AUDIT,
            non_conformities=['Несоответствие'],
            actions=self._actions(),
            created_by=self.smk,
        )
        task = Task.objects.get(smk_source=source)
        self.client.force_login(self.employee)
        response = self.client.get(reverse('tasks:list'), {'tab': 'my'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('tasks:detail', args=[task.pk]))
        # The registry names the source «СМК» and links to the record itself.
        self.assertContains(response, source.label)
        self.assertContains(response, reverse('smk:detail', args=[source.pk]))
