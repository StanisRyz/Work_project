"""A `BOARD` task lives on its board: redirects, the panel's work, the registry."""

import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from references.models import TaskStatus
from tasks.models import Task, TaskAttachment
from tasks.services import TaskWorkflowError, add_task_attachment, complete_task

from ..services import create_board
from .helpers import (
    FOLLOW_FORM, BoardFixtureMixin, board_url, done_column_of, fresh_code, legacy_attachment, new_card,
)


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def main_of(response):
    """The page's own content, without the topbar (whose «Сообщить об ошибке»
    carries a confirm trigger and the current URL of its own)."""
    return response.content.decode().split('<main', 1)[1].split('</main>', 1)[0]


def panel_of(content):
    """The card panel's «Описание» — its heading, the description, the
    checklist, the facts and tools, the followers — without «Чат» and «Лог»."""
    return content.split('data-live-board-panel', 1)[1].split('data-board-tab-body="chat"', 1)[0]


def upload(name='отчёт.pdf', content=b'%PDF-1.4 '):
    return SimpleUploadedFile(name, content, 'application/pdf')


MEDIA = override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='board-attachments-'))


class PanelTestMixin(BoardFixtureMixin):
    def setUp(self):
        self.card_obj = self.card('Сверить остатки', assignees=[self.member], stage='REVIEW')
        self.task = task_of(self.card_obj)

    @property
    def card_url(self):
        return f"{board_url(self.board)}?card={self.card_obj.pk}"

    def panel(self, user):
        self.client.force_login(user)
        return self.client.get(self.card_url)


@MEDIA
class RedirectTests(PanelTestMixin, TestCase):
    def test_task_detail_opens_the_board_card(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse('tasks:detail', args=[self.task.pk]))
        self.assertRedirects(response, self.card_url)

    def test_an_upload_creates_nothing_and_leads_to_the_chat(self):
        """A card's files are attached in its «Чат»; its task takes none."""
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('tasks:add_attachment', args=[self.task.pk]),
            {'file': upload(), 'list_query': 'tab=files', 'execution_comment': 'Почти готово'},
            follow=True,
        )
        self.assertRedirects(response, f'{self.card_url}&tab=chat')
        self.assertEqual(response.context['tab'], 'chat')
        self.assertContains(response, 'Файлы карточки прикрепляют в чате')
        self.assertEqual(response.context['execution_comment'], '')
        self.assertNotContains(response, 'Почти готово')
        self.assertFalse(TaskAttachment.objects.exists())
        # A GET is led there too, and the service refuses whoever calls it.
        self.assertRedirects(
            self.client.get(reverse('tasks:add_attachment', args=[self.task.pk])),
            f'{self.card_url}&tab=chat', fetch_redirect_response=False,
        )
        with self.assertRaises(TaskWorkflowError):
            add_task_attachment(self.task, self.member, upload())
        self.assertFalse(TaskAttachment.objects.exists())

    def test_tasks_complete_does_nothing_for_a_board_task(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('tasks:complete', args=[self.task.pk]), {'execution_comment': 'Сделано'}, follow=True,
        )
        self.assertRedirects(response, self.card_url)
        self.assertTemplateNotUsed(response, 'tasks/detail.html')
        self.assertContains(response, 'Карточку доски завершают и возвращают на доске.')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'IN_PROGRESS')
        self.assertEqual(self.task.execution_comment, '')
        self.assertFalse(self.card_obj.events.filter(kind='COMPLETED').exists())

    def test_tasks_reopen_does_nothing_for_a_board_task(self):
        complete_task(self.task, self.member, 'Готово')
        self.client.force_login(self.admin)
        response = self.client.post(reverse('tasks:reopen', args=[self.task.pk]), follow=True)
        self.assertRedirects(response, self.card_url)
        self.assertContains(response, 'Карточку доски завершают и возвращают на доске.')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'COMPLETED')
        self.assertFalse(self.card_obj.events.filter(kind='REOPENED').exists())

    def test_a_bad_file_is_not_even_looked_at(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('tasks:add_attachment', args=[self.task.pk]),
            {'file': upload('вирус.exe'), 'execution_comment': 'Текст'},
            follow=True,
        )
        self.assertRedirects(response, f'{self.card_url}&tab=chat')
        self.assertTemplateNotUsed(response, 'tasks/detail.html')
        self.assertContains(response, 'Файлы карточки прикрепляют в чате')
        self.assertFalse(TaskAttachment.objects.exists())

    def test_other_sources_upload_as_before(self):
        from bugs.models import BugReport
        from tasks.services import create_bug_report_task

        report = BugReport.objects.create(reporter=self.member, message='Ошибка', page_url='/')
        task = create_bug_report_task(
            report, [self.member.pk], created_by=self.member, due_date=self.task.due_date,
        )
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('tasks:add_attachment', args=[task.pk]), {'file': upload(), 'execution_comment': ''},
        )
        self.assertRedirects(response, reverse('tasks:detail', args=[task.pk]), fetch_redirect_response=False)
        self.assertEqual(TaskAttachment.objects.filter(task=task).count(), 1)


