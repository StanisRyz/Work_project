"""Server side of the task registry: shared builder and live fragment."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActRootAnalysis
from references.models import ActStatus, DefectType, Operation, TaskStatus
from tasks.models import Task, TaskAssignee
from tasks.selectors import build_task_list_state
from tasks.services import complete_task


class TaskLiveMixin:
    @classmethod
    def setUpTestData(cls):
        cls.status_created, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        cls.in_progress, _ = TaskStatus.objects.get_or_create(
            code='IN_PROGRESS', defaults={'name': 'В работе'}
        )
        cls.completed, _ = TaskStatus.objects.get_or_create(
            code='COMPLETED', defaults={'name': 'Выполнено', 'is_final': True}
        )
        cls.operation = Operation.objects.create(code='LIVE_OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='LIVE_DEFECT', name='Дефект')
        cls.department = Department.objects.create(code='LIVE_DEP', name='Отдел')
        cls.owner = cls.make_user('live_to', UserProfile.Role.TO)
        cls.other = cls.make_user('live_other', UserProfile.Role.TO)
        cls.act = Act.objects.create(
            created_by=cls.owner,
            nomenclature='Катушка',
            status=cls.status_created,
        )

    @classmethod
    def make_user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.save(update_fields=['role'])
        return user

    def make_task(self, assignee, *, days=3, status=None, text='Мероприятие'):
        root = ActRootAnalysis.objects.create(act=self.act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment=text,
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=days),
        )
        task = Task.objects.create(
            source_action=action,
            act=self.act,
            root_analysis=root,
            task_text=text,
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=days),
            created_by=self.owner,
            status=status or self.in_progress,
        )
        TaskAssignee.objects.create(task=task, user=assignee)
        return task


class TaskListBuilderTests(TaskLiveMixin, TestCase):
    def test_the_my_tab_shows_only_the_users_own_active_tasks(self):
        mine = self.make_task(self.owner)
        theirs = self.make_task(self.other)

        state = build_task_list_state(self.owner, _query('tab=my'))

        self.assertEqual([task.pk for task in state['tasks']], [mine.pk])
        self.assertNotIn(theirs.pk, [task.pk for task in state['tasks']])

    def test_a_completed_task_leaves_the_active_tabs(self):
        task = self.make_task(self.owner)
        complete_task(task, self.owner, 'Готово')

        active = build_task_list_state(self.owner, _query('tab=my'))
        archive = build_task_list_state(self.owner, _query('tab=archive'))

        self.assertEqual(list(active['tasks']), [])
        self.assertEqual([item.pk for item in archive['tasks']], [task.pk])

    def test_filters_and_sorting_are_validated(self):
        state = build_task_list_state(
            self.owner, _query('tab=nonsense&source_type=hack&due=hack&sort=hack')
        )

        self.assertEqual(state['tab'], 'my')
        self.assertEqual(state['selected']['source_type'], '')
        self.assertEqual(state['selected']['due'], '')
        self.assertEqual(state['selected']['sort'], '')

    def test_tab_and_sort_urls_keep_the_current_filters(self):
        state = build_task_list_state(self.owner, _query('tab=all&source=AOK&sort=nearest'))

        self.assertIn('source=AOK', state['tab_urls']['archive'])
        self.assertIn('tab=archive', state['tab_urls']['archive'])
        self.assertIn('sort=farthest', state['sort_url'])
        self.assertEqual(state['reset_url'], '?tab=all')


class TaskListFragmentTests(TaskLiveMixin, TestCase):
    def setUp(self):
        self.url = reverse('tasks:list_fragment')
        self.client.force_login(self.owner)

    def test_authentication_is_required(self):
        self.client.logout()

        response = self.client.get(self.url)

        # A technical fragment endpoint answers 401 JSON, never an
        # HTML login redirect the fetch()-based client cannot parse as JSON.
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('Location', response)
        self.assertEqual(response.json(), {'error': 'authentication_required'})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_the_fragment_selection_matches_the_full_page(self):
        mine = self.make_task(self.owner)
        self.make_task(self.other)

        page = self.client.get(reverse('tasks:list'), {'tab': 'my'})
        payload = self.client.get(self.url, {'tab': 'my'}).json()

        self.assertEqual(payload['task_ids'], [mine.pk])
        self.assertEqual(
            [task.pk for task in page.context['tasks']], payload['task_ids']
        )
        self.assertIn(f'data-task-row="{mine.pk}"', payload['results_html'])

    def test_the_tab_and_filters_are_preserved(self):
        self.make_task(self.owner, text='Первое')

        payload = self.client.get(self.url, {'tab': 'all', 'sort': 'nearest'}).json()

        self.assertEqual(payload['tab'], 'all')
        self.assertIn('task-sort-link--active', payload['results_html'])

    def test_another_users_task_is_present_in_the_all_tab(self):
        theirs = self.make_task(self.other, text='Чужое мероприятие')

        payload = self.client.get(self.url, {'tab': 'all'}).json()

        self.assertIn(f'data-task-row="{theirs.pk}"', payload['results_html'])
        self.assertIn(theirs.pk, payload['task_ids'])

    def test_a_new_task_appears_in_the_my_tab(self):
        before = self.client.get(self.url, {'tab': 'my'}).json()
        task = self.make_task(self.owner)

        after = self.client.get(self.url, {'tab': 'my'}).json()

        self.assertEqual(before['task_ids'], [])
        self.assertEqual(after['task_ids'], [task.pk])

    def test_a_completed_task_moves_from_the_active_list_to_the_archive(self):
        task = self.make_task(self.owner)
        complete_task(task, self.owner, 'Готово')

        active = self.client.get(self.url, {'tab': 'my'}).json()
        archive = self.client.get(self.url, {'tab': 'archive'}).json()

        self.assertEqual(active['task_ids'], [])
        self.assertNotIn(f'data-task-row="{task.pk}"', active['results_html'])
        self.assertEqual(archive['task_ids'], [task.pk])
        self.assertIn(f'data-task-row="{task.pk}"', archive['results_html'])

    def test_a_get_changes_nothing(self):
        task = self.make_task(self.owner)

        self.client.get(self.url, {'tab': 'my'})

        task.refresh_from_db()
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        self.assertEqual(Task.objects.count(), 1)

    def test_the_response_is_not_cacheable(self):
        response = self.client.get(self.url)

        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])
        self.assertEqual(response['Vary'], 'Cookie')

    def test_the_payload_reports_when_it_was_generated(self):
        payload = self.client.get(self.url).json()

        self.assertTrue(payload['generated_at'])

    def test_the_list_page_exposes_a_live_container(self):
        content = self.client.get(reverse('tasks:list')).content.decode()

        self.assertIn('data-live-task-list', content)
        self.assertIn(f'data-fragment-url="{self.url}"', content)


def _query(raw):
    from django.http import QueryDict

    return QueryDict(raw)
