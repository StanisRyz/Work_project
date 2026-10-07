"""Shared fixtures for the board tests."""

import itertools
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile

from ..models import BoardColumn, SubBoard
from ..services import create_board, create_card


_CODES = itertools.count(1)


def fresh_code():
    """A board code no other board of this test run has: «T1», «T2», …"""
    return f'T{next(_CODES)}'


# The default columns of a sub-board by the names the old fixed stages had —
# so a test written against «Сделать / В работе / На проверке / Готово»
# still says what it means.
STAGE_INDEX = {'TODO': 0, 'IN_PROGRESS': 1, 'REVIEW': 2}


def main_sub_board(board):
    """The board's first tab — «Основная» for a board `create_board(code=fresh_code(), )` made."""
    return SubBoard.objects.filter(board=board).order_by('position', 'pk').first()


def column_of(board, stage='TODO', sub_board=None):
    """The working column standing where the old `stage` stood (by place)."""
    sub_board = sub_board or main_sub_board(board)
    working = list(
        BoardColumn.objects.filter(sub_board=sub_board, is_done=False).order_by('position', 'pk')
    )
    return working[STAGE_INDEX[stage]]


def done_column_of(board, sub_board=None):
    return BoardColumn.objects.get(sub_board=sub_board or main_sub_board(board), is_done=True)


def stage_of(card):
    """The old stage name of the default column a card stands in."""
    card.refresh_from_db()
    for stage in STAGE_INDEX:
        if column_of(card.board, stage, card.sub_board).pk == card.column_id:
            return stage
    return None


def expected_counts(board, sub_board=None, *, TODO=0, IN_PROGRESS=0, REVIEW=0, DONE=0):
    """`column_counts()` as it should read, keyed by column id."""
    return {
        str(column_of(board, 'TODO', sub_board).pk): TODO,
        str(column_of(board, 'IN_PROGRESS', sub_board).pk): IN_PROGRESS,
        str(column_of(board, 'REVIEW', sub_board).pk): REVIEW,
        str(done_column_of(board, sub_board).pk): DONE,
    }


def board_url(board, sub_board=None):
    """The page of a board's sub-board (its first one by default)."""
    sub_board = sub_board or main_sub_board(board)
    return reverse('boards:sub_board', args=[board.pk, sub_board.pk])


def fragment_url(board, sub_board=None):
    sub_board = sub_board or main_sub_board(board)
    return reverse('boards:fragment', args=[board.pk, sub_board.pk])


# The live blocks of a sub-board page: the markup each one is drawn from in
# the fragment, and the `data-*-revision` attribute the page carries for it.
# The card panel is one block in three containers, with one fingerprint.
LIVE_BLOCKS = {
    'tabs': ('tabs_html',),
    'columns': ('columns_html',),
    'panel': ('panel_html', 'card_html', 'facts_html'),
    'comments': ('comments_html',),
    'log': ('log_html',),
}

CSRF_INPUT = re.compile(r'<input\b[^>]*\bname="csrfmiddlewaretoken"[^>]*>')

# The panel heading's «Следить» / «Вы следите» form — every reader's own.
FOLLOW_FORM = re.compile(r'<form class="board-follow".*?</form>', re.S)


def page_attribute(content, name):
    """The value of the first `name="…"` in `content`, `&amp;` decoded."""
    match = re.search(rf'{name}="([^"]*)"', content)
    return match.group(1).replace('&amp;', '&') if match else None


def assert_page_matches_fragment(test, page, fragment, blocks=LIVE_BLOCKS):
    """Every live block of the fragment is in the page, under the page's fingerprint."""
    page_markup = CSRF_INPUT.sub('', page)
    for block, keys in blocks.items():
        with test.subTest(block=block):
            for key in keys:
                test.assertIn(CSRF_INPUT.sub('', fragment[key]), page_markup)
            test.assertEqual(page_attribute(page, f'data-{block}-revision'), fragment[f'{block}_revision'])


def card_create_url(board, sub_board=None):
    sub_board = sub_board or main_sub_board(board)
    return reverse('boards:card_create', args=[board.pk, sub_board.pk])