@MEDIA
class PanelWorkTests(PanelTestMixin, TestCase):
    def test_assignee_completes_and_the_card_is_done(self):
        response = self.panel(self.member)
        self.assertContains(response, reverse('boards:card_complete', args=[self.board.pk, self.card_obj.pk]))
        self.assertContains(response, 'data-hotkey-submit')
        response = self.client.post(
            reverse('boards:card_complete', args=[self.board.pk, self.card_obj.pk]),
            {'execution_comment': 'Остатки сверены'},
        )
        self.assertRedirects(response, self.card_url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'COMPLETED')
        done = self.client.get(self.card_url).context['columns'][-1]
        self.assertTrue(done['is_done'])
        self.assertEqual(done['column'], done_column_of(self.board))
        self.assertEqual([item['card'] for item in done['cards']], [self.card_obj])

    def test_empty_comment_is_refused_and_kept(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('boards:card_complete', args=[self.board.pk, self.card_obj.pk]),
            {'execution_comment': '   '},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['panel'], 'view')
        self.assertContains(response, 'Укажите результат выполнения задачи.')
        self.assertEqual(response.context['execution_comment'], '   ')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'IN_PROGRESS')

    def test_not_an_assignee_sees_no_form_and_is_refused(self):
        response = self.panel(self.colleague)
        self.assertNotIn('execution_comment', main_of(response))
        self.assertNotIn(reverse('tasks:add_attachment', args=[self.task.pk]), main_of(response))
        response = self.client.post(
            reverse('boards:card_complete', args=[self.board.pk, self.card_obj.pk]),
            {'execution_comment': 'Не моё'},
        )
        self.assertContains(response, 'Завершение задачи недоступно.')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'IN_PROGRESS')

    def test_get_on_complete_changes_nothing(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('boards:card_complete', args=[self.board.pk, self.card_obj.pk]))
        self.assertRedirects(response, self.card_url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'IN_PROGRESS')

    def test_admin_reopens_into_the_old_column_with_the_old_result(self):
        complete_task(self.task, self.member, 'Сверено, расхождений нет')
        reopen_url = reverse('boards:card_reopen', args=[self.board.pk, self.card_obj.pk])
        response = self.panel(self.admin)
        self.assertContains(response, reopen_url)
        self.assertNotContains(response, reverse('tasks:reopen', args=[self.task.pk]))
        self.assertContains(response, 'Сверено, расхождений нет')
        response = self.client.post(reopen_url, follow=True)
        self.assertRedirects(response, self.card_url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'IN_PROGRESS')
        review = next(column for column in response.context['columns'] if column['name'] == 'На проверке')
        self.assertEqual([item['card'] for item in review['cards']], [self.card_obj])
        # The administrator may complete it again, so the old result is back in the field.
        self.assertEqual(response.context['execution_comment'], 'Сверено, расхождений нет')

    def test_member_does_not_see_reopen(self):
        complete_task(self.task, self.member, 'Готово')
        self.assertNotContains(
            self.panel(self.member), reverse('boards:card_reopen', args=[self.board.pk, self.card_obj.pk]),
        )

    def test_an_older_attachment_is_shown_in_the_chat_and_deleted_back_to_the_board(self):
        attachment = legacy_attachment(self.task, self.member)
        response = self.panel(self.member)
        self.assertContains(response, reverse('tasks:download_attachment', args=[self.task.pk, attachment.pk]))
        self.assertContains(response, reverse('tasks:delete_attachment', args=[self.task.pk, attachment.pk]))
        self.assertNotContains(response, reverse('tasks:add_attachment', args=[self.task.pk]))
        response = self.client.post(
            reverse('tasks:delete_attachment', args=[self.task.pk, attachment.pk]),
            {'list_query': 'tab=chat'}, follow=True,
        )
        self.assertRedirects(response, f'{self.card_url}&tab=chat')
        self.assertFalse(TaskAttachment.objects.exists())

    def test_an_older_attachment_is_deleted_by_the_task_rule_only(self):
        attachment = legacy_attachment(self.task, self.member)
        # Not an исполнитель: no cross, and the route refuses.
        self.assertNotContains(
            self.panel(self.colleague), reverse('tasks:delete_attachment', args=[self.task.pk, attachment.pk]),
        )
        response = self.client.post(reverse('tasks:delete_attachment', args=[self.task.pk, attachment.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(TaskAttachment.objects.filter(pk=attachment.pk).exists())

    def test_completed_card_downloads_only(self):
        attachment = legacy_attachment(self.task, self.member)
        complete_task(self.task, self.member, 'Готово')
        content = main_of(self.panel(self.member))
        self.assertIn(reverse('tasks:download_attachment', args=[self.task.pk, attachment.pk]), content)
        self.assertNotIn(reverse('tasks:delete_attachment', args=[self.task.pk, attachment.pk]), content)
        self.assertNotIn(reverse('tasks:add_attachment', args=[self.task.pk]), content)
        self.assertIn('Готово', content)
        self.assertIn('Завершил', content)

    def test_cancelled_card_has_no_action(self):
        Task.objects.filter(pk=self.task.pk).update(
            status=TaskStatus.objects.get(code='CANCELLED'), cancellation_reason='Отозвано',
        )
        for user in (self.member, self.admin):
            with self.subTest(user=user.username):
                # «Следить» stays on a closed card: it may still be discussed.
                content = FOLLOW_FORM.sub('', panel_of(main_of(self.panel(user))))
                self.assertIn('Отозвано', content)
                for marker in ('<form', 'data-confirm', 'edit=1'):
                    self.assertNotIn(marker, content)

    def _page_queries(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.card_url)
        return len(queries)

    def test_query_count_does_not_depend_on_cards_or_attachments(self):
        self.client.force_login(self.member)
        legacy_attachment(self.task, self.member)
        complete_task(task_of(self.card('Готовая')), self.member, 'Да')
        baseline = self._page_queries()
        for index in range(4):
            legacy_attachment(self.task, self.member, f'файл-{index}.pdf')
        for index in range(5):
            self.card(f'Ещё {index}', assignees=[self.member, self.colleague])
        for index in range(2):
            complete_task(task_of(self.card(f'Готовая {index}')), self.member, 'Да')
        self.assertEqual(self._page_queries(), baseline)


@MEDIA
class RegistryTests(PanelTestMixin, TestCase):
    def test_source_is_the_board_with_a_link_to_the_card(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        row = next(row for row in response.context['rows'] if row['task'].pk == self.task.pk)
        self.assertEqual(
            row['source'], {'label': f'Доска «Планирование» · {self.board.code}-1', 'url': self.card_url},
        )
        self.assertContains(response, f'Доска «Планирование» · {self.board.code}-1')
        self.assertContains(response, f'href="{self.card_url}"')

    def test_row_click_opens_the_card(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        row_url = reverse('tasks:detail', args=[self.task.pk])
        self.assertContains(response, f'data-row-url="{row_url}')
        self.assertRedirects(self.client.get(row_url), self.card_url)

    def test_search_by_board_name(self):
        other = create_board(
            code=fresh_code(),
            name='Отгрузки', department=self.department, owner=self.owner, actor=self.owner,
        )
        foreign = new_card(other, self.owner, 'Чужая', assignees=[self.owner])
        self.client.force_login(self.member)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': 'Планир'})
        pks = [row['task'].pk for row in response.context['rows']]
        self.assertIn(self.task.pk, pks)
        self.assertNotIn(task_of(foreign).pk, pks)

    def _queries(self, params):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse('tasks:list'), params)
        return len(queries)

    def test_query_count_does_not_grow_with_rows(self):
        self.client.force_login(self.member)
        for params in ({'tab': 'all'}, {'tab': 'all', 'export': 'xlsx'}):
            with self.subTest(params=params):
                baseline = self._queries(params)
                for index in range(4):
                    self.card(f'Строка {index} {params}', assignees=[self.member, self.colleague])
                self.assertEqual(self._queries(params), baseline)
