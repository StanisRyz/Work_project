"""RT-4 server side for the acts registry and the open act page."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import (
    Act,
    ActComment,
    ActCorrectiveAction,
    ActHistoryEvent,
    ActRootAnalysis,
)
from acts.selectors import build_act_list_state, build_route_steps
from acts.services import add_act_comment, add_act_history_event
from references.models import ActStatus, DefectType, Operation, TaskStatus
from tasks.models import Task, TaskAssignee
from tasks.services import complete_task


class ActLiveMixin:
    @classmethod
    def setUpTestData(cls):
        cls.statuses = {}
        for code, name in (
            ('CREATED_OTK', 'Создан ОТК'),
            ('KO_REVIEW', 'На рассмотрении КО'),
            ('TO_ANALYSIS', 'На анализе ТО'),
            ('OTK_REVIEW', 'Проверка ОТК'),
            ('ARCHIVED', 'Архивирован'),
        ):
            cls.statuses[code], _ = ActStatus.objects.get_or_create(
                code=code, defaults={'name': name}
            )
        cls.in_progress, _ = TaskStatus.objects.get_or_create(
            code='IN_PROGRESS', defaults={'name': 'В работе'}
        )
        cls.completed_status, _ = TaskStatus.objects.get_or_create(
            code='COMPLETED', defaults={'name': 'Выполнено', 'is_final': True}
        )
        cls.operation = Operation.objects.create(code='ACTLIVE_OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='ACTLIVE_DEFECT', name='Дефект')
        cls.department = Department.objects.create(code='ACTLIVE_DEP', name='Отдел')
        cls.otk = cls.make_user('live_otk', UserProfile.Role.OTK)
        cls.other_otk = cls.make_user('live_otk_two', UserProfile.Role.OTK)
        cls.ko = cls.make_user('live_ko', UserProfile.Role.KO)
        cls.manager = cls.make_user('live_manager', UserProfile.Role.MANAGER)

    @classmethod
    def make_user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.save(update_fields=['role'])
        return user

    def make_act(self, status_code='CREATED_OTK', *, author=None, number=None, **extra):
        values = {
            'created_by': author or self.otk,
            'party_number': 'P-1',
            'nomenclature': 'Катушка',
            'operation': self.operation,
            'defect_type': self.defect_type,
            'status': self.statuses[status_code],
            'description': 'Описание',
        }
        if number:
            values['number'] = number
        values.update(extra)
        return Act.objects.create(**values)


class ActRegistryBuilderTests(ActLiveMixin, TestCase):
    def test_the_scope_and_filters_are_validated(self):
        state = build_act_list_state(
            self.manager, QueryDict('scope=nonsense&due=hack&act_type=hack')
        )

        self.assertEqual(state['scope'], 'my')
        self.assertEqual(state['selected']['due'], '')
        self.assertEqual(state['selected']['act_type'], '')

    def test_kpis_count_the_filtered_queryset(self):
        self.make_act('CREATED_OTK')
        self.make_act('KO_REVIEW')

        state = build_act_list_state(self.manager, QueryDict('scope=all'))

        self.assertEqual(state['kpis']['total'], 2)
        self.assertEqual(state['kpis']['created_otk'], 1)
        self.assertEqual(state['kpis']['ko_review'], 1)

    def test_a_status_change_moves_an_act_between_scopes(self):
        act = self.make_act('OTK_REVIEW')

        # The builder returns a lazy queryset, so each result is materialised
        # before the next mutation.
        active_ids = [
            item.pk for item in build_act_list_state(self.manager, QueryDict('scope=all'))['acts']
        ]
        act.status = self.statuses['ARCHIVED']
        act.save(update_fields=['status'])
        after_ids = [
            item.pk for item in build_act_list_state(self.manager, QueryDict('scope=all'))['acts']
        ]
        archive_ids = [
            item.pk
            for item in build_act_list_state(self.manager, QueryDict('scope=archive'))['acts']
        ]

        self.assertIn(act.pk, active_ids)
        self.assertNotIn(act.pk, after_ids)
        self.assertIn(act.pk, archive_ids)


class ActRegistryFragmentTests(ActLiveMixin, TestCase):
    def setUp(self):
        self.url = reverse('acts:list_fragment')
        self.client.force_login(self.manager)

    def test_authentication_is_required(self):
        self.client.logout()

        response = self.client.get(self.url)

        # STAB-1: a technical fragment endpoint answers 401 JSON, never an
        # HTML login redirect the fetch()-based client cannot parse as JSON.
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('Location', response)
        self.assertEqual(response.json(), {'error': 'authentication_required'})

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_the_fragment_selection_and_kpis_match_the_full_page(self):
        self.make_act('CREATED_OTK')
        self.make_act('KO_REVIEW')

        page = self.client.get(reverse('acts:list'), {'scope': 'all'})
        payload = self.client.get(self.url, {'scope': 'all'}).json()

        self.assertEqual(
            sorted(act.pk for act in page.context['acts']), sorted(payload['act_ids'])
        )
        for value in page.context['kpis'].values():
            self.assertIn(f'<strong>{value}</strong>', payload['kpis_html'])

    def test_the_scope_and_filters_are_preserved(self):
        visible = self.make_act('CREATED_OTK', number='АОК-2026-901')
        self.make_act('KO_REVIEW', number='АОК-2026-902')

        payload = self.client.get(
            self.url, {'scope': 'all', 'search': 'АОК-2026-901'}
        ).json()

        self.assertEqual(payload['act_ids'], [visible.pk])
        self.assertIn('АОК-2026-901', payload['results_html'])
        self.assertNotIn('АОК-2026-902', payload['results_html'])

    def test_an_act_outside_the_users_scope_never_reaches_the_html(self):
        foreign = self.make_act('KO_REVIEW', author=self.other_otk, number='АОК-2026-903')
        self.client.force_login(self.otk)

        payload = self.client.get(self.url, {'scope': 'my'}).json()

        self.assertNotIn(foreign.pk, payload['act_ids'])
        self.assertNotIn('АОК-2026-903', payload['results_html'])

    def test_a_status_change_moves_the_act_between_scopes(self):
        act = self.make_act('OTK_REVIEW')

        before = self.client.get(self.url, {'scope': 'all'}).json()
        act.status = self.statuses['ARCHIVED']
        act.save(update_fields=['status'])
        after = self.client.get(self.url, {'scope': 'all'}).json()
        archive = self.client.get(self.url, {'scope': 'archive'}).json()

        self.assertIn(act.pk, before['act_ids'])
        self.assertNotIn(act.pk, after['act_ids'])
        self.assertIn(act.pk, archive['act_ids'])

    def test_a_get_changes_nothing(self):
        act = self.make_act('CREATED_OTK')

        self.client.get(self.url, {'scope': 'all'})

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')
        self.assertEqual(Act.objects.count(), 1)

    def test_the_response_is_not_cacheable(self):
        response = self.client.get(self.url)

        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])
        self.assertEqual(response['Vary'], 'Cookie')

    def test_the_registry_page_exposes_live_containers(self):
        content = self.client.get(reverse('acts:list')).content.decode()

        self.assertIn('data-live-act-registry', content)
        self.assertIn('data-live-act-registry-kpis', content)
        self.assertIn('data-live-act-registry-results', content)
        self.assertIn(f'data-fragment-url="{self.url}"', content)


class ActDetailFragmentTests(ActLiveMixin, TestCase):
    def setUp(self):
        self.act = self.make_act('CREATED_OTK')
        self.urls = {
            'summary': reverse('acts:live_summary_fragment', args=[self.act.pk]),
            'history': reverse('acts:history_fragment', args=[self.act.pk]),
            'comments': reverse('acts:comments_fragment', args=[self.act.pk]),
            'activities': reverse('acts:activities_fragment', args=[self.act.pk]),
        }
        self.client.force_login(self.otk)

    def test_every_fragment_requires_authentication(self):
        self.client.logout()

        for name, url in self.urls.items():
            with self.subTest(fragment=name):
                response = self.client.get(url)
                # STAB-1: 401 JSON, never an HTML login redirect.
                self.assertEqual(response.status_code, 401)
                self.assertNotIn('Location', response)
                self.assertEqual(response.json(), {'error': 'authentication_required'})

    def test_every_fragment_checks_can_view_act(self):
        # A KO user cannot see an act still sitting at CREATED_OTK.
        self.client.force_login(self.ko)

        for name, url in self.urls.items():
            with self.subTest(fragment=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(self.act.number, response.content.decode())

    def test_only_get_is_allowed(self):
        for name, url in self.urls.items():
            with self.subTest(fragment=name):
                self.assertEqual(self.client.post(url).status_code, 405)

    def test_losing_access_returns_a_safe_404_without_act_data(self):
        self.act.status = self.statuses['KO_REVIEW']
        self.act.save(update_fields=['status'])

        response = self.client.get(self.urls['summary'])

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.act.number, response.content.decode())
        self.assertNotIn('Катушка', response.content.decode())

    def test_the_summary_shows_the_current_status_and_route(self):
        payload = self.client.get(self.urls['summary']).json()

        self.assertIn(self.act.number, payload['html'])
        self.assertIn('data-act-status-badge', payload['html'])
        self.assertIn('Создан ОТК', payload['html'])
        self.assertEqual(payload['status_code'], 'CREATED_OTK')
        self.assertIn('act-route__step--current', payload['html'])

    def test_the_summary_follows_a_status_change(self):
        self.client.force_login(self.manager)
        self.act.status = self.statuses['TO_ANALYSIS']
        self.act.save(update_fields=['status'])

        payload = self.client.get(self.urls['summary']).json()

        self.assertEqual(payload['status_code'], 'TO_ANALYSIS')
        steps = build_route_steps(Act.objects.get(pk=self.act.pk))
        self.assertEqual(steps[2]['state'], 'current')
        self.assertEqual(steps[0]['state'], 'completed')

    def test_the_history_fragment_shows_a_new_event(self):
        add_act_history_event(
            self.act,
            self.otk,
            ActHistoryEvent.EventType.ACT_EDITED,
            'Акт отредактирован в тесте.',
            emit_notification=False,
        )

        payload = self.client.get(self.urls['history']).json()

        self.assertIn('Акт отредактирован в тесте.', payload['html'])

    def test_the_comments_fragment_shows_a_new_comment_without_the_form(self):
        add_act_comment(self.act, self.otk, 'Свежий комментарий', notify=False)

        payload = self.client.get(self.urls['comments']).json()

        self.assertIn('Свежий комментарий', payload['html'])
        # The new-comment textarea must never be part of the replaceable block.
        self.assertNotIn('<textarea', payload['html'])
        self.assertNotIn('csrfmiddlewaretoken', payload['html'])
        self.assertNotIn('<form', payload['html'])

    def test_the_activities_fragment_reflects_the_task_status(self):
        task = self._make_task(self.otk)

        before = self.client.get(self.urls['activities']).json()
        complete_task(task, self.otk, 'Готово')
        after = self.client.get(self.urls['activities']).json()

        self.assertIn(f'data-related-task="{task.pk}"', before['html'])
        self.assertIn('В работе', before['html'])
        self.assertIn('Выполнено', after['html'])

    def test_related_tasks_are_filtered_by_permissions(self):
        task = self._make_task(self.other_otk)
        self.client.force_login(self.manager)
        manager_payload = self.client.get(self.urls['activities']).json()

        self.client.force_login(self.otk)
        author_payload = self.client.get(self.urls['activities']).json()

        self.assertIn(f'data-related-task="{task.pk}"', manager_payload['html'])
        # The act's OTK author is not an assignee, so the task stays hidden.
        self.assertNotIn(f'data-related-task="{task.pk}"', author_payload['html'])
        self.assertIn('недоступны', author_payload['html'])

    def test_fragments_are_not_cacheable(self):
        for name, url in self.urls.items():
            with self.subTest(fragment=name):
                response = self.client.get(url)
                self.assertIn('no-store', response['Cache-Control'])
                self.assertEqual(response['Vary'], 'Cookie')

    def test_a_get_changes_nothing(self):
        add_act_comment(self.act, self.otk, 'Комментарий', notify=False)
        comments_before = ActComment.objects.count()
        events_before = ActHistoryEvent.objects.count()

        for url in self.urls.values():
            self.client.get(url)

        self.assertEqual(ActComment.objects.count(), comments_before)
        self.assertEqual(ActHistoryEvent.objects.count(), events_before)

    def test_the_detail_page_exposes_live_containers_and_urls(self):
        content = self.client.get(reverse('acts:detail', args=[self.act.pk])).content.decode()

        self.assertIn('data-live-act-config', content)
        self.assertIn(f'data-live-act-id="{self.act.pk}"', content)
        self.assertIn(f'data-summary-url="{self.urls["summary"]}"', content)
        self.assertIn(f'data-history-url="{self.urls["history"]}"', content)
        self.assertIn(f'data-comments-url="{self.urls["comments"]}"', content)
        self.assertIn(f'data-activities-url="{self.urls["activities"]}"', content)
        self.assertIn('data-live-act-summary', content)
        self.assertIn('data-act-conflict-banner', content)
        self.assertIn('data-act-access-banner', content)
        self.assertIn('data-workflow-submit', content)

    def _make_task(self, assignee):
        root = ActRootAnalysis.objects.create(act=self.act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment='Мероприятие',
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=5),
        )
        task = Task.objects.create(
            source_action=action,
            act=self.act,
            root_analysis=root,
            task_text='Мероприятие',
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=5),
            created_by=self.otk,
            status=self.in_progress,
        )
        TaskAssignee.objects.create(task=task, user=assignee)
        return task


class ActWorkFragmentTests(ActLiveMixin, TestCase):
    """The «Проработка» fragment must match the tab it replaces."""

    def setUp(self):
        self.act = self.make_act('CREATED_OTK')
        self.url = reverse('acts:work_fragment', args=[self.act.pk])
        self.client.force_login(self.otk)

    def test_authentication_is_required(self):
        self.client.logout()

        response = self.client.get(self.url)

        # STAB-1: 401 JSON, never an HTML login redirect.
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('Location', response)
        self.assertEqual(response.json(), {'error': 'authentication_required'})

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_can_view_act_is_enforced(self):
        self.client.force_login(self.ko)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.act.number, response.content.decode())

    def test_the_fragment_matches_the_work_tab_of_the_full_page(self):
        page = self.client.get(reverse('acts:detail', args=[self.act.pk]), {'tab': 'work'})
        payload = self.client.get(self.url).json()

        self.assertTemplateUsed(page, 'acts/includes/work_content.html')
        self.assertEqual(payload['status_code'], 'CREATED_OTK')
        # Party data and the defect card come from the same partial.
        self.assertIn('Данные партии', payload['html'])
        self.assertIn(self.act.nomenclature, payload['html'])

    def test_the_fragment_carries_the_actions_the_status_allows(self):
        payload = self.client.get(self.url).json()

        # The OTK author may hand a CREATED_OTK act to KO.
        self.assertIn('Передать в КО', payload['html'])
        self.assertIn('data-workflow-submit', payload['html'])
        self.assertNotIn('Утвердить', payload['html'])

    def test_the_actions_follow_a_status_change(self):
        self.act.status = self.statuses['KO_REVIEW']
        self.act.save(update_fields=['status'])
        self.client.force_login(self.manager)

        payload = self.client.get(self.url).json()

        self.assertEqual(payload['status_code'], 'KO_REVIEW')
        self.assertIn('Передать в ТО', payload['html'])
        self.assertNotIn('Передать в КО', payload['html'])

    def test_a_get_changes_nothing(self):
        self.client.get(self.url)

        self.act.refresh_from_db()
        self.assertEqual(self.act.status.code, 'CREATED_OTK')
        self.assertEqual(ActHistoryEvent.objects.filter(act=self.act).count(), 0)

    def test_the_response_is_not_cacheable(self):
        response = self.client.get(self.url)

        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Vary'], 'Cookie')

    def test_the_detail_page_exposes_the_work_container_and_url(self):
        content = self.client.get(reverse('acts:detail', args=[self.act.pk])).content.decode()

        self.assertIn('data-live-act-work', content)
        self.assertIn(f'data-work-url="{self.url}"', content)
