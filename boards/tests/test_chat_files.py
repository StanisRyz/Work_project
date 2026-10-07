"""Stage 19: files in a card's «Чат» — `BoardCardFile`, its services, its
protected download, the chat's two views, the tile's «📎 N»; the tab «Файлы»
gone, and the guarded panel without «Подписчики»."""

import json
import os
import re
import tempfile
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification
from realtime.events import RealtimeEventType
from realtime.fragments import content_revision
from realtime.sync import REVISION_BOARDS, build_sync_state
from realtime.testing import capture_realtime_events
from tasks.models import TaskAttachment
from tasks.services import complete_task

from ..models import MAX_FILES_PER_MESSAGE, BoardCardComment, BoardCardFile
from ..services import (
    BoardError,
    add_board_members,
    archive_board,
    cancel_card,
    complete_card,
    delete_card_file,
    post_card_comment,
    remove_board_member,
)
from .helpers import (
    BoardFixtureMixin,
    assert_page_matches_fragment,
    board_url,
    chat_upload,
    fragment_url,
    legacy_attachment,
    make_user,
    page_attribute,
)
from .test_journal import task_of


MEDIA_ROOT = tempfile.mkdtemp(prefix='board-chat-files-')
MEDIA = override_settings(MEDIA_ROOT=MEDIA_ROOT)
REAL_ACCESS = mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN}))

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 32


def image(name='скриншот.png', content_type='image/png'):
    return chat_upload(name, PNG, content_type)


def stored_files():
    """Every file under `boards/files/` of the test media root."""
    root = os.path.join(MEDIA_ROOT, 'boards', 'files')
    found = []
    for folder, _dirs, files in os.walk(root):
        found.extend(os.path.join(folder, name) for name in files)
    return found


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


