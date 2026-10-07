"""Stage 19 on PostgreSQL, after `seed_demo.py`: files of «Чат» through the
services and the selectors — the message with files, the refusals, the
rollback, the tombstone, «📎 N» and «Только файлы».

    python manage.py shell < tasksandreports/boards/reports/stage-19/pg_checks.py
"""
import os
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from boards.models import BoardCard, BoardCardComment, BoardCardFile
from boards.selectors import build_board_state
from boards.services import BoardError, delete_card_file, post_card_comment
from tasks.models import Task, TaskAttachment

admin, ivanov, petrova = (User.objects.get(username=name) for name in ('admin1', 'ivanov', 'petrova'))
card = BoardCard.objects.get(board__code='ZAP', number=1)
sub_board = card.sub_board


def upload(name, content=b'%PDF-1.4 pg', kind='application/pdf'):
    return SimpleUploadedFile(name, content, content_type=kind)


comment = post_card_comment(card, actor=ivanov, text='Чертёж и фото',
                            files=[upload('чертёж.pdf'), upload('фото.png', b'\x89PNG pg', 'image/png')])
print('сообщение с файлами:', [(f.original_name, f.size, f.file.name.split('/')[:3]) for f in comment.files.order_by('pk')])
only = post_card_comment(card, actor=petrova, text='', files=[upload('скриншот-2026-10-07-101500.png', b'\x89PNG', 'image/png')])
print('только файл, текст:', repr(only.text), 'файлов:', only.files.count())
for label, call in (
    ('пустое', lambda: post_card_comment(card, actor=ivanov, text=' ')),
    ('11 файлов', lambda: post_card_comment(card, actor=ivanov, text='x', files=[upload(f'{i}.pdf') for i in range(11)])),
    ('exe', lambda: post_card_comment(card, actor=ivanov, text='x', files=[upload('вирус.exe')])),
):
    try:
        call()
    except BoardError as exc:
        print(f'{label}: {exc}')
before = (BoardCardComment.objects.count(), BoardCardFile.objects.count())
try:
    with mock.patch('notifications.services.notify_board_card_comment', side_effect=RuntimeError('сбой')):
        post_card_comment(card, actor=ivanov, text='сбой', files=[upload('сбой.pdf')])
except RuntimeError:
    pass
print('после сбоя строк столько же:', before == (BoardCardComment.objects.count(), BoardCardFile.objects.count()))
pdf = comment.files.get(original_name='чертёж.pdf')
path = pdf.file.path
try:
    delete_card_file(pdf, actor=petrova)
except BoardError as exc:
    print('чужой файл:', exc)
with transaction.atomic():
    print('удалено:', delete_card_file(pdf, actor=ivanov))
pdf.refresh_from_db()
print('надгробие:', pdf.deleted_at is not None, pdf.deleted_by.username, repr(pdf.file.name), 'файл на диске:', os.path.exists(path))
print('повтор:', delete_card_file(pdf, actor=ivanov))
state = build_board_state(card.board, sub_board, admin, card_id=card.pk)
tile = next(item for column in state['columns'] for item in column['cards'] if item['card'].pk == card.pk)
print('📎 на плитке (живые файлы чата + вложение задачи):', tile['file_count'],
      '=', BoardCardFile.objects.filter(card=card, deleted_at__isnull=True).count(), '+',
      TaskAttachment.objects.filter(task__board_card=card).count())
panel = state['card']
print('«Только файлы»:', [row['name'] for row in panel['files']])
print('лента:', [row['kind'] for row in panel['feed']])
with CaptureQueriesContext(connection) as queries:
    build_board_state(card.board, sub_board, admin, card_id=card.pk)
small = len(queries)
for index in range(15):
    post_card_comment(card, actor=ivanov, text=f'Сообщение {index}', files=[upload(f'{index}.pdf')])
with CaptureQueriesContext(connection) as queries:
    build_board_state(card.board, sub_board, admin, card_id=card.pk)
print('запросов build_board_state с карточкой: до', small, 'после 15 сообщений с файлами', len(queries))
