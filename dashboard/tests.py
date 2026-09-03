"""The landing page: it opens, the grid respects the section permissions, and
the task block shows the user's own open work.

Deliberately three tests. The sections behind the cards and the task registry
itself are covered by their own apps' suites; what is new here is the page, the
filtering of the grid and the shortened «Мои задачи» list.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act
from references.models import ActStatus, TaskStatus
from tasks.models import Task, TaskAssignee


def _create_user(username, role):
    user = User.objects.create_user(
        username=username, password='demo12345', first_name='Иван', last_name='Иванов',
    )
    user.userprofile.role = role
    user.userprofile.save()
    return user


class DashboardPageTests(TestCase):
    def test_the_dashboard_is_the_root_page_and_opens_for_an_authenticated_user(self):
        self.client.force_login(_create_user('otk_dashboard', UserProfile.Role.OTK))

        response = self.client.get(reverse('dashboard:home'))

        self.assertEqual(reverse('dashboard:home'), '/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Добро пожаловать, Иван Иванов!')

    def test_the_grid_shows_only_the_sections_the_user_may_open(self):
        # The card descriptions, not the labels: those also appear in the
        # navigation panel every page renders.
        descriptions = (
            'Создание и просмотр актов',
            'Протоколы проверок и совещаний',
            'Процессы и документы системы качества',
            'Мои задачи и поручения',
            'Расчетные инструменты и шаблоны',
        )
        documentation = 'Нормативные и справочные документы'

        self.client.force_login(_create_user('otk_sections', UserProfile.Role.OTK))
        employee_page = self.client.get(reverse('dashboard:home'))

        for description in descriptions:
            self.assertContains(employee_page, description)
        # «Документация» is administrative: the card follows the same
        # `can_view_documents()` the library itself enforces.
        self.assertNotContains(employee_page, documentation)

        self.client.force_login(_create_user('admin_sections', UserProfile.Role.ADMIN))

        self.assertContains(self.client.get(reverse('dashboard:home')), documentation)


class DashboardTaskBlockTests(TestCase):
    def test_the_block_lists_the_users_own_open_tasks_and_nobody_elses(self):
        assignee = _create_user('otk_tasks', UserProfile.Role.OTK)
        other = _create_user('otk_other', UserProfile.Role.OTK)
        department = Department.objects.create(code='DASH', name='ПДО')
        act = Act.objects.create(
            number='АОК-2026-00128',
            nomenclature='Изделие',
            status=ActStatus.objects.get(code='ARCHIVED'),
            created_by=assignee,
        )
        task_status = TaskStatus.objects.get(code='IN_PROGRESS')
        due_date = timezone.localdate() + timedelta(days=3)

        mine = Task.objects.create(
            source_type=Task.SourceType.ACT_REJECTION, act=act,
            task_text='Подготовить ответ на замечания', department=department,
            due_date=due_date, created_by=assignee, status=task_status,
        )
        TaskAssignee.objects.create(task=mine, user=assignee)
        theirs = Task.objects.create(
            source_type=Task.SourceType.ACT_WORKFLOW, act=act,
            workflow_stage=Task.WorkflowStage.KO_REVIEW, task_text='Чужая задача',
            due_date=due_date, created_by=assignee, status=task_status,
        )
        TaskAssignee.objects.create(task=theirs, user=other)

        self.client.force_login(assignee)
        response = self.client.get(reverse('dashboard:home'))

        self.assertContains(response, 'Подготовить ответ на замечания')
        self.assertNotContains(response, 'Чужая задача')
        # The source of the row comes from `tasks.presentation`, not from a
        # second description written for this page.
        self.assertContains(response, act.number)
        # Two lines and no more, through the shared helper in `text.css`; the
        # whole задача stays on `title`.
        self.assertContains(response, 'text-clamp-2')

    def test_the_block_never_shows_more_than_five_tasks(self):
        assignee = _create_user('otk_limit', UserProfile.Role.OTK)
        department = Department.objects.create(code='LIMIT', name='ПДО')
        archived = ActStatus.objects.get(code='ARCHIVED')
        task_status = TaskStatus.objects.get(code='IN_PROGRESS')
        for index in range(7):
            act = Act.objects.create(
                number=f'АОК-2026-0020{index}', nomenclature='Изделие',
                status=archived, created_by=assignee,
            )
            task = Task.objects.create(
                source_type=Task.SourceType.ACT_REJECTION, act=act,
                task_text=f'Мероприятие {index}', department=department,
                due_date=timezone.localdate() + timedelta(days=index),
                created_by=assignee, status=task_status,
            )
            TaskAssignee.objects.create(task=task, user=assignee)

        self.client.force_login(assignee)
        response = self.client.get(reverse('dashboard:home'))

        # The five nearest deadlines, in that order; the rest stay in «Задачи».
        self.assertEqual(response.content.decode().count('class="dashboard-task"'), 5)
        self.assertContains(response, 'Мероприятие 4')
        self.assertNotContains(response, 'Мероприятие 5')