@MEDIA
class PostWithFilesTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Чертёж', assignees=[self.member])

    def post(self, text='', files=(), actor=None, **extra):
        return post_card_comment(self.card_obj, actor=actor or self.member, text=text, files=list(files), **extra)

    def test_a_message_with_files_and_one_of_files_only(self):
        comment = self.post('Смотрите вложения', [chat_upload('схема.pdf'), image()])
        files = list(comment.files.order_by('pk'))
        self.assertEqual([item.original_name for item in files], ['схема.pdf', 'скриншот.png'])
        for item in files:
            self.assertEqual(item.card, self.card_obj)
            self.assertEqual(item.uploaded_by, self.member)
            # The browser's name is never the stored path.
            self.assertRegex(item.file.name, rf'^boards/files/{self.card_obj.pk}/[0-9a-f]{{32}}\.(pdf|png)$')
            self.assertTrue(os.path.exists(item.file.path))
        self.assertEqual(files[0].size, len(b'%PDF-1.4 chat'))
        self.assertEqual(files[0].content_type, 'application/pdf')
        only_files = self.post('   ', [chat_upload('акт.docx', b'docx', 'application/octet-stream')])
        self.assertEqual(only_files.text, '')
        self.assertEqual(only_files.files.count(), 1)

    def test_an_empty_message_without_files_is_refused(self):
        with self.assertRaisesMessage(BoardError, 'Напишите сообщение или прикрепите файл.'):
            self.post('  ')
        self.assertFalse(BoardCardComment.objects.exists())

    def test_at_most_ten_files_per_message(self):
        self.assertEqual(MAX_FILES_PER_MESSAGE, 10)
        before = stored_files()
        with self.assertRaisesMessage(BoardError, 'не больше 10 файлов'):
            self.post('Много', [chat_upload(f'{index}.pdf') for index in range(11)])
        self.assertFalse(BoardCardComment.objects.exists())
        self.assertEqual(stored_files(), before)
        self.assertEqual(self.post('Десять', [chat_upload(f'{index}.pdf') for index in range(10)]).files.count(), 10)

    def test_type_and_size_are_the_common_policy_and_a_refusal_leaves_nothing(self):
        before = stored_files()
        with self.assertRaisesMessage(BoardError, 'Файл «вирус.exe»: Недопустимый тип файла.'):
            self.post('Плохой', [chat_upload('хороший.pdf'), chat_upload('вирус.exe', b'MZ')])
        with mock.patch('ecosystem.attachments.MAX_ATTACHMENT_SIZE', 5):
            with self.assertRaisesMessage(BoardError, 'Размер файла превышает допустимый лимит.'):
                self.post('Большой', [chat_upload('большой.pdf')])
        self.assertFalse(BoardCardComment.objects.exists())
        self.assertFalse(BoardCardFile.objects.exists())
        self.assertEqual(stored_files(), before)

    def test_a_failure_after_the_files_were_written_removes_them(self):
        before = stored_files()
        with mock.patch('notifications.services.notify_board_card_comment', side_effect=RuntimeError('сбой')):
            with self.assertRaises(RuntimeError):
                self.post('С файлом', [chat_upload('схема.pdf'), image()])
        self.assertFalse(BoardCardComment.objects.exists())
        self.assertFalse(BoardCardFile.objects.exists())
        self.assertEqual(stored_files(), before)

    def test_who_may_attach(self):
        self.assertEqual(self.post('Участник', [chat_upload()], actor=self.colleague).files.count(), 1)
        self.assertEqual(self.post('Администратор', [chat_upload()], actor=self.admin).files.count(), 1)
        files = BoardCardFile.objects.count()
        with self.assertRaises(BoardError):
            self.post('Посторонний', [chat_upload()], actor=self.outsider)
        self.assertEqual(BoardCardFile.objects.count(), files)

    def test_a_closed_card_takes_files_and_an_archived_board_does_not(self):
        complete_card(self.card_obj, actor=self.member, execution_comment='Готово')
        self.assertEqual(self.post('После', [chat_upload()]).files.count(), 1)
        archive_board(self.board, actor=self.owner)
        files = stored_files()
        with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
            self.post('В архиве', [chat_upload()])
        self.assertEqual(stored_files(), files)

    def test_one_event_and_one_notification_per_message_naming_no_file(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                comment = self.post('', [chat_upload('секретный-чертёж.pdf'), image('тайный.png')], actor=self.colleague)
            events = board_events(publisher)
            self.assertEqual(
                [(event.data['change'], event.data['card_id']) for event in events],
                [('comment_added', self.card_obj.pk)],
            )
            for event in publisher.events:
                payload = json.dumps(event.as_dict(), ensure_ascii=False)
                self.assertNotIn('секретный', payload)
                self.assertNotIn('тайный', payload)
        notes = Notification.objects.filter(event_type=Notification.EventType.BOARD_CARD_COMMENT)
        self.assertEqual([note.recipient for note in notes], [self.member])
        for note in notes:
            self.assertNotIn('секретный', f'{note.title} {note.message}')
            self.assertNotIn('тайный', f'{note.title} {note.message}')
        self.assertEqual(comment.files.count(), 2)

    def test_the_log_line_names_no_file(self):
        with self.assertLogs('ecosystem.workflow', 'INFO') as logs:
            self.post('Лог', [chat_upload('секретный-чертёж.pdf')])
        line = next(entry for entry in logs.output if 'board.comment_posted' in entry)
        self.assertIn('file_count=1', line)
        self.assertNotIn('секретный', line)


@MEDIA
class DeleteFileTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Чертёж', assignees=[self.member])
        self.comment = post_card_comment(
            self.card_obj, actor=self.member, text='Файл', files=[chat_upload('схема.pdf')],
        )
        self.card_file = self.comment.files.get()
        self.path = self.card_file.file.path

    def test_the_uploader_deletes_it_and_the_row_stays(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(delete_card_file(self.card_file, actor=self.member))
            self.assertEqual(
                [(event.data['change'], event.data['card_id']) for event in board_events(publisher)],
                [('file_deleted', self.card_obj.pk)],
            )
        self.card_file.refresh_from_db()
        self.assertIsNotNone(self.card_file.deleted_at)
        self.assertEqual(self.card_file.deleted_by, self.member)
        self.assertEqual(self.card_file.file.name, '')
        self.assertEqual(self.card_file.original_name, 'схема.pdf')
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(BoardCardComment.objects.filter(pk=self.comment.pk).exists())

    def test_the_file_leaves_the_disk_only_after_the_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            delete_card_file(self.card_file, actor=self.member)
        self.assertTrue(os.path.exists(self.path))
        for callback in callbacks:
            callback()
        self.assertFalse(os.path.exists(self.path))

    def test_an_administrator_may_and_another_member_may_not(self):
        with self.assertRaisesMessage(BoardError, 'Удалить файл может тот, кто его прикрепил, или администратор.'):
            delete_card_file(self.card_file, actor=self.colleague)
        self.card_file.refresh_from_db()
        self.assertIsNone(self.card_file.deleted_at)
        self.assertTrue(delete_card_file(self.card_file, actor=self.admin))

    def test_an_uploader_taken_off_the_board_may_not(self):
        comment = post_card_comment(self.card_obj, actor=self.colleague, text='Мой', files=[chat_upload()])
        card_file = comment.files.get()
        remove_board_member(self.board, self.colleague, actor=self.owner)
        with self.assertRaises(BoardError):
            delete_card_file(card_file, actor=self.colleague)

    def test_an_archived_board_refuses(self):
        cancel_card(self.card_obj, actor=self.owner, reason='Не нужна')
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
            delete_card_file(self.card_file, actor=self.admin)
        self.assertTrue(os.path.exists(self.path))

    def test_twice_is_nothing_the_second_time(self):
        delete_card_file(self.card_file, actor=self.member)
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertFalse(delete_card_file(self.card_file, actor=self.member))
            self.assertEqual(publisher.events, [])

    def test_the_sync_revision_moves(self):
        token = build_sync_state(self.member)['revisions'][REVISION_BOARDS]
        delete_card_file(self.card_file, actor=self.member)
        self.assertNotEqual(build_sync_state(self.member)['revisions'][REVISION_BOARDS], token)

    def test_the_route(self):
        url = reverse('boards:file_delete', args=[self.board.pk, self.card_obj.pk, self.card_file.pk])
        chat = f'{board_url(self.board)}?card={self.card_obj.pk}&tab=chat'
        self.client.force_login(self.colleague)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.member)
        self.assertRedirects(self.client.get(url), chat, fetch_redirect_response=False)
        self.card_file.refresh_from_db()
        self.assertIsNone(self.card_file.deleted_at)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'{url}?chat=files')
        self.assertRedirects(
            response, f'{board_url(self.board)}?card={self.card_obj.pk}&tab=files', fetch_redirect_response=False,
        )
        self.card_file.refresh_from_db()
        self.assertIsNotNone(self.card_file.deleted_at)
        # Somebody who does not read the board: a 404, as for a download.
        with REAL_ACCESS:
            stranger = make_user('delete_stranger')
            self.client.force_login(stranger)
            self.assertEqual(self.client.post(url).status_code, 404)