def new_card(board, actor, title='Карточка', *, assignees, stage='TODO', sub_board=None,
             column=None, **extra):
    """`create_card()` on a board's sub-board, in `column` — or in the column
    the old `stage` named."""
    sub_board = sub_board or (column.sub_board if column is not None else main_sub_board(board))
    return create_card(
        sub_board,
        actor=actor,
        title=title,
        due_date=extra.pop('due_date', due()),
        assignee_ids=[getattr(user, 'pk', user) for user in assignees],
        column=column if column is not None else column_of(board, stage, sub_board),
        **extra,
    )


def legacy_attachment(task, user, name='акт.pdf', content=b'%PDF-1.4 legacy', content_type='application/pdf'):
    """A `TaskAttachment` of a board card's task, as one added before files
    moved into «Чат» — written directly, since `tasks:add_attachment` and
    `add_task_attachment()` refuse a `BOARD` task now. The test sets
    `MEDIA_ROOT`."""
    from django.core.files.base import ContentFile

    from tasks.models import TaskAttachment

    attachment = TaskAttachment(
        task=task, uploaded_by=user, original_name=name, file_size=len(content), content_type=content_type,
    )
    attachment.file.save(name, ContentFile(content), save=False)
    attachment.save()
    return attachment


def chat_upload(name='схема.pdf', content=b'%PDF-1.4 chat', content_type='application/pdf'):
    """One file as a browser posts it with a message of «Чат»."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content, content_type=content_type)


def department():
    # Seeded by `accounts.0003`; reused rather than duplicated.
    return Department.objects.get_or_create(code='PDO', defaults={'name': 'ПДО'})[0]


def make_user(username, role=UserProfile.Role.OTK, *, superuser=False):
    if superuser:
        user = User.objects.create_superuser(username=username, password='demo12345')
    else:
        user = User.objects.create_user(username=username, password='demo12345')
    profile = user.userprofile
    profile.role = role
    profile.department = department()
    profile.save()
    return user


def due(days=5):
    return timezone.localdate() + timedelta(days=days)


# Every role there is: board access as it will be once widened.
ALL_ROLES = frozenset(UserProfile.Role.values)


class WidenedBoardAccess:
    """Full board access open to every role for the whole test class.

    Full access — every board, creating one — is the administrator's only for
    now (`boards.permissions.BOARD_ACCESS_ROLES`); the board tests keep ПДО as
    a creator and owner and read boards as an outsider, so they run with full
    access widened — which is also what keeps the behaviour after widening
    covered. Patched before `setUpTestData()`, which already creates boards.
    `boards/tests/test_access.py` runs with the real constant: full access and
    reading by membership.
    """

    @classmethod
    def setUpClass(cls):
        patcher = mock.patch('boards.permissions.BOARD_ACCESS_ROLES', ALL_ROLES)
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        super().setUpClass()


class BoardFixtureMixin(WidenedBoardAccess):
    """An ПДО-owned board with two ordinary members and one outsider."""

    @classmethod
    def setUpTestData(cls):
        cls.department = department()
        cls.owner = make_user('pdo_owner', UserProfile.Role.PDO)
        cls.member = make_user('member_one', UserProfile.Role.OTK)
        cls.colleague = make_user('member_two', UserProfile.Role.TO)
        cls.outsider = make_user('outsider', UserProfile.Role.OTK)
        cls.admin = make_user('admin_user', UserProfile.Role.ADMIN)
        cls.board = create_board(
            code=fresh_code(),
            name='Планирование',
            department=cls.department,
            owner=cls.owner,
            actor=cls.owner,
            member_ids=[cls.member.pk, cls.colleague.pk],
        )
        cls.main = main_sub_board(cls.board)

    def card(self, title='Согласовать график', *, assignees=None, stage='TODO', actor=None, **extra):
        return new_card(
            self.board, actor or self.member, title,
            assignees=assignees or [self.member], stage=stage, **extra,
        )

    def column(self, stage='TODO'):
        return column_of(self.board, stage)

    def page_url(self):
        return board_url(self.board)
