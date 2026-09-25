"""Quick search, «что ждёт меня» counts and the registry spreadsheets."""

import io
import zipfile
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act
from dashboard.search import quick_search
from dashboard.summary import my_work_counts
from ecosystem.xlsx import build_xlsx
from protocols.models import QUALITY_PROTOCOL_TYPE_CODE, Protocol, ProtocolApproval, ProtocolType
from protocols.services import create_protocol
from references.models import ActStatus, TaskStatus
from tasks.models import Task, TaskAssignee


class QuickSearchAndSummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())
        cls.department, _ = Department.objects.get_or_create(code='QS_DEP', defaults={'name': 'Отдел'})
        cls.otk = cls._user('qs_otk', UserProfile.Role.OTK)
        cls.colleague = cls._user('qs_colleague', UserProfile.Role.TO)
        cls.act = Act.objects.create(
            number='АОК-2026-00104', customer='ПАО «Россети»', nomenclature='Катушка',
            created_by=cls.otk, status=ActStatus.objects.get(code='CREATED_OTK'),
            due_date=timezone.localdate() - timedelta(days=1),
        )
        cls.protocol = create_protocol(ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE), cls.colleague)

    @classmethod
    def _user(cls, username, role):
        user = User.objects.create_user(username, password='pw-12345')
        profile = user.userprofile
        profile.role = role
        profile.department = cls.department
        profile.save(update_fields=['role', 'department'])
        return User.objects.get(pk=user.pk)

    def _task(self, user, text='Проверить намотку', days=2):
        task = Task.objects.create(
            source_type=Task.SourceType.BUG,
            task_text=text, due_date=timezone.localdate() + timedelta(days=days),
            status=TaskStatus.objects.get(code='IN_PROGRESS'), created_by=self.colleague,
            bug_report=self._bug(),
        )
        TaskAssignee.objects.create(task=task, user=user)
        return task

    def _bug(self):
        from bugs.models import BugReport

        return BugReport.objects.create(reporter=self.colleague, message='Ошибка', page_url='/')

    # -------------------------------------------------------------- search

    def labels(self, term, user=None):
        return {label: [hit.title for hit in hits] for label, hits in quick_search(user or self.otk, term)}

    def test_an_act_is_found_by_number_customer_and_its_digits(self):
        for term in ('АОК-2026-00104', 'Россети', '104'):
            with self.subTest(term=term):
                self.assertIn('АОК-2026-00104', self.labels(term).get('Акты', []))

    def test_a_protocol_is_found_by_type_and_number(self):
        title = f'Качество №{self.protocol.number}'
        self.assertIn(title, self.labels(f'Качество {self.protocol.number}').get('Протоколы', []))
        self.assertIn(title, self.labels(f'Качество №{self.protocol.number}').get('Протоколы', []))

    def test_a_task_is_found_by_number_but_not_by_a_protocol_term(self):
        task = self._task(self.otk)
        self.assertIn(f'Задача №{task.pk}', self.labels(f'задача {task.pk}').get('Задачи', []))
        self.assertNotIn(f'Задача №{task.pk}', self.labels(f'Качество {task.pk}').get('Задачи', []))
        self.assertIn(f'Задача №{task.pk}', self.labels('намотку').get('Задачи', []))

    def test_a_single_hit_opens_directly_and_short_terms_search_nothing(self):
        self.client.force_login(self.otk)

        response = self.client.get(reverse('dashboard:search'), {'q': 'Россети'})
        self.assertRedirects(response, reverse('acts:detail', args=[self.act.pk]), fetch_redirect_response=False)
        self.assertEqual(quick_search(self.otk, 'К'), [])
        self.assertContains(self.client.get(reverse('dashboard:search'), {'q': 'К'}), 'хотя бы два символа')

    def test_the_topbar_carries_the_box(self):
        self.client.force_login(self.otk)

        self.assertContains(self.client.get(reverse('acts:list')), 'data-quick-search')

    # -------------------------------------------------------------- summary

    def test_counts_are_the_owning_queues(self):
        self._task(self.otk, days=-2)
        self._task(self.otk, days=3)
        self._task(self.colleague)
        self.protocol.status = Protocol.Status.APPROVAL
        self.protocol.revision = 2
        self.protocol.save(update_fields=['status', 'revision'])
        ProtocolApproval.objects.create(protocol=self.protocol, revision=2, user=self.otk, display_name='ОТК')
        # A pending row of an earlier round is not waiting for anybody.
        ProtocolApproval.objects.create(protocol=self.protocol, revision=1, user=self.colleague, display_name='x')

        counts = my_work_counts(self.otk)

        self.assertEqual(counts['acts'], 1)
        self.assertEqual(counts['acts_overdue'], 1)
        self.assertEqual(counts['tasks'], 2)
        self.assertEqual(counts['tasks_overdue'], 1)
        self.assertEqual(counts['approvals'], 1)
        self.assertEqual(my_work_counts(self.colleague)['approvals'], 0)

        self.client.force_login(self.otk)
        home = self.client.get(reverse('dashboard:home'))
        self.assertContains(home, 'Протоколы ждут вашей подписи')
        self.assertContains(home, 'class="nav-count nav-count--alert"')

    # -------------------------------------------------------------- export

    def sheet(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="', response['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            return archive.read('xl/worksheets/sheet1.xml').decode('utf-8')

    def test_every_registry_exports_what_it_shows(self):
        self._task(self.otk, text='Экспортируемая задача')
        self.client.force_login(self.otk)

        acts = self.sheet(self.client.get(reverse('acts:list'), {'scope': 'all', 'export': 'xlsx'}))
        self.assertIn('Россети', acts)
        self.assertIn('Заказчик', acts)
        filtered = self.sheet(self.client.get(reverse('acts:list'), {'scope': 'all', 'search': 'нет-такого', 'export': 'xlsx'}))
        self.assertNotIn('Россети', filtered)

        self.assertIn('Экспортируемая задача', self.sheet(self.client.get(reverse('tasks:list'), {'export': 'xlsx'})))
        self.assertIn(f'<v>{self.protocol.number}</v>', self.sheet(self.client.get(reverse('protocols:list'), {'export': 'xlsx'})))
        self.assertIn('Тип аудита', self.sheet(self.client.get(reverse('smk:list'), {'export': 'xlsx'})))

    def test_the_writer_keeps_values_honest(self):
        content = build_xlsx('Лист: [тест]', ['A', 'B', 'C', 'D'], [['текст <b>', 5, None, timezone.localdate()]])
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            sheet = archive.read('xl/worksheets/sheet1.xml').decode('utf-8')
            workbook = archive.read('xl/workbook.xml').decode('utf-8')
        self.assertIn('текст &lt;b&gt;', sheet)
        self.assertIn('<c r="B2"><v>5</v></c>', sheet)
        self.assertIn('<c r="C2"/>', sheet)
        self.assertIn(timezone.localdate().strftime('%d.%m.%Y'), sheet)
        self.assertIn('name="Лист   тест "', workbook)