@MEDIA
class DownloadTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Чертёж', assignees=[self.member])
        comment = post_card_comment(
            self.card_obj, actor=self.member, text='Файлы',
            # The type a browser sent is never what an image is served as.
            files=[chat_upload('схема.pdf'), chat_upload('снимок.png', PNG, 'text/html')],
        )
        self.pdf, self.png = comment.files.order_by('pk')
        self.other = self.card('Другая', assignees=[self.member])

    def url(self, card_file, name='boards:file_download', card=None):
        return reverse(name, args=[self.board.pk, (card or self.card_obj).pk, card_file.pk])

    def test_a_reader_downloads_it(self):
        self.client.force_login(self.colleague)
        response = self.client.get(self.url(self.pdf))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'%PDF-1.4 chat')
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertEqual(response['Content-Type'], 'application/pdf')

    def test_a_stranger_another_card_and_a_deleted_file_are_the_same_404(self):
        with REAL_ACCESS:
            stranger = make_user('download_stranger')
            self.client.force_login(stranger)
            self.assertEqual(self.client.get(self.url(self.pdf)).status_code, 404)
            self.assertEqual(self.client.get(self.url(self.png, 'boards:file_preview')).status_code, 404)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url(self.pdf, card=self.other)).status_code, 404)
        delete_card_file(self.pdf, actor=self.member)
        self.assertEqual(self.client.get(self.url(self.pdf)).status_code, 404)

    def test_the_preview_is_an_image_inline_whatever_was_stored(self):
        self.client.force_login(self.colleague)
        response = self.client.get(self.url(self.png, 'boards:file_preview'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['Content-Disposition'], 'inline')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Content-Security-Policy'], 'sandbox')
        self.assertEqual(b''.join(response.streaming_content), PNG)
        # Not an image: no preview.
        self.assertEqual(self.client.get(self.url(self.pdf, 'boards:file_preview')).status_code, 404)

    def test_the_log_names_identifiers_only(self):
        self.client.force_login(self.colleague)
        with self.assertLogs('ecosystem.attachments', 'INFO') as logs:
            self.client.get(self.url(self.pdf))
        self.assertTrue(any(f'board_card_file_id={self.pdf.pk}' in line for line in logs.output))
        self.assertFalse(any('схема' in line for line in logs.output))


@MEDIA
class ChatViewTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Чертёж', assignees=[self.member])
        self.task = task_of(self.card_obj)
        self.page = board_url(self.board)
        self.fragment = fragment_url(self.board)
        self.comment_url = reverse('boards:card_comment', args=[self.board.pk, self.card_obj.pk])
        self.client.force_login(self.member)

    def payload(self, **query):
        return self.client.get(self.fragment, {'card': self.card_obj.pk, 'tab': 'chat', **query}).json()

    def test_images_are_thumbnails_and_the_rest_rows(self):
        comment = post_card_comment(
            self.card_obj, actor=self.member, text='Вот', files=[chat_upload('схема.pdf'), image('фото.png')],
        )
        pdf, png = comment.files.order_by('pk')
        html = self.payload()['comments_html']
        preview = reverse('boards:file_preview', args=[self.board.pk, self.card_obj.pk, png.pk])
        self.assertIn(f'<img src="{preview}" alt="фото.png" loading="lazy">', html)
        self.assertIn(f'<a href="{preview}" target="_blank" rel="noopener"', html)
        row = html.split('class="board-chat__files"', 1)[1].split('</ul>', 1)[0]
        self.assertIn('board-file__badge--pdf', row)
        self.assertIn('title="схема.pdf">схема.pdf</span>', row)
        self.assertIn(reverse('boards:file_download', args=[self.board.pk, self.card_obj.pk, pdf.pk]), row)
        self.assertNotIn('<img', row)

    def test_the_cross_is_drawn_for_whoever_may_delete(self):
        comment = post_card_comment(self.card_obj, actor=self.member, text='Мой', files=[chat_upload()])
        card_file = comment.files.get()
        delete = reverse('boards:file_delete', args=[self.board.pk, self.card_obj.pk, card_file.pk])
        self.assertIn(delete, self.payload()['comments_html'])
        self.client.force_login(self.colleague)
        self.assertNotIn(delete, self.payload()['comments_html'])
        self.client.force_login(self.admin)
        self.assertIn(delete, self.payload()['comments_html'])

    def test_a_deleted_file_reads_as_deleted(self):
        comment = post_card_comment(
            self.card_obj, actor=self.member, text='Было', files=[chat_upload('схема.pdf'), image('фото.png')],
        )
        for card_file in comment.files.all():
            delete_card_file(card_file, actor=self.member)
        html = self.payload()['comments_html']
        messages = html.split('data-board-chat-view="files"', 1)[0]
        self.assertEqual(messages.count('Файл удалён'), 2)
        self.assertNotIn('схема.pdf', messages)
        self.assertNotIn('<img', messages)
        self.assertIn('Файлов пока нет', html.split('data-board-chat-view="files"', 1)[1])

    def test_older_task_attachments_stand_in_the_feed_by_time(self):
        post_card_comment(self.card_obj, actor=self.colleague, text='Первое сообщение')
        attachment = legacy_attachment(self.task, self.member, 'старый-акт.pdf')
        post_card_comment(self.card_obj, actor=self.colleague, text='Второе сообщение')
        first_at = BoardCardComment.objects.get(text='Первое сообщение').created_at
        TaskAttachment.objects.filter(pk=attachment.pk).update(created_at=first_at + timedelta(seconds=1))
        BoardCardComment.objects.filter(text='Второе сообщение').update(created_at=first_at + timedelta(seconds=2))
        html = self.payload()['comments_html'].split('data-board-chat-view="files"', 1)[0]
        order = [html.index(text) for text in ('Первое сообщение', 'старый-акт.pdf', 'Второе сообщение')]
        self.assertEqual(order, sorted(order))
        self.assertIn('добавлен файл к задаче', html)
        self.assertIn(reverse('tasks:download_attachment', args=[self.task.pk, attachment.pk]), html)
        self.assertIn(f'id="board-attachment-{attachment.pk}"', html)
        # Nothing was copied into the board's own files.
        self.assertFalse(BoardCardFile.objects.exists())

    def test_only_files_lists_every_file_newest_first(self):
        first = post_card_comment(self.card_obj, actor=self.colleague, text='Раз', files=[chat_upload('первый.pdf')])
        attachment = legacy_attachment(self.task, self.member, 'задача.pdf')
        second = post_card_comment(self.card_obj, actor=self.member, text='', files=[image('второй.png')])
        payload = self.payload(chat='files')
        files = payload['comments_html'].split('data-board-chat-view="files"', 1)[1]
        order = [files.index(name) for name in ('второй.png', 'задача.pdf', 'первый.pdf')]
        self.assertEqual(order, sorted(order))
        self.assertIn(f'#board-comment-{first.pk}"', files)
        self.assertIn(f'#board-comment-{second.pk}"', files)
        self.assertIn(f'#board-attachment-{attachment.pk}"', files)
        self.assertEqual(files.count('>К сообщению<'), 3)
        self.assertEqual(payload['files_count'], 3)
        self.assertEqual(payload['chat_mode'], 'files')
        page = self.client.get(self.page, {'card': self.card_obj.pk, 'tab': 'chat', 'chat': 'files'}).content.decode()
        self.assertIn('data-board-chat-mode="files"', page)
        self.assertIn('data-board-chat-files-count>3<', page)
        self.assertRegex(page, r'class="board-chat__mode is-active"[^>]*data-board-chat-mode-link="files"')
        self.assertEqual(
            page_attribute(page, 'data-board-page-url'), f'{self.page}?card={self.card_obj.pk}&tab=chat&chat=files',
        )

    def test_the_old_files_tab_opens_the_chat_on_its_files(self):
        for url in (
            f'{self.page}?card={self.card_obj.pk}&tab=files',
            reverse('tasks:detail', args=[self.task.pk]) + '?tab=files',
        ):
            with self.subTest(url=url):
                content = self.client.get(url, follow=True).content.decode()
                self.assertIn('data-board-tab="chat"', content)
                self.assertIn('data-board-chat-mode="files"', content)
                tabs = re.findall(r'data-board-tab-link="(\w+)"', content)
                self.assertEqual(tabs, ['description', 'chat', 'log'])

    def test_the_page_equals_the_fragment_in_both_modes(self):
        post_card_comment(self.card_obj, actor=self.member, text='С файлами', files=[chat_upload(), image()])
        legacy_attachment(self.task, self.member)
        for mode in ('', 'files'):
            with self.subTest(mode=mode):
                query = {'card': self.card_obj.pk, 'tab': 'chat', 'chat': mode}
                page = self.client.get(self.page, query).content.decode()
                payload = self.client.get(self.fragment, query).json()
                assert_page_matches_fragment(self, page, payload)
                self.assertEqual(payload['comments_revision'], content_revision(payload['comments_html']))

    def test_the_fingerprint_moves_with_a_file_added_and_deleted_and_the_panel_does_not(self):
        before = self.payload()
        comment = post_card_comment(self.card_obj, actor=self.colleague, text='', files=[chat_upload()])
        added = self.payload()
        self.assertNotEqual(before['comments_revision'], added['comments_revision'])
        self.assertEqual(before['panel_revision'], added['panel_revision'])
        delete_card_file(comment.files.get(), actor=self.colleague)
        deleted = self.payload()
        self.assertNotEqual(added['comments_revision'], deleted['comments_revision'])
        self.assertEqual(added['panel_revision'], deleted['panel_revision'])
        self.assertEqual((added['files_count'], deleted['files_count']), (1, 0))

    def test_the_tile_counts_live_chat_files_and_older_attachments(self):
        tile = re.compile(r'class="board-tile__files"[^>]*><span aria-hidden="true">📎</span>(\d+)<')
        self.assertIsNone(tile.search(self.payload()['columns_html']))
        comment = post_card_comment(self.card_obj, actor=self.member, text='', files=[chat_upload(), image()])
        legacy_attachment(self.task, self.member)
        self.assertEqual(tile.search(self.payload()['columns_html']).group(1), '3')
        delete_card_file(comment.files.first(), actor=self.member)
        self.assertEqual(tile.search(self.payload()['columns_html']).group(1), '2')

    def test_a_message_with_files_through_the_form(self):
        response = self.client.post(self.comment_url, {
            'text': 'Прикладываю', 'files': [chat_upload('схема.pdf'), image('экран.png')],
        })
        self.assertRedirects(
            response, f'{self.page}?card={self.card_obj.pk}&tab=chat', fetch_redirect_response=False,
        )
        comment = BoardCardComment.objects.get()
        self.assertEqual(sorted(comment.files.values_list('original_name', flat=True)), ['схема.pdf', 'экран.png'])
        response = self.client.post(self.comment_url, {'text': '', 'files': [image()]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BoardCardComment.objects.count(), 2)

    def test_a_refusal_keeps_the_text_and_asks_for_the_files_again(self):
        response = self.client.post(self.comment_url, {
            'text': 'Вот вирус', 'files': [chat_upload('вирус.exe', b'MZ')],
        })
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Файл «вирус.exe»: Недопустимый тип файла. Выберите файлы заново.', content)
        self.assertIn('>Вот вирус</textarea>', content)
        self.assertFalse(BoardCardComment.objects.exists())
        # Without files, nothing to choose again.
        content = self.client.post(self.comment_url, {'text': ' '}).content.decode()
        self.assertIn('Напишите сообщение или прикрепите файл.', content)
        self.assertNotIn('Выберите файлы заново', content)

    def test_the_form_is_multipart_and_works_without_javascript(self):
        content = self.client.get(self.page, {'card': self.card_obj.pk, 'tab': 'chat'}).content.decode()
        form = content.split('class="board-chat__form"', 1)[1].split('</form>', 1)[0]
        self.assertIn('enctype="multipart/form-data"', form)
        self.assertIn('type="file" name="files" multiple data-chat-files-input', form)
        self.assertIn('data-chat-files ', form)
        # A message of files alone is allowed, so the text is not required.
        textarea = re.search(r'<textarea id="board-comment-text"[^>]*>', form).group(0)
        self.assertNotIn('required', textarea)
        self.assertIn("js/board_chat_files.js", content)

    def test_the_log_names_the_files_and_marks_a_deleted_one(self):
        comment = post_card_comment(self.card_obj, actor=self.member, text='', files=[chat_upload('схема.pdf')])
        log = self.payload()['log_html']
        self.assertIn('Добавлен файл «схема.pdf»', log)
        delete_card_file(comment.files.get(), actor=self.member)
        self.assertIn('Добавлен файл «схема.pdf» (удалён)', self.payload()['log_html'])
        # Nothing about files is written into the journal.
        self.assertFalse(self.card_obj.events.exclude(kind='CREATED').exists())

    def _queries(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.page, {'card': self.card_obj.pk, 'tab': 'chat'})
        return len(queries)

    def test_the_query_count_does_not_grow_with_files_or_messages(self):
        # One of each first, so every right is asked at the baseline.
        post_card_comment(self.card_obj, actor=self.member, text='Первое')
        baseline = self._queries()
        post_card_comment(self.card_obj, actor=self.member, text='', files=[chat_upload()])
        legacy_attachment(self.task, self.member)
        self.assertEqual(self._queries(), baseline, 'one file')
        for index in range(19):
            post_card_comment(
                self.card_obj, actor=self.colleague if index % 2 else self.member,
                text=f'Сообщение {index}', files=[chat_upload(f'{index}.pdf')] if index % 3 else [image()],
            )
        legacy_attachment(self.task, self.member, 'ещё.pdf')
        self.assertEqual(BoardCardFile.objects.count(), 20)
        self.assertEqual(self._queries(), baseline, 'twenty files')


@MEDIA
class TaskAttachmentRefusalTests(BoardFixtureMixin, TestCase):
    """`tasks:add_attachment` creates nothing for a `BOARD` task and leads to
    its «Чат»; completing a card needs no file."""

    def test_completion_asks_for_no_file(self):
        card = self.card('Без файлов', assignees=[self.member])
        task = task_of(card)
        self.assertFalse(task.requires_attachment)
        complete_task(task, self.member, 'Сделано без файлов')
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')


class DrawerMarkupTests(BoardFixtureMixin, TestCase):
    """The order of «Описание» in the markup is the order on screen, and
    «Подписчики» are outside the guarded panel."""

    def test_description_checklist_facts_followers_in_this_order(self):
        card = self.card('Порядок', assignees=[self.member])
        self.client.force_login(self.member)
        drawer = self.client.get(fragment_url(self.board), {'card': card.pk}).json()['drawer_html']
        order = [
            drawer.index(marker) for marker in (
                'data-live-board-card', 'data-live-board-checklist', 'class="board-checklist__add"',
                'data-live-board-facts', 'data-live-board-followers', 'data-board-tab-body="chat"',
            )
        ]
        self.assertEqual(order, sorted(order))
        self.assertLess(drawer.index('Описания нет.'), drawer.index('data-live-board-checklist'))
        self.assertLess(drawer.index('data-live-board-checklist'), drawer.index('>Редактировать<'))

    def test_the_stylesheet_reorders_nothing_on_the_description(self):
        from django.conf import settings

        css = open(os.path.join(settings.BASE_DIR, 'static', 'css', 'boards.css'), encoding='utf-8').read()
        pane = css.split('/* ---- «Описание»: one pane', 1)[1].split('/* ----', 1)[0]
        self.assertNotRegex(pane, r'\border\s*:')

    def test_a_follower_added_while_a_result_is_typed_raises_no_banner(self):
        card = self.card('Подписка', assignees=[self.member])
        add_board_members(self.board, [self.outsider.pk], actor=self.owner)
        self.client.force_login(self.member)
        before = self.client.get(fragment_url(self.board), {'card': card.pk}).json()
        post_card_comment(card, actor=self.colleague, text='@Посмотри', mentions=[self.outsider.pk])
        after = self.client.get(fragment_url(self.board), {'card': card.pk}).json()
        self.assertEqual(before['panel_revision'], after['panel_revision'])
        self.assertNotEqual(before['followers_revision'], after['followers_revision'])
