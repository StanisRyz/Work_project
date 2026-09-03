"""Regression cover for whole-row navigation in the object tables.

Every registry and «Связанные мероприятия» table opens its object when the row
is clicked anywhere. The behaviour itself is one delegated handler in
`static/js/clickable_rows.js`; what a Django test can assert — and what would
silently regress — is the contract the templates hold up for it:

* the row carries `data-row-url`, and it points at that row's own object, with
  the same query the number link carries;
* the number stays a real link, so keyboard use and «open in new tab» survive;
* a row with nothing to open carries no URL at all, and so gets neither the
  pointer cursor nor the hover the `[data-row-url]` rules give the others;
* nothing meant as a template comment reaches the reader — Django's `{#` form
  is single-line only, and one written across several lines is not a comment at
  all but text, rendered into the page for the user to read.

The акт registry and the задачи registry stand for the four registries — they
are the two shapes: a row of plain cells, and a row that holds a second link
of its own.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActCorrectiveActionAssignee, ActRootAnalysis
from references.models import ActStatus, TaskStatus
from tasks.models import Task, TaskAssignee


class ClickableRowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(code='ROW_TO', name='Технологический отдел')
        cls.employee = cls._employee('row_employee', UserProfile.Role.TO)
        cls.otk = cls._employee('row_otk', UserProfile.Role.OTK)
        cls.act = Act.objects.create(
            number='АОК-2026-00901',
            created_by=cls.otk,
            nomenclature='Изделие',
            act_type=Act.Type.OPERATIONAL_CONTROL,
            status=ActStatus.objects.get(code='ARCHIVED'),
        )
        root = ActRootAnalysis.objects.create(act=cls.act, root_cause='Причина')
        due = timezone.localdate() + timedelta(days=7)
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Мероприятие', department=cls.department, due_date=due,
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=cls.employee)
        cls.task = Task.objects.create(
            source_action=action, act=cls.act, root_analysis=root, task_text=action.comment,
            department=cls.department, due_date=due, created_by=cls.otk,
            status=TaskStatus.objects.get(code='IN_PROGRESS'),
        )
        TaskAssignee.objects.create(task=cls.task, user=cls.employee)

    @classmethod
    def _employee(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.department = cls.department
        profile.save(update_fields=['role', 'department'])
        return user

    def test_act_registry_row_opens_its_act_and_keeps_the_number_link(self):
        """A row of plain cells: the whole row and the number lead to one place."""
        self.client.force_login(self.otk)

        # The архив scope: that is where a закрытый акт is listed.
        response = self.client.get(reverse('acts:list'), {'scope': 'archive'})

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        detail_url = reverse('acts:detail', args=[self.act.pk])
        self.assertIn(f'data-row-url="{detail_url}"', page)
        self.assertIn(f'<a class="table-link" href="{detail_url}">{self.act.number}</a>', page)
        # The one handler behind every such row has to be loaded at all.
        self.assertIn('js/clickable_rows.js', page)
        self.assertNotIn('{#', page)

    def test_task_registry_row_keeps_the_list_query_and_the_source_link(self):
        """A row that holds a second link: «Источник» keeps its own target.

        The row URL repeats the number's, list query included, so a click
        anywhere returns to the same tab and filters afterwards — while the
        act behind the task stays reachable from its own cell.
        """
        self.client.force_login(self.employee)

        response = self.client.get(reverse('tasks:list'), {'tab': 'my', 'sort': 'latest'})

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        task_url = reverse('tasks:detail', args=[self.task.pk])
        self.assertIn(f'data-row-url="{task_url}?tab=my&amp;sort=latest"', page)
        self.assertIn(f'href="{task_url}?tab=my&amp;sort=latest"', page)
        self.assertIn(f'href="{reverse("acts:detail", args=[self.act.pk])}"', page)
        self.assertNotIn('{#', page)

    def test_related_activity_row_opens_its_task(self):
        """«Связанные мероприятия» on the акт page: the row is the task's row."""
        self.client.force_login(self.otk)

        response = self.client.get(reverse('acts:detail', args=[self.act.pk]), {'tab': 'activities'})

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        task_url = reverse('tasks:detail', args=[self.task.pk])
        self.assertIn(f'data-related-task="{self.task.pk}" data-row-url="{task_url}"', page)
        self.assertNotIn('{#', page)
