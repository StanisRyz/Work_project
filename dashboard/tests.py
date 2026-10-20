"""The landing page: it opens, the section list respects the permissions, and
the lists show the user's own open work — tasks, acts, signatures.

The sections behind the links and the registries themselves are covered by
their own apps' suites; what is tested here is the page, the filtering of the
section list and the shortened lists.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act
from protocols.models import QUALITY_PROTOCOL_TYPE_CODE, ProtocolAgendaItem, ProtocolType
from protocols.services import add_participant, add_speech, create_protocol, send_protocol_for_approval
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
        # «Документация» is read by every employee: the card follows the same
        # `can_view_documents()` the library itself enforces.
        self.assertContains(employee_page, documentation)

        self.client.logout()
        self.assertNotEqual(self.client.get(reverse('dashboard:home')).status_code, 200)


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

    def test_the_block_never_shows_more_than_eight_tasks(self):
        assignee = _create_user('otk_limit', UserProfile.Role.OTK)
        department = Department.objects.create(code='LIMIT', name='ПДО')
        archived = ActStatus.objects.get(code='ARCHIVED')
        task_status = TaskStatus.objects.get(code='IN_PROGRESS')
        for index in range(10):
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

        # The eight nearest deadlines, in that order; the rest stay in «Задачи».
        self.assertEqual(response.content.decode().count('class="dashboard-task"'), 8)
        self.assertContains(response, 'Мероприятие 7')
        self.assertNotContains(response, 'Мероприятие 8')


class DashboardActAndApprovalBlockTests(TestCase):
    def test_my_acts_are_the_registrys_own_queue(self):
        author = _create_user('otk_acts', UserProfile.Role.OTK)
        other = _create_user('otk_acts_other', UserProfile.Role.OTK)
        created, _ = ActStatus.objects.get_or_create(code='CREATED_OTK', defaults={'name': 'Создан ОТК'})
        Act.objects.create(number='АОК-2026-00301', nomenclature='Моё изделие', status=created, created_by=author)
        Act.objects.create(number='АОК-2026-00302', nomenclature='Чужое изделие', status=created, created_by=other)

        self.client.force_login(author)
        response = self.client.get(reverse('dashboard:home'))

        # «Мои акты» is the creator's own CREATED_OTK act, never a colleague's.
        self.assertContains(response, 'Мои акты в работе')
        self.assertContains(response, 'АОК-2026-00301')
        self.assertNotContains(response, 'АОК-2026-00302')

    def test_a_protocol_waiting_for_my_signature_is_listed_only_for_me(self):
        department = Department.objects.create(name='ТО', code='DASH_TO')
        author = _create_user('dash_author', UserProfile.Role.OTK)
        approver = _create_user('dash_approver', UserProfile.Role.OTK)
        for user in (author, approver):
            user.userprofile.department = department
            user.userprofile.save(update_fields=['department'])
        protocol = create_protocol(ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE), author)
        add_participant(protocol, approver, department=department, requires_approval=True, display_order=1)
        ProtocolAgendaItem.objects.create(protocol=protocol, text='Вопрос', display_order=0)
        add_speech(protocol, protocol.participants.get(user=author), 'Доложил.')
        send_protocol_for_approval(protocol, author)

        self.client.force_login(approver)
        mine = self.client.get(reverse('dashboard:home'))
        self.assertContains(mine, 'Ждут вашей подписи')
        self.assertContains(mine, reverse('protocols:detail', args=[protocol.pk]))

        # The author signs nothing, so the card is not drawn at all.
        self.client.force_login(author)
        self.assertNotContains(self.client.get(reverse('dashboard:home')), 'Ждут вашей подписи')
