"""The one way a board, its members and its cards are written.

Every function here is one `atomic()` block. The lock order is fixed —
`Board.select_for_update()` first, then the card, then its task — and the
permission and the state are re-checked *after* the locks, so a stale tab or a
double click is refused rather than raced. Locks are taken without
`select_related()`, so a joined `SELECT … FOR UPDATE` never locks shared
reference rows.

A card's work is an ordinary `tasks.Task` (`source_type=BOARD`): it is created
by `tasks.services.create_board_card_task()` in the same transaction as the
card, its wording and deadline change through `update_board_card_task()`, its
assignees through `replace_task_assignees()`, and it is finished by the
ordinary `complete_task()`. This module owns the decision to do each of
those; `tasks.services` owns the task.

Every successful write that changed something announces itself with exactly
one `board.updated` (`realtime.emitters.emit_board_updated()`), from inside
its own `atomic()` block, so the event is published after the commit and a
refusal or a rollback publishes nothing. A write that stored nothing — an edit
that changes no field, a drop where the card already stood — is not a change
and says nothing. A board's task changed elsewhere (an attachment added or
removed) is the task's own `task.*` event and the `boards` sync revision, never
a `board.updated`.

Every change of a card is also one entry of its journal (`BoardCardEvent`,
written by `_record()` in the same transaction): created, edited (which
fields), moved (the columns' names as they were), completed, reopened,
cancelled. A write that stored nothing records nothing, and a rollback takes
the entry with it. Completing and reopening a card's task go through
`complete_card()`/`reopen_card()` only — `tasks:complete` and `tasks:reopen`
refuse a `BOARD` task — so the journal misses none of them.

A card is named by its board's code and its own number («ZAP-12»):
`clean_board_code()` is the one normaliser of a code, `create_card()` gives
the next number under the board lock. A working column may carry pinned
people (`set_column_pins()`), whom a card created in it or moved into it gets
through `_apply_pins()` in the same transaction; `move_card()` takes a
working column of any sub-board of the card's own board.

A board has its own card fields («Поля карточек»: text, number, date, a list
of coloured options), set up by its manager — `create_field()` and the rest,
one `structure_changed` each — and filled in by `create_card()`/
`update_card()` through `field_values`, parsed by kind in
`_clean_field_values()`. A field or an option a card holds a value of is
archived, never deleted.
"""

import datetime
import logging
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from ecosystem.logging_utils import log_event
from realtime.emitters import emit_board_updated
from realtime.events import (
    BOARD_CHANGE_BOARD_ARCHIVED,
    BOARD_CHANGE_BOARD_RESTORED,
    BOARD_CHANGE_CARD_CANCELLED,
    BOARD_CHANGE_CARD_COMPLETED,
    BOARD_CHANGE_CARD_CREATED,
    BOARD_CHANGE_CARD_MOVED,
    BOARD_CHANGE_CARD_REOPENED,
    BOARD_CHANGE_CARD_UPDATED,
    BOARD_CHANGE_COMMENT_ADDED,
    BOARD_CHANGE_MEMBERS_CHANGED,
    BOARD_CHANGE_STRUCTURE_CHANGED,
)

from .columns import DEFAULT_COLUMNS, MAX_COLUMNS
from .models import (
    STALE_DAYS_MAX,
    STALE_DAYS_MIN,
    BOARD_CODE_MAX_LENGTH,
    BOARD_CODE_MIN_LENGTH,
    BOARD_CODE_PATTERN,
    Board,
    BoardCard,
    BoardCardComment,
    BoardCardEvent,
    BoardCardFieldValue,
    BoardColumn,
    BoardColumnPin,
    BoardField,
    BoardFieldColor,
    BoardFieldOption,
    BoardMember,
    SubBoard,
)
from .permissions import (
    active_employee_q,
    can_cancel_card,
    can_comment_card,
    can_create_board,
    can_manage_board,
    can_restore_board,
    can_work_on_board,
)


logger = logging.getLogger('ecosystem.workflow')

# Positions within a column are spaced this far apart, so a card dropped
# between two neighbours usually takes the midpoint and writes one row.
POSITION_STEP = 1024
# The ceiling of a PostgreSQL `integer`, which is what `PositiveIntegerField`
# is there: a column whose end would pass it is renumbered first.
MAX_POSITION = 2_147_483_647
TITLE_MAX_LENGTH = 200
COMMENT_MAX_LENGTH = 4000
SUB_BOARD_NAME_MAX_LENGTH = 100
COLUMN_NAME_MAX_LENGTH = 60
DEFAULT_SUB_BOARD_NAME = 'Основная'


ARCHIVED_MESSAGE = 'Доска в архиве — изменить её нельзя.'


class BoardError(Exception):
    """A refused board operation; the message is meant for the user."""


class StaleCardError(BoardError):
    """The card was edited by somebody else after this edit form was drawn."""


class BoardCodeError(BoardError):
    """A board code refused: badly formed, or taken by another board."""


def _rejected(operation, reason, *, actor, board_id=None, card_id=None):
    log_event(
        logger,
        'INFO',
        'board.operation_rejected',
        operation=operation,
        board_id=board_id,
        board_card_id=card_id,
        actor_user_id=getattr(actor, 'pk', None),
        reason=reason,
        outcome='rejected',
    )


def _refuse_archived(operation, board, *, actor, card_id=None):
    """Every write to an archived board stops here, before the right is asked.

    `boards.permissions` already refuses it; this only gives the refusal its
    own sentence instead of «недоступна».
    """
    if board.is_archived:
        _rejected(operation, 'board_archived', actor=actor, board_id=board.pk, card_id=card_id)
        raise BoardError(ARCHIVED_MESSAGE)


def _lock_board(board_id):
    return Board.objects.select_for_update().get(pk=board_id)


def _lock_card(card, board):
    """The card, locked, and only if it really is on `board`."""
    try:
        return BoardCard.objects.select_for_update().get(pk=card.pk, board=board)
    except BoardCard.DoesNotExist as exc:
        raise BoardError('Карточка не найдена на этой доске.') from exc


def _lock_card_task(card):
    from tasks.models import Task

    try:
        return Task.objects.select_for_update().get(
            source_type=Task.SourceType.BOARD, board_card=card,
        )
    except Task.DoesNotExist as exc:
        raise BoardError('У карточки нет задачи.') from exc


def _active_users(user_ids):
    """`{pk: user}` for those among `user_ids` who may be on a board at all.

    Any active employee: an id sent by hand for anybody else is left out, and
    the caller refuses the request.
    """
    ids = {int(getattr(value, 'pk', value)) for value in user_ids}
    if not ids:
        return {}
    return {
        user.pk: user
        for user in get_user_model().objects.filter(active_employee_q(), pk__in=ids)
    }


def _users(user_ids):
    return list(get_user_model().objects.filter(pk__in=user_ids).order_by('pk'))


def _clean_title(title):
    title = (title or '').strip()
    if not title:
        raise BoardError('Укажите заголовок карточки.')
    if len(title) > TITLE_MAX_LENGTH:
        raise BoardError(f'Заголовок карточки — не длиннее {TITLE_MAX_LENGTH} символов.')
    return title


def _clean_assignees(board, assignee_ids):
    """Sorted ids of the requested исполнители, each an active board member."""
    ids = sorted({int(getattr(value, 'pk', value)) for value in assignee_ids or ()})
    if not ids:
        raise BoardError('Укажите хотя бы одного исполнителя.')
    member_ids = set(
        BoardMember.objects.filter(
            active_employee_q('user__'), board=board, user_id__in=ids,
        ).values_list('user_id', flat=True)
    )
    if set(ids) - member_ids:
        raise BoardError('Исполнителями могут быть только активные участники доски.')
    return ids


def compose_task_text(title, description):
    """`Task.task_text` for a card — the one place it is composed.

    The title alone, or the title, an empty line and the description.
    """
    title = (title or '').strip()
    description = (description or '').strip()
    if not description:
        return title
    return f'{title}\n\n{description}'


# --------------------------------------------------------------------------
# Boards and members
# --------------------------------------------------------------------------


def _record(card, kind, *, actor, **details):
    """One journal entry of `card`, inside the caller's transaction.

    `details` are identifiers and names of columns as they are now — never
    the card's text, a reason or a message.
    """
    BoardCardEvent.objects.create(card=card, actor=actor, kind=kind, details=details)


def _column_snapshot(prefix, column):
    """`{prefix}_id`/`{prefix}` of a column, its name as it reads today."""
    if column is None:
        return {f'{prefix}_id': None, prefix: ''}
    return {f'{prefix}_id': column.pk, prefix: column.name}


def clean_board_code(code):
    """A board's code as it is stored: trimmed, upper case, checked.

    Two to six letters (Latin or Cyrillic) and digits — «zap» becomes «ZAP».
    The one normaliser: storing upper case is what makes the plain unique
    constraint uniqueness whatever the case was typed in (`str.upper()` folds
    Cyrillic everywhere, unlike SQLite's `UPPER()`).
    """
    code = (code or '').strip().upper()
    if not code:
        raise BoardCodeError('Укажите код доски.')
    if not BOARD_CODE_PATTERN.match(code):
        raise BoardCodeError(
            f'Код доски — от {BOARD_CODE_MIN_LENGTH} до {BOARD_CODE_MAX_LENGTH} '
            'букв (латиница или кириллица) и цифр, без пробелов и знаков.'
        )
    return code


def _refuse_taken_code(code, *, exclude_pk=None, operation, actor):
    others = Board.objects.filter(code=code)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    if others.exists():
        _rejected(operation, 'code_taken', actor=actor, board_id=exclude_pk)
        raise _code_taken_error(code)


def _code_taken_error(code):
    return BoardCodeError(f'Код «{code}» уже занят другой доской.')


def create_board(*, name, code, owner, actor, member_ids=(), department=None, description=''):
    """A new board; its owner is always one of its members.

    It starts with one sub-board, «Основная», and its `DEFAULT_COLUMNS`.
    `code` («ZAP») is required — `clean_board_code()` — and unique among all
    boards whatever the case; every card number of the board starts with it.

    `department` and `description` are kept for the boards that carry them;
    the form asks for neither, and a new board names no department.
    """
    if not can_create_board(actor):
        _rejected('create_board', 'not_permitted', actor=actor)
        raise BoardError('Создание доски недоступно.')
    name = (name or '').strip()
    if not name:
        raise BoardError('Укажите название доски.')
    if len(name) > 200:
        raise BoardError('Название доски — не длиннее 200 символов.')
    code = clean_board_code(code)
    _refuse_taken_code(code, operation='create_board', actor=actor)
    requested = {owner.pk, *(int(getattr(value, 'pk', value)) for value in member_ids)}
    users = _active_users(requested)
    if owner.pk not in users:
        raise BoardError('Владельцем доски может быть только активный сотрудник.')
    if set(requested) - set(users):
        _rejected('create_board', 'ineligible_member', actor=actor)
        raise BoardError('Участниками доски могут быть только активные сотрудники.')
    try:
        with transaction.atomic():
            board = Board.objects.create(
                name=name,
                code=code,
                description=(description or '').strip(),
                department=department,
                owner=owner,
            )
            BoardMember.objects.bulk_create(
                [BoardMember(board=board, user_id=user_id, added_by=actor) for user_id in sorted(users)]
            )
            _create_sub_board_rows(board, DEFAULT_SUB_BOARD_NAME, position=1, actor=actor)
    except IntegrityError as exc:
        # Taken by a board created at the same moment: `unique_board_code`.
        _rejected('create_board', 'code_taken', actor=actor)
        raise _code_taken_error(code) from exc
    log_event(
        logger,
        'INFO',
        'board.created',
        board_id=board.pk,
        actor_user_id=actor.pk,
        member_count=len(users),
        outcome='ok',
    )
    return board


BOARD_NAME_MAX_LENGTH = 200


def rename_board(board, *, actor, name):
    """A new name for a board: its owner or an administrator, never archived.

    Trimmed, required, at most `BOARD_NAME_MAX_LENGTH`. The same name changes
    nothing and announces nothing; a new one publishes one
    `board.updated(structure_changed)` — the name is drawn by every page of
    the board.
    """
    with transaction.atomic():
        board = _lock_board(board.pk)
        _refuse_archived('rename_board', board, actor=actor)
        if not can_manage_board(actor, board):
            _rejected('rename_board', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Переименовать доску может её владелец или администратор.')
        name = _clean_name(name, max_length=BOARD_NAME_MAX_LENGTH, what='доски')
        if name == board.name:
            return board
        board.name = name
        board.save(update_fields=['name', 'updated_at'])
        _structure_changed(board, 'board.renamed', actor=actor)
    return board


def change_board_code(board, *, actor, code):
    """A new code for a board: its owner or an administrator, never archived.

    `clean_board_code()`, unique among all boards whatever the case. Every
    card of the board is named by it, so «ZAP-12» reads «ZAK-12» afterwards —
    the numbers stay. The same code changes nothing and announces nothing; a
    new one publishes one `board.updated(structure_changed)`.
    """
    try:
        with transaction.atomic():
            board = _lock_board(board.pk)
            _refuse_archived('change_board_code', board, actor=actor)
            if not can_manage_board(actor, board):
                _rejected('change_board_code', 'not_permitted', actor=actor, board_id=board.pk)
                raise BoardError('Код доски меняет её владелец или администратор.')
            code = clean_board_code(code)
            if code == board.code:
                return board
            _refuse_taken_code(code, exclude_pk=board.pk, operation='change_board_code', actor=actor)
            board.code = code
            board.save(update_fields=['code', 'updated_at'])
            # Every tile of every sub-board draws the code: the `boards` sync
            # revision reads the sub-boards' `updated_at`, so a reader who
            # missed the event still redraws.
            SubBoard.objects.filter(board=board).update(updated_at=timezone.now())
            _structure_changed(board, 'board.code_changed', actor=actor)
    except IntegrityError as exc:
        _rejected('change_board_code', 'code_taken', actor=actor, board_id=board.pk)
        raise _code_taken_error(code) from exc
    return board


def add_board_members(board, user_ids, *, actor):
    """Add active employees to the board; those already on it are skipped.

    Returns the list of user ids actually added.
    """
    with transaction.atomic():
        board = _lock_board(board.pk)
        _refuse_archived('add_members', board, actor=actor)
        if not can_manage_board(actor, board):
            _rejected('add_members', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Управление участниками доски недоступно.')
        requested = {int(getattr(value, 'pk', value)) for value in user_ids}
        if not requested:
            raise BoardError('Не выбраны сотрудники.')
        users = _active_users(requested)
        if requested - set(users):
            _rejected('add_members', 'ineligible_member', actor=actor, board_id=board.pk)
            raise BoardError('Участниками доски могут быть только активные сотрудники.')
        existing = set(
            BoardMember.objects.filter(board=board, user_id__in=requested)
            .values_list('user_id', flat=True)
        )
        added = sorted(requested - existing)
        BoardMember.objects.bulk_create(
            [BoardMember(board=board, user_id=user_id, added_by=actor) for user_id in added]
        )
        if added:
            board.save(update_fields=['updated_at'])
            emit_board_updated(board.pk, BOARD_CHANGE_MEMBERS_CHANGED)
    log_event(
        logger,
        'INFO',
        'board.members_added',
        board_id=board.pk,
        actor_user_id=actor.pk,
        added_count=len(added),
        outcome='ok',
    )
    return added


def remove_board_member(board, user, *, actor):
    """Take one person off the board.

    Never the owner, and never someone who is still an исполнитель of an open
    card here — that card would be left with a person who may no longer work
    on the board. Reassign it first.

    Their pins (`BoardColumn.pinned_assignees`) go with them, in every column
    of the board and in the same transaction: a column must never put a card
    on somebody who is no longer on the board.
    """
    from tasks.models import Task

    with transaction.atomic():
        board = _lock_board(board.pk)
        _refuse_archived('remove_member', board, actor=actor)
        if not can_manage_board(actor, board):
            _rejected('remove_member', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Управление участниками доски недоступно.')
        if user.pk == board.owner_id:
            _rejected('remove_member', 'owner', actor=actor, board_id=board.pk)
            raise BoardError('Владельца доски нельзя исключить из участников.')
        membership = BoardMember.objects.filter(board=board, user=user).first()
        if membership is None:
            raise BoardError('Сотрудник не является участником доски.')
        holds_open_card = Task.objects.filter(
            source_type=Task.SourceType.BOARD,
            board_card__board=board,
            status__code='IN_PROGRESS',
            assignees__user=user,
        ).exists()
        if holds_open_card:
            _rejected('remove_member', 'open_card_assignee', actor=actor, board_id=board.pk)
            raise BoardError(
                'Сотрудник — исполнитель открытой карточки этой доски. '
                'Сначала переназначьте карточку.'
            )
        membership.delete()
        pins = BoardColumnPin.objects.filter(column__sub_board__board=board, user=user)
        pinned_columns = list(pins.values_list('column_id', flat=True))
        if pinned_columns:
            pins.delete()
            # The column headers draw the pins: the `boards` sync revision
            # reads the columns' `updated_at`.
            BoardColumn.objects.filter(pk__in=pinned_columns).update(updated_at=timezone.now())
        board.save(update_fields=['updated_at'])
        emit_board_updated(board.pk, BOARD_CHANGE_MEMBERS_CHANGED)
    log_event(
        logger,
        'INFO',
        'board.member_removed',
        board_id=board.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------


def _sub_board_columns(sub_board):
    """The sub-board's columns in order — read under the board lock."""
    return list(BoardColumn.objects.filter(sub_board=sub_board).order_by('position', 'pk'))


def _working_column(sub_board, column, *, operation, actor, card_id=None):
    """`column` (an object or an id) as a working column of `sub_board`.

    Refused when it is the closing column — reached only by completing the
    task — or not a column of this sub-board at all: deleted meanwhile, or of
    another sub-board, which a card never moves to.
    """
    column_id = getattr(column, 'pk', column)
    try:
        column_id = int(column_id)
    except (TypeError, ValueError):
        column_id = None
    found = (
        BoardColumn.objects.filter(pk=column_id, sub_board=sub_board).first()
        if column_id is not None else None
    )
    if found is None:
        _rejected(operation, 'unknown_column', actor=actor, board_id=sub_board.board_id, card_id=card_id)
        raise BoardError('Колонка не найдена на этой поддоске — возможно, её удалили. Обновите страницу.')
    if found.is_done:
        _rejected(operation, 'done_column', actor=actor, board_id=sub_board.board_id, card_id=card_id)
        raise BoardError(
            f'В колонку «{found.name}» карточка попадает, когда её задача выполнена: '
            'завершите задачу с результатом.'
        )
    return found


def _first_working_id(sub_board):
    return (
        BoardColumn.objects.filter(sub_board=sub_board, is_done=False)
        .order_by('position', 'pk').values_list('pk', flat=True).first()
    )


def _in_column_q(column, first_working_id):
    """The cards standing in `column`: those naming it, and — for the first
    working column — those whose column was deleted (`column` NULL)."""
    condition = Q(column=column)
    if column.pk == first_working_id:
        condition |= Q(column__isnull=True)
    return condition


def _column(column, *, exclude_card_id=None):
    """The cards stored in one working column, in order.

    Every card standing there, done ones included: a completed card keeps its
    place so that reopening it puts it back where it was.
    """
    cards = BoardCard.objects.filter(
        _in_column_q(column, _first_working_id(column.sub_board_id)),
        sub_board_id=column.sub_board_id,
    )
    if exclude_card_id is not None:
        cards = cards.exclude(pk=exclude_card_id)
    return list(cards.order_by('position', 'pk'))


def _renumber(cards):
    """Respace `cards` (already in their new order) at `POSITION_STEP`."""
    for index, card in enumerate(cards, start=1):
        card.position = index * POSITION_STEP
    BoardCard.objects.bulk_update(cards, ['position'])


def _end_position(column):
    """The position after the last card of a column, renumbering if needed."""
    last = (
        BoardCard.objects.filter(
            _in_column_q(column, _first_working_id(column.sub_board_id)),
            sub_board_id=column.sub_board_id,
        )
        .aggregate(last=Max('position'))['last']
    ) or 0
    if last + POSITION_STEP <= MAX_POSITION:
        return last + POSITION_STEP
    cards = _column(column)
    _renumber(cards)
    return (len(cards) + 1) * POSITION_STEP


def _sub_board_of(board, sub_board, *, operation, actor):
    """`sub_board` (an object or an id), re-read and only if it is on `board`."""
    sub_board_id = getattr(sub_board, 'pk', sub_board)
    found = SubBoard.objects.filter(pk=sub_board_id, board=board).first()
    if found is None:
        _rejected(operation, 'unknown_sub_board', actor=actor, board_id=board.pk)
        raise BoardError('Поддоска не найдена на этой доске — возможно, её удалили.')
    return found


def _pinned_ids(column, board):
    """The column's pinned people who are still active members of `board`."""
    return set(
        BoardColumnPin.objects.filter(
            active_employee_q('user__'),
            column=column,
            user__board_memberships__board=board,
        ).values_list('user_id', flat=True)
    )


def _apply_pins(card, task, column, board, *, actor, current_ids):
    """Give the card the people pinned to `column`, if that changes anything.

    `ADD` puts them beside `current_ids`, `REPLACE` in their place. Only active
    members of the board count; a `REPLACE` whose pinned people are all gone
    would leave the card with nobody, so it changes nothing. A change goes
    through `tasks.services.replace_task_assignees()` and is one journal entry
    «Исполнители по колонке «…»» (`EDITED`, `fields=['assignees']`,
    `by_column` the column's name as it is now). Returns the card's
    исполнители afterwards and those added (sorted ids): the caller tells the
    people concerned, inside its own transaction.
    """
    from tasks.services import TaskWorkflowError, replace_task_assignees

    current = set(current_ids)
    pinned = _pinned_ids(column, board)
    if not pinned:
        return sorted(current), []
    target = pinned if column.pinned_mode == BoardColumn.PinnedMode.REPLACE else current | pinned
    if target == current:
        return sorted(current), []
    try:
        replace_task_assignees(task, sorted(target), actor=actor)
    except TaskWorkflowError as exc:
        raise BoardError(str(exc)) from exc
    _record(
        card, BoardCardEvent.Kind.EDITED, actor=actor,
        fields=['assignees'], by_column_id=column.pk, by_column=column.name,
    )
    return sorted(target), sorted(target - current)


def _next_number(board):
    """The next card number of `board` — read under the board lock.

    The largest ever given plus one: no card is deleted and a cancelled one
    keeps its number, so a number is never handed out twice.
    """
    return (BoardCard.objects.filter(board=board).aggregate(last=Max('number'))['last'] or 0) + 1


def create_card(
    sub_board, *, actor, title, due_date, assignee_ids, description='', column=None,
    field_values=None,
):
    """A new card at the end of a working column of `sub_board`, and its task.

    `column` (an object or an id) must be a working column of this very
    sub-board; `None` is its first working column. The card takes the board's
    next number (`_next_number()`, under the board lock) and the people pinned
    to its column (`_apply_pins()`); every исполнитель it ends up with is told
    once. `field_values` (`{field id: raw value}`) are the board's own fields,
    parsed by kind (`_clean_field_values()`); an empty one stores nothing.
    """
    from notifications.services import notify_board_task_assigned
    from tasks.services import TaskWorkflowError, create_board_card_task

    with transaction.atomic():
        board = _lock_board(sub_board.board_id)
        _refuse_archived('create_card', board, actor=actor)
        if not can_work_on_board(actor, board):
            _rejected('create_card', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        sub_board = _sub_board_of(board, sub_board, operation='create_card', actor=actor)
        title = _clean_title(title)
        description = (description or '').strip()
        if due_date is None:
            raise BoardError('Укажите срок карточки.')
        if column is None:
            column = _first_working_id(sub_board)
        column = _working_column(sub_board, column, operation='create_card', actor=actor)
        ids = _clean_assignees(board, assignee_ids)
        values = _clean_field_values(board, field_values, {})
        card = BoardCard(
            board=board,
            sub_board=sub_board,
            column=column,
            position=_end_position(column),
            number=_next_number(board),
            title=title,
            description=description,
            created_by=actor,
        )
        card.clean()
        card.save()
        _write_field_values(card, _field_value_changes(values, {}), {})
        try:
            task = create_board_card_task(
                card,
                ids,
                created_by=actor,
                due_date=due_date,
                task_text=compose_task_text(title, description),
                # A board is shared work of people from any department: its
                # tasks name none (`Task.department` is free for `BOARD`).
                department=None,
            )
        except TaskWorkflowError as exc:
            raise BoardError(str(exc)) from exc
        _record(card, BoardCardEvent.Kind.CREATED, actor=actor, **_column_snapshot('column', column))
        ids, _ = _apply_pins(card, task, column, board, actor=actor, current_ids=ids)
        # Inside the transaction and after the task and its исполнители exist,
        # so a rollback leaves no notification about a card that never was —
        # and only the people the card really ended up with.
        notify_board_task_assigned(task, actor, _users(ids))
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_CREATED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_created',
        board_id=board.pk,
        sub_board_id=sub_board.pk,
        board_card_id=card.pk,
        column_id=column.pk,
        task_id=task.pk,
        actor_user_id=actor.pk,
        assignee_count=len(ids),
        outcome='ok',
    )
    return card


def update_card(card, *, actor, title, description, due_date, assignee_ids,
                expected_version=None, field_values=None):
    """Correct a live card, and its task with it.

    `expected_version` is the `BoardCard.version` the edit form was drawn with.
    A different current version means somebody else saved in between, and the
    edit is refused with `StaleCardError` rather than written over theirs.
    `None` — a call that is not a form — skips the comparison. An edit that
    stored something raises the version by one; one that changed nothing does
    not.

    `field_values` (`{field id: raw value}`) corrects the board's own fields
    named in it — `None` touches none. An empty value deletes the row; an
    archived field is left as it is. A value that changed is part of the same
    edit: the same version step, the same `card_updated`, and the journal's
    `EDITED` entry adds `custom` to `fields` and the fields' names as they
    are now to `custom_fields` («Изменено: Номер заявки, Срок изг.»).
    """
    from notifications.services import notify_board_task_assigned
    from tasks.models import TaskAssignee
    from tasks.services import (
        TaskWorkflowError,
        replace_task_assignees,
        update_board_card_task,
    )

    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        task = _lock_card_task(card)
        _refuse_archived('update_card', board, actor=actor, card_id=card.pk)
        if not can_work_on_board(actor, board):
            _rejected('update_card', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        if task.status.is_final:
            _rejected('update_card', 'task_final', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Задача карточки уже закрыта — изменить её нельзя.')
        if expected_version is not None and int(expected_version) != card.version:
            _rejected('update_card', 'stale_version', actor=actor, board_id=board.pk, card_id=card.pk)
            raise StaleCardError(
                'Карточку изменили, пока вы её редактировали. Ваши правки не '
                'сохранены: проверьте текущую версию и внесите их снова.'
            )
        title = _clean_title(title)
        description = (description or '').strip()
        if due_date is None:
            raise BoardError('Укажите срок карточки.')
        ids = _clean_assignees(board, assignee_ids)
        task_text = compose_task_text(title, description)
        current_ids = set(
            TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True)
        )
        current_rows = {
            row.field_id: row
            for row in BoardCardFieldValue.objects.filter(card=card).select_related('field')
        }
        values = _clean_field_values(board, field_values, current_rows)
        custom_changes = _field_value_changes(values, current_rows)
        changed = [
            name for name, value in (('title', title), ('description', description))
            if getattr(card, name) != value
        ]
        # Each of the three writes below is itself a no-op when its part did
        # not change; this is what tells the caller whether anything did.
        due_changed = task.due_date != due_date
        task_changed = task.task_text != task_text or due_changed
        assignees_changed = set(ids) != current_ids
        stored = bool(changed or task_changed or assignees_changed or custom_changes)
        if stored:
            card.title = title
            card.description = description
            card.version += 1
            card.save(update_fields=[*changed, 'version', 'updated_at'])
            _write_field_values(card, custom_changes, current_rows)
        try:
            update_board_card_task(task, task_text=task_text, due_date=due_date, actor=actor)
            replace_task_assignees(task, ids, actor=actor)
        except TaskWorkflowError as exc:
            raise BoardError(str(exc)) from exc
        # Only the people this edit put on the card: those who stay already
        # know, and those removed have nothing to do.
        added_ids = [user_id for user_id in ids if user_id not in current_ids]
        if added_ids:
            notify_board_task_assigned(task, actor, _users(added_ids))
        if stored:
            # Which of the four things an editor changes, in the form's order.
            fields = [*changed]
            if due_changed:
                fields.append('due_date')
            if assignees_changed:
                fields.append('assignees')
            details = {}
            if custom_changes:
                # The board's own fields, by their names as they are now.
                fields.append('custom')
                details['custom_fields'] = [field.name for field, _ in custom_changes]
            _record(card, BoardCardEvent.Kind.EDITED, actor=actor, fields=fields, **details)
            emit_board_updated(board.pk, BOARD_CHANGE_CARD_UPDATED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_updated',
        board_id=board.pk,
        board_card_id=card.pk,
        task_id=task.pk,
        actor_user_id=actor.pk,
        outcome='ok' if stored else 'unchanged',
    )
    return card


def _board_working_column(board, column, *, operation, actor, card_id=None):
    """`column` (an object or an id) as a working column of any sub-board of `board`.

    Refused when it is a closing column — reached only by completing the task
    — or not a column of this board at all: deleted meanwhile, or of another
    board, which a card never moves to.
    """
    column_id = getattr(column, 'pk', column)
    try:
        column_id = int(column_id)
    except (TypeError, ValueError):
        column_id = None
    found = (
        BoardColumn.objects.select_related('sub_board')
        .filter(pk=column_id, sub_board__board=board).first()
        if column_id is not None else None
    )
    if found is None:
        _rejected(operation, 'unknown_column', actor=actor, board_id=board.pk, card_id=card_id)
        raise BoardError('Колонка не найдена на этой доске — возможно, её удалили. Обновите страницу.')
    if found.is_done:
        _rejected(operation, 'done_column', actor=actor, board_id=board.pk, card_id=card_id)
        raise BoardError(
            f'В колонку «{found.name}» карточка попадает, когда её задача выполнена: '
            'завершите задачу с результатом.'
        )
    return found


def move_card(card, *, actor, column, before_card_id=None):
    """Put a live card in a working column of its board: at the end, or before
    another card.

    `column` is an object or an id, a working column of any sub-board of the
    card's own board; a closing column, a column deleted meanwhile and a
    column of another board are refused. A column of another sub-board moves
    the card there (`sub_board` follows the column; the panel's «Переместить
    в…» puts it at the end, and its number stays). `before_card_id` must name
    another card standing in the target column. Positions are spaced by
    `POSITION_STEP`; when the gap is gone the whole column is renumbered under
    the same board lock.

    A card whose task is closed is refused: the closing column is reached by
    completing the task, and left only by an administrator reopening it
    (`reopen_card()`).

    A move to where the card already stands — its own column, between the
    same neighbours — writes nothing and announces nothing. A move into
    another column records one `MOVED` entry (the columns' names as they are
    now, and the sub-boards' when it changes them) and gives the card the
    people pinned to the new column (`_apply_pins()`); those added are told.
    """
    from notifications.services import notify_board_task_assigned
    from tasks.models import TaskAssignee

    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        task = _lock_card_task(card)
        _refuse_archived('move_card', board, actor=actor, card_id=card.pk)
        if not can_work_on_board(actor, board):
            _rejected('move_card', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        target = _board_working_column(board, column, operation='move_card', actor=actor, card_id=card.pk)
        if task.status.is_final:
            _rejected('move_card', 'task_final', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(
                'Задача карточки закрыта. Вернуть её в работу может только администратор.'
            )
        previous_sub_board_id = card.sub_board_id
        crosses = target.sub_board_id != previous_sub_board_id
        previous_column_id = card.column_id or _first_working_id(previous_sub_board_id)
        cards = _column(target, exclude_card_id=card.pk)
        if before_card_id is None:
            index = len(cards)
        else:
            try:
                before_card_id = int(before_card_id)
            except (TypeError, ValueError):
                before_card_id = None
            index = next(
                (i for i, other in enumerate(cards) if other.pk == before_card_id),
                None,
            )
            if index is None:
                _rejected('move_card', 'bad_before_card', actor=actor, board_id=board.pk, card_id=card.pk)
                raise BoardError('Карточка, перед которой нужно встать, не найдена в этой колонке.')
        if target.pk == previous_column_id:
            # Where the card stands now: after every other card of its column
            # that sorts before it. The same index is the same place.
            current_index = sum(
                1 for other in cards if (other.position, other.pk) < (card.position, card.pk)
            )
            if index == current_index:
                return card
        lower = cards[index - 1].position if index > 0 else 0
        if index < len(cards):
            upper = cards[index].position
            position = (lower + upper) // 2 if upper - lower >= 2 else None
        else:
            position = lower + POSITION_STEP if lower + POSITION_STEP <= MAX_POSITION else None
        renumbered = position is None
        card.column = target
        card.sub_board = target.sub_board
        card.clean()
        if renumbered:
            # No gap left: respace the whole column with the card in its new
            # place. Every card write happens under the board lock held here.
            _renumber(cards[:index] + [card] + cards[index:])
            card.save(update_fields=['column', 'sub_board', 'updated_at'])
        else:
            card.position = position
            card.save(update_fields=['column', 'sub_board', 'position', 'updated_at'])
        if target.pk != previous_column_id:
            # A reorder within a column is a move of the tile, not of the
            # work: the journal records where the card went, by name — and,
            # across sub-boards, on which tab.
            previous = BoardColumn.objects.select_related('sub_board').filter(pk=previous_column_id).first()
            details = {
                **_column_snapshot('from_column', previous),
                **_column_snapshot('to_column', target),
            }
            if crosses:
                details.update({
                    'from_sub_board_id': previous_sub_board_id,
                    'from_sub_board': previous.sub_board.name if previous is not None else '',
                    'to_sub_board_id': target.sub_board_id,
                    'to_sub_board': target.sub_board.name,
                })
            _record(card, BoardCardEvent.Kind.MOVED, actor=actor, **details)
            current_ids = sorted(TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True))
            final_ids, added = _apply_pins(card, task, target, board, actor=actor, current_ids=current_ids)
            if final_ids != current_ids:
                # The edit form holds the исполнители: one drawn before this
                # move must not put the old ones back silently.
                card.version += 1
                card.save(update_fields=['version'])
            if added:
                notify_board_task_assigned(task, actor, _users(added))
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_MOVED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_moved',
        board_id=board.pk,
        sub_board_id=card.sub_board_id,
        previous_sub_board_id=previous_sub_board_id,
        board_card_id=card.pk,
        previous_column_id=previous_column_id,
        column_id=target.pk,
        renumbered=renumbered,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return card


def complete_card(card, *, actor, execution_comment):
    """Finish a card's work — `tasks.services.complete_task()`, nothing more.

    The comment is required and the right is the task's own
    (`can_complete_task()`). `card.column` is not touched: the card stands in
    the closing column because its task is completed, and reopening the task
    returns it to the working column it came from.
    """
    from tasks.services import TaskWorkflowError, complete_task

    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        task = _lock_card_task(card)
        try:
            task = complete_task(task, actor, execution_comment)
        except TaskWorkflowError as exc:
            raise BoardError(str(exc)) from exc
        _record(card, BoardCardEvent.Kind.COMPLETED, actor=actor)
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_COMPLETED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_completed',
        board_id=board.pk,
        board_card_id=card.pk,
        task_id=task.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return task


def reopen_card(card, *, actor):
    """Put a completed card back into work — `tasks.services.reopen_task()`.

    The right is the task's own (`can_reopen_task()`: an administrator, a
    `COMPLETED` task), asked by `reopen_task()` under the task's lock; the
    board adds only that an archived board stays as it was shelved. The card
    returns to the working column it kept, or to the first one if that column
    was deleted meanwhile (`columns.card_column()` decides; nothing is
    written to `column`), and the journal names that column.
    """
    from tasks.services import TaskWorkflowError, reopen_task

    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        task = _lock_card_task(card)
        _refuse_archived('reopen_card', board, actor=actor, card_id=card.pk)
        try:
            task = reopen_task(task, actor)
        except TaskWorkflowError as exc:
            raise BoardError(str(exc)) from exc
        working = [column for column in _sub_board_columns(card.sub_board) if not column.is_done]
        column = next((column for column in working if column.pk == card.column_id), None)
        if column is None and working:
            column = working[0]
        _record(card, BoardCardEvent.Kind.REOPENED, actor=actor, **_column_snapshot('column', column))
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_REOPENED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_reopened',
        board_id=board.pk,
        board_card_id=card.pk,
        task_id=task.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return task


def cancel_card(card, *, actor, reason):
    """Withdraw a card that should never have been put on the board.

    Its task becomes `CANCELLED` through `tasks.services.cancel_board_card_task()`
    — with the reason, who and when, and nothing claimed about the work — and
    the card leaves every column (`columns.card_column()`); its panel still
    reads the record by `?card=`. Who may do it is `can_cancel_card()`, asked
    after the locks; its исполнители get one bell entry each
    (`notify_board_task_cancelled()`), never an email.
    """
    from notifications.services import notify_board_task_cancelled
    from tasks.models import TaskAssignee
    from tasks.services import TaskWorkflowError, cancel_board_card_task

    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        task = _lock_card_task(card)
        card.board = board
        _refuse_archived('cancel_card', board, actor=actor, card_id=card.pk)
        if not can_cancel_card(actor, card):
            _rejected('cancel_card', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(
                'Отменить карточку может её автор, владелец доски или администратор.'
            )
        try:
            task = cancel_board_card_task(task, actor=actor, reason=reason)
        except TaskWorkflowError as exc:
            raise BoardError(str(exc)) from exc
        # Its исполнители, in the bell only; whoever cancelled is not told.
        assignee_ids = TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True)
        notify_board_task_cancelled(task, actor, _users(assignee_ids))
        _record(card, BoardCardEvent.Kind.CANCELLED, actor=actor)
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_CANCELLED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_cancelled',
        board_id=board.pk,
        board_card_id=card.pk,
        task_id=task.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return task


# --------------------------------------------------------------------------
# Sub-boards and columns
# --------------------------------------------------------------------------
#
# The board's structure belongs to whoever manages the board
# (`can_manage_board()`: the owner while an active employee, or an
# administrator), never to an archived board. Each operation locks the board
# and nothing else — the lock is what serialises every write on the board, so
# no card can be created in, or moved into, a column while it is being deleted.
# Positions are plain 1, 2, 3, … and the touched rows are renumbered; the
# closing column is always the last. Every change publishes one
# `board.updated(structure_changed)`; a refusal and a rename to the same name
# publish nothing.


def _manageable_board(board_id, operation, *, actor):
    """The board, locked, if `actor` may change its structure."""
    board = _lock_board(board_id)
    _refuse_archived(operation, board, actor=actor)
    if not can_manage_board(actor, board):
        _rejected(operation, 'not_permitted', actor=actor, board_id=board.pk)
        raise BoardError('Менять поддоски и колонки может владелец доски или администратор.')
    return board


def _clean_name(name, *, max_length, what):
    name = (name or '').strip()
    if not name:
        raise BoardError(f'Укажите название {what}.')
    if len(name) > max_length:
        raise BoardError(f'Название {what} — не длиннее {max_length} символов.')
    return name


def _clean_sub_board_name(board, name, *, exclude_pk=None):
    name = _clean_name(name, max_length=SUB_BOARD_NAME_MAX_LENGTH, what='поддоски')
    # Compared here, not by `name__iexact`: SQLite folds the case of ASCII
    # letters only, and «основная» must clash with «Основная» everywhere.
    others = SubBoard.objects.filter(board=board)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    if name.casefold() in {other.casefold() for other in others.values_list('name', flat=True)}:
        raise BoardError(f'Поддоска «{name}» на этой доске уже есть.')
    return name


def _renumber_rows(rows):
    """Positions 1, 2, 3, … for `rows` in their new order.

    Only the rows whose position really changes are written, with their
    `updated_at` — the `boards` sync revision reads it, and `bulk_update()`
    would not touch an `auto_now` field by itself.
    """
    now = timezone.now()
    changed = []
    for index, row in enumerate(rows, start=1):
        if row.position != index:
            row.position = index
            row.updated_at = now
            changed.append(row)
    if changed:
        model = type(changed[0])
        # An option of a list field has no `updated_at` of its own.
        stamped = any(field.name == 'updated_at' for field in model._meta.concrete_fields)
        model.objects.bulk_update(changed, ['position', 'updated_at'] if stamped else ['position'])


def _structure_changed(board, event, *, actor, **ids):
    emit_board_updated(board.pk, BOARD_CHANGE_STRUCTURE_CHANGED)
    log_event(
        logger, 'INFO', event,
        board_id=board.pk, actor_user_id=actor.pk, outcome='ok', **ids,
    )


def _create_sub_board_rows(board, name, *, position, actor):
    """A sub-board and its `DEFAULT_COLUMNS` — no checks, no event."""
    sub_board = SubBoard.objects.create(
        board=board, name=name, position=position, created_by=actor,
    )
    BoardColumn.objects.bulk_create([
        BoardColumn(sub_board=sub_board, name=column_name, position=index, is_done=is_done)
        for index, (column_name, is_done) in enumerate(DEFAULT_COLUMNS, start=1)
    ])
    return sub_board


def create_sub_board(board, *, actor, name):
    """A new sub-board at the end of the tabs, with the default columns."""
    with transaction.atomic():
        board = _manageable_board(board.pk, 'create_sub_board', actor=actor)
        name = _clean_sub_board_name(board, name)
        last = SubBoard.objects.filter(board=board).aggregate(last=Max('position'))['last'] or 0
        sub_board = _create_sub_board_rows(board, name, position=last + 1, actor=actor)
        _structure_changed(board, 'board.sub_board_created', actor=actor, sub_board_id=sub_board.pk)
    return sub_board


def rename_sub_board(sub_board, *, actor, name):
    with transaction.atomic():
        board = _manageable_board(sub_board.board_id, 'rename_sub_board', actor=actor)
        sub_board = _sub_board_of(board, sub_board, operation='rename_sub_board', actor=actor)
        name = _clean_sub_board_name(board, name, exclude_pk=sub_board.pk)
        if name == sub_board.name:
            return sub_board
        sub_board.name = name
        sub_board.save(update_fields=['name', 'updated_at'])
        _structure_changed(board, 'board.sub_board_renamed', actor=actor, sub_board_id=sub_board.pk)
    return sub_board


def _step(rows, row, direction):
    """`rows` with `row` swapped one place left or right — or `None` at the edge."""
    if direction not in ('left', 'right'):
        raise BoardError('Неизвестное направление.')
    index = next(i for i, other in enumerate(rows) if other.pk == row.pk)
    other = index - 1 if direction == 'left' else index + 1
    if other < 0 or other >= len(rows):
        return None
    rows = list(rows)
    rows[index], rows[other] = rows[other], rows[index]
    return rows


def move_sub_board(sub_board, *, actor, direction):
    """One tab left (`'left'`) or right (`'right'`)."""
    with transaction.atomic():
        board = _manageable_board(sub_board.board_id, 'move_sub_board', actor=actor)
        sub_board = _sub_board_of(board, sub_board, operation='move_sub_board', actor=actor)
        rows = _step(list(SubBoard.objects.filter(board=board).order_by('position', 'pk')),
                     sub_board, direction)
        if rows is None:
            _rejected('move_sub_board', 'edge', actor=actor, board_id=board.pk)
            raise BoardError(
                'Поддоска уже первая.' if direction == 'left' else 'Поддоска уже последняя.'
            )
        _renumber_rows(rows)
        _structure_changed(board, 'board.sub_board_moved', actor=actor, sub_board_id=sub_board.pk)
    return sub_board


def delete_sub_board(sub_board, *, actor):
    """Remove an empty sub-board with its columns.

    Only one that holds no card at all — a cancelled or a completed card is a
    record (`BoardCard.sub_board` is `PROTECT`) — and never the board's last.
    """
    with transaction.atomic():
        board = _manageable_board(sub_board.board_id, 'delete_sub_board', actor=actor)
        sub_board = _sub_board_of(board, sub_board, operation='delete_sub_board', actor=actor)
        if BoardCard.objects.filter(sub_board=sub_board).exists():
            _rejected('delete_sub_board', 'has_cards', actor=actor, board_id=board.pk)
            raise BoardError(
                'На поддоске есть карточки — в том числе завершённые или отменённые. '
                'Удалить можно только пустую поддоску.'
            )
        if SubBoard.objects.filter(board=board).count() <= 1:
            _rejected('delete_sub_board', 'last', actor=actor, board_id=board.pk)
            raise BoardError('Это единственная поддоска доски — её нельзя удалить.')
        sub_board_id = sub_board.pk
        BoardColumn.objects.filter(sub_board=sub_board).delete()
        sub_board.delete()
        _renumber_rows(list(SubBoard.objects.filter(board=board).order_by('position', 'pk')))
        _structure_changed(board, 'board.sub_board_deleted', actor=actor, sub_board_id=sub_board_id)


def _column_of(board, column, *, operation, actor):
    """`column` (an object or an id), re-read and only if it is on `board`."""
    column_id = getattr(column, 'pk', column)
    found = BoardColumn.objects.filter(pk=column_id, sub_board__board=board).first()
    if found is None:
        _rejected(operation, 'unknown_column', actor=actor, board_id=board.pk)
        raise BoardError('Колонка не найдена на этой доске — возможно, её удалили.')
    return found


def create_column(sub_board, *, actor, name):
    """A new working column, just before the closing one."""
    with transaction.atomic():
        board = _manageable_board(sub_board.board_id, 'create_column', actor=actor)
        sub_board = _sub_board_of(board, sub_board, operation='create_column', actor=actor)
        name = _clean_name(name, max_length=COLUMN_NAME_MAX_LENGTH, what='колонки')
        columns = _sub_board_columns(sub_board)
        if len(columns) >= MAX_COLUMNS:
            _rejected('create_column', 'limit', actor=actor, board_id=board.pk)
            raise BoardError(f'На поддоске уже {MAX_COLUMNS} колонок — больше нельзя.')
        working = [column for column in columns if not column.is_done]
        done = [column for column in columns if column.is_done]
        column = BoardColumn.objects.create(
            sub_board=sub_board, name=name, position=len(columns) + 1, is_done=False,
        )
        _renumber_rows(working + [column] + done)
        _structure_changed(
            board, 'board.column_created', actor=actor,
            sub_board_id=sub_board.pk, column_id=column.pk,
        )
    return column


def rename_column(column, *, actor, name):
    """A new name for any column, the closing one included."""
    with transaction.atomic():
        board = _manageable_board(column.sub_board.board_id, 'rename_column', actor=actor)
        column = _column_of(board, column, operation='rename_column', actor=actor)
        name = _clean_name(name, max_length=COLUMN_NAME_MAX_LENGTH, what='колонки')
        if name == column.name:
            return column
        column.name = name
        column.save(update_fields=['name', 'updated_at'])
        _structure_changed(board, 'board.column_renamed', actor=actor, column_id=column.pk)
    return column


def move_column(column, *, actor, direction):
    """A working column one place left or right among the working ones.

    The closing column does not move: it is always the last.
    """
    with transaction.atomic():
        board = _manageable_board(column.sub_board.board_id, 'move_column', actor=actor)
        column = _column_of(board, column, operation='move_column', actor=actor)
        if column.is_done:
            _rejected('move_column', 'done_column', actor=actor, board_id=board.pk)
            raise BoardError('Завершающая колонка всегда последняя.')
        columns = _sub_board_columns(column.sub_board_id)
        working = [other for other in columns if not other.is_done]
        rows = _step(working, column, direction)
        if rows is None:
            _rejected('move_column', 'edge', actor=actor, board_id=board.pk)
            raise BoardError(
                'Колонка уже первая.' if direction == 'left'
                else 'Правее только завершающая колонка — она всегда последняя.'
            )
        _renumber_rows(rows + [other for other in columns if other.is_done])
        _structure_changed(board, 'board.column_moved', actor=actor, column_id=column.pk)
    return column


def delete_column(column, *, actor):
    """Remove a working column that holds no open card.

    Never the closing column, never the last working one, and never a column
    an open card (task `IN_PROGRESS`) stands in — the first working column
    holds the cards whose column was deleted before, so they count there too.
    The closed cards that named it keep their record with `column` NULL: they
    return to the first working column if reopened.
    """
    from tasks.models import Task

    with transaction.atomic():
        board = _manageable_board(column.sub_board.board_id, 'delete_column', actor=actor)
        column = _column_of(board, column, operation='delete_column', actor=actor)
        if column.is_done:
            _rejected('delete_column', 'done_column', actor=actor, board_id=board.pk)
            raise BoardError('Завершающую колонку удалить нельзя — её можно переименовать.')
        working = BoardColumn.objects.filter(sub_board_id=column.sub_board_id, is_done=False)
        if working.count() <= 1:
            _rejected('delete_column', 'last_working', actor=actor, board_id=board.pk)
            raise BoardError('Это единственная рабочая колонка поддоски — её нельзя удалить.')
        standing = BoardCard.objects.filter(
            _in_column_q(column, _first_working_id(column.sub_board_id)),
            sub_board_id=column.sub_board_id,
        )
        open_count = Task.objects.filter(
            source_type=Task.SourceType.BOARD,
            board_card__in=standing,
            status__code='IN_PROGRESS',
        ).count()
        if open_count:
            _rejected('delete_column', 'open_cards', actor=actor, board_id=board.pk)
            raise BoardError(
                f'В колонке открытые карточки: {open_count}. Перенесите, завершите '
                'или отмените их, затем удалите колонку.'
            )
        column_id, sub_board_id = column.pk, column.sub_board_id
        BoardCard.objects.filter(column=column).update(column=None)
        column.delete()
        _renumber_rows(_sub_board_columns(sub_board_id))
        _structure_changed(
            board, 'board.column_deleted', actor=actor,
            sub_board_id=sub_board_id, column_id=column_id,
        )


def set_column_pins(column, *, actor, user_ids, mode):
    """«Закреплённые исполнители» of a working column: who, and `ADD` or `REPLACE`.

    The manager's, like every other part of the structure: one board lock,
    never an archived board, never a closing column. Everyone pinned is an
    active member of the board; an empty list unpins everybody. The cards
    already standing in the column keep their people — a pin acts on the
    next card created in the column or moved into it (`_apply_pins()`).
    The same people and mode change nothing and announce nothing; a change
    publishes one `board.updated(structure_changed)`.
    """
    with transaction.atomic():
        board = _manageable_board(column.sub_board.board_id, 'set_column_pins', actor=actor)
        column = _column_of(board, column, operation='set_column_pins', actor=actor)
        if column.is_done:
            _rejected('set_column_pins', 'done_column', actor=actor, board_id=board.pk)
            raise BoardError(
                'В завершающую колонку карточка попадает выполненной — закреплять '
                'за ней исполнителей незачем.'
            )
        if mode not in BoardColumn.PinnedMode.values:
            raise BoardError('Неизвестный режим закрепления.')
        requested = {int(getattr(value, 'pk', value)) for value in user_ids or ()}
        members = set(
            BoardMember.objects.filter(
                active_employee_q('user__'), board=board, user_id__in=requested,
            ).values_list('user_id', flat=True)
        )
        if requested - members:
            _rejected('set_column_pins', 'not_a_member', actor=actor, board_id=board.pk)
            raise BoardError('Закрепить за колонкой можно только активных участников доски.')
        current = set(BoardColumnPin.objects.filter(column=column).values_list('user_id', flat=True))
        if current == requested and column.pinned_mode == mode:
            return column
        BoardColumnPin.objects.filter(column=column, user_id__in=current - requested).delete()
        BoardColumnPin.objects.bulk_create(
            [BoardColumnPin(column=column, user_id=user_id) for user_id in sorted(requested - current)]
        )
        column.pinned_mode = mode
        column.save(update_fields=['pinned_mode', 'updated_at'])
        _structure_changed(
            board, 'board.column_pins_changed', actor=actor,
            column_id=column.pk, pinned_count=len(requested),
        )
    return column


def set_column_stale_days(column, *, actor, days):
    """«Застой» of a working column: highlight a card standing in it `days`
    calendar days or more; `None` (or an empty value) switches it off.

    The manager's, like every other part of the structure: one board lock,
    never an archived board, never the closing column (a completed card is not
    stuck), 1 to `STALE_DAYS_MAX` days. The same threshold again stores and
    announces nothing; a change publishes one `board.updated(structure_changed)`
    and touches the column's `updated_at` (the `boards` sync revision).
    """
    if days in (None, ''):
        days = None
    else:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise BoardError('Укажите число дней или оставьте поле пустым.') from None
    with transaction.atomic():
        board = _manageable_board(column.sub_board.board_id, 'set_column_stale_days', actor=actor)
        column = _column_of(board, column, operation='set_column_stale_days', actor=actor)
        if column.is_done:
            _rejected('set_column_stale_days', 'done_column', actor=actor, board_id=board.pk)
            raise BoardError(
                'В завершающей колонке работа уже выполнена — застоя в ней не бывает.'
            )
        if days is not None and not STALE_DAYS_MIN <= days <= STALE_DAYS_MAX:
            _rejected('set_column_stale_days', 'out_of_range', actor=actor, board_id=board.pk)
            raise BoardError(
                f'Застой задаётся числом дней от {STALE_DAYS_MIN} до {STALE_DAYS_MAX} '
                'или не задаётся вовсе.'
            )
        if column.stale_after_days == days:
            return column
        column.stale_after_days = days
        column.save(update_fields=['stale_after_days', 'updated_at'])
        _structure_changed(
            board, 'board.column_stale_days_changed', actor=actor,
            column_id=column.pk, stale_after_days=days,
        )
    return column


# --------------------------------------------------------------------------
# Custom card fields
# --------------------------------------------------------------------------
#
# A board's own fields — text, number, date, a list of coloured options —
# set up by whoever manages the board, under one board lock each, never on an
# archived board. Every change publishes one `board.updated(structure_changed)`
# (the tiles and the card form draw the fields) and touches the sub-boards'
# `updated_at`, which the `boards` sync revision reads; the same values again
# store and publish nothing. A field or an option some card holds a value of
# is archived, never deleted, and a field with values keeps its kind.
#
# The values themselves are written by `create_card()`/`update_card()`
# (`field_values`), parsed here by kind: `_clean_field_values()`.

MAX_FIELDS = 20
MAX_OPTIONS = 30
FIELD_NAME_MAX_LENGTH = 60
OPTION_LABEL_MAX_LENGTH = 60
FIELD_TEXT_MAX_LENGTH = 500
# `DecimalField(18, 4)`: fourteen digits before the decimal comma, four after.
NUMBER_INTEGER_DIGITS = 14
NUMBER_DECIMAL_PLACES = 4

_NUMBER = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$')
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
# Spaces a number may be typed or pasted with: «1 234,5».
_NUMBER_SPACES = re.compile(r'[\s  ]')


class FieldValueError(BoardError):
    """A card field's value refused; the message names the field.

    `field_id` lets a form put the message beside that very input.
    """

    def __init__(self, field, message):
        super().__init__(f'«{field.name}»: {message}')
        self.field_id = field.pk


def _fields_board(board_id, operation, *, actor):
    """The board, locked, if `actor` may set up its card fields."""
    board = _lock_board(board_id)
    _refuse_archived(operation, board, actor=actor)
    if not can_manage_board(actor, board):
        _rejected(operation, 'not_permitted', actor=actor, board_id=board.pk)
        raise BoardError('Поля карточек настраивает владелец доски или администратор.')
    return board


def _fields_changed(board, event, *, actor, **ids):
    """One `board.updated(structure_changed)`, and the sync revision moved.

    The `boards` revision of `/realtime/sync/` reads the sub-boards'
    `updated_at`; every tile and card form of every sub-board draws the
    fields, so a reader who missed the event still redraws.
    """
    SubBoard.objects.filter(board=board).update(updated_at=timezone.now())
    _structure_changed(board, event, actor=actor, **ids)


def _field_of(board, field, *, operation, actor):
    """`field` (an object or an id), re-read and only if it is on `board`."""
    field_id = getattr(field, 'pk', field)
    found = BoardField.objects.filter(pk=field_id, board=board).first()
    if found is None:
        _rejected(operation, 'unknown_field', actor=actor, board_id=board.pk)
        raise BoardError('Поле не найдено на этой доске — возможно, его удалили.')
    return found


def _option_of(board, option, *, operation, actor):
    """`option` (an object or an id), re-read and only if it is on `board`."""
    option_id = getattr(option, 'pk', option)
    found = (
        BoardFieldOption.objects.select_related('field')
        .filter(pk=option_id, field__board=board).first()
    )
    if found is None:
        _rejected(operation, 'unknown_option', actor=actor, board_id=board.pk)
        raise BoardError('Вариант не найден — возможно, его удалили.')
    return found


def _clean_field_name(board, name, *, exclude_pk=None):
    """Trimmed, required, at most 60, unique among the board's fields
    (archived ones too) whatever the case — compared by `casefold()`, as
    sub-board names are, since SQLite folds ASCII only."""
    name = _clean_name(name, max_length=FIELD_NAME_MAX_LENGTH, what='поля')
    others = BoardField.objects.filter(board=board)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    if name.casefold() in {other.casefold() for other in others.values_list('name', flat=True)}:
        raise BoardError(f'Поле «{name}» на этой доске уже есть.')
    return name


def _clean_kind(kind):
    if kind not in BoardField.Kind.values:
        raise BoardError('Неизвестный вид поля.')
    return kind


def _clean_color(color):
    """One of the eight colours; empty is grey."""
    color = (color or BoardFieldColor.GRAY).strip()
    if color not in BoardFieldColor.values:
        raise BoardError('Неизвестный цвет варианта.')
    return color


def _clean_option_label(label, taken=(), *, field_name=''):
    label = _clean_name(label, max_length=OPTION_LABEL_MAX_LENGTH, what='варианта')
    if label.casefold() in {other.casefold() for other in taken}:
        where = f' поля «{field_name}»' if field_name else ''
        raise BoardError(f'Вариант «{label}»{where} уже есть.')
    return label


def _refuse_field_limit(board, operation, *, actor):
    if BoardField.objects.filter(board=board, is_archived=False).count() >= MAX_FIELDS:
        _rejected(operation, 'field_limit', actor=actor, board_id=board.pk)
        raise BoardError(
            f'На доске уже {MAX_FIELDS} полей — больше нельзя. Уберите ненужное в архив.'
        )


def _refuse_option_limit(field, operation, *, actor, adding=1):
    live = BoardFieldOption.objects.filter(field=field, is_archived=False).count()
    if live + adding > MAX_OPTIONS:
        _rejected(operation, 'option_limit', actor=actor, board_id=field.board_id)
        raise BoardError(
            f'У поля «{field.name}» может быть не больше {MAX_OPTIONS} вариантов. '
            'Уберите ненужные в архив.'
        )


def _has_values(field):
    return BoardCardFieldValue.objects.filter(field=field).exists()


def create_field(board, *, actor, name, kind, show_on_tile=True, options=()):
    """A new field at the end of the board's fields.

    `options` — `(label, colour)` pairs — only for a list (`SELECT`): any
    other kind takes none. At most `MAX_FIELDS` live fields per board and
    `MAX_OPTIONS` options per field.
    """
    with transaction.atomic():
        board = _fields_board(board.pk, 'create_field', actor=actor)
        name = _clean_field_name(board, name)
        kind = _clean_kind(kind)
        options = [tuple(option) for option in options or ()]
        if options and kind != BoardField.Kind.SELECT:
            raise BoardError('Варианты бывают только у поля вида «Список».')
        if len(options) > MAX_OPTIONS:
            raise BoardError(f'У поля может быть не больше {MAX_OPTIONS} вариантов.')
        cleaned = []
        for label, color in options:
            cleaned.append((
                _clean_option_label(label, [item[0] for item in cleaned]),
                _clean_color(color),
            ))
        _refuse_field_limit(board, 'create_field', actor=actor)
        last = BoardField.objects.filter(board=board).aggregate(last=Max('position'))['last'] or 0
        field = BoardField.objects.create(
            board=board, name=name, kind=kind, position=last + 1, show_on_tile=bool(show_on_tile),
        )
        BoardFieldOption.objects.bulk_create([
            BoardFieldOption(field=field, label=label, color=color, position=index)
            for index, (label, color) in enumerate(cleaned, start=1)
        ])
        _fields_changed(board, 'board.field_created', actor=actor, field_id=field.pk)
    return field


def update_field(field, *, actor, name, show_on_tile, kind=None):
    """A field's name, whether its tile shows it, and — while no card holds a
    value of it — its kind. `kind=None` keeps the kind.

    A list turned into another kind loses its options (none can be used: the
    field has no values). The same values store and publish nothing.
    """
    with transaction.atomic():
        board = _fields_board(field.board_id, 'update_field', actor=actor)
        field = _field_of(board, field, operation='update_field', actor=actor)
        name = _clean_field_name(board, name, exclude_pk=field.pk)
        kind = field.kind if kind in (None, '') else _clean_kind(kind)
        show_on_tile = bool(show_on_tile)
        if kind != field.kind and _has_values(field):
            _rejected('update_field', 'kind_with_values', actor=actor, board_id=board.pk)
            raise BoardError(
                f'В карточках уже есть значения поля «{field.name}» — его вид менять нельзя.'
            )
        changed = [
            attribute for attribute, value in (('name', name), ('kind', kind), ('show_on_tile', show_on_tile))
            if getattr(field, attribute) != value
        ]
        if not changed:
            return field
        if 'kind' in changed and field.kind == BoardField.Kind.SELECT:
            BoardFieldOption.objects.filter(field=field).delete()
        field.name, field.kind, field.show_on_tile = name, kind, show_on_tile
        field.save(update_fields=[*changed, 'updated_at'])
        _fields_changed(board, 'board.field_updated', actor=actor, field_id=field.pk)
    return field


def move_field(field, *, actor, direction):
    """One place towards the start (`'left'`) or the end (`'right'`)."""
    with transaction.atomic():
        board = _fields_board(field.board_id, 'move_field', actor=actor)
        field = _field_of(board, field, operation='move_field', actor=actor)
        rows = _step(list(BoardField.objects.filter(board=board).order_by('position', 'pk')), field, direction)
        if rows is None:
            _rejected('move_field', 'edge', actor=actor, board_id=board.pk)
            raise BoardError('Поле уже первое.' if direction == 'left' else 'Поле уже последнее.')
        _renumber_rows(rows)
        _fields_changed(board, 'board.field_moved', actor=actor, field_id=field.pk)
    return field


def archive_field(field, *, actor):
    """«В архив»: no longer offered on a card or drawn on a tile; the values
    the cards hold stay and «Описание» shows them marked «(в архиве)»."""
    return _set_field_archived(field, actor=actor, archived=True)


def restore_field(field, *, actor):
    """«Вернуть»: live again, within `MAX_FIELDS`."""
    return _set_field_archived(field, actor=actor, archived=False)


def _set_field_archived(field, *, actor, archived):
    operation = 'archive_field' if archived else 'restore_field'
    with transaction.atomic():
        board = _fields_board(field.board_id, operation, actor=actor)
        field = _field_of(board, field, operation=operation, actor=actor)
        if field.is_archived == archived:
            return field
        if not archived:
            _refuse_field_limit(board, operation, actor=actor)
        field.is_archived = archived
        field.save(update_fields=['is_archived', 'updated_at'])
        _fields_changed(
            board, 'board.field_archived' if archived else 'board.field_restored',
            actor=actor, field_id=field.pk,
        )
    return field


def delete_field(field, *, actor):
    """Remove a field no card holds a value of, with its options.

    One with values is refused: it can only be archived, so the values stay.
    """
    with transaction.atomic():
        board = _fields_board(field.board_id, 'delete_field', actor=actor)
        field = _field_of(board, field, operation='delete_field', actor=actor)
        if _has_values(field):
            _rejected('delete_field', 'has_values', actor=actor, board_id=board.pk)
            raise BoardError(
                f'В карточках есть значения поля «{field.name}» — его можно только убрать в архив.'
            )
        field_id = field.pk
        field.delete()
        _renumber_rows(list(BoardField.objects.filter(board=board).order_by('position', 'pk')))
        _fields_changed(board, 'board.field_deleted', actor=actor, field_id=field_id)


def _select_field(board, field, *, operation, actor):
    field = _field_of(board, field, operation=operation, actor=actor)
    if field.kind != BoardField.Kind.SELECT:
        _rejected(operation, 'not_a_list', actor=actor, board_id=board.pk)
        raise BoardError('Варианты бывают только у поля вида «Список».')
    return field


def create_option(field, *, actor, label, color):
    """A new option at the end of a list field's options."""
    with transaction.atomic():
        board = _fields_board(field.board_id, 'create_option', actor=actor)
        field = _select_field(board, field, operation='create_option', actor=actor)
        options = list(BoardFieldOption.objects.filter(field=field))
        label = _clean_option_label(label, [option.label for option in options], field_name=field.name)
        color = _clean_color(color)
        _refuse_option_limit(field, 'create_option', actor=actor)
        option = BoardFieldOption.objects.create(
            field=field, label=label, color=color,
            position=max((other.position for other in options), default=0) + 1,
        )
        _fields_changed(board, 'board.option_created', actor=actor, field_id=field.pk, option_id=option.pk)
    return option


def update_option(option, *, actor, label, color):
    """An option's label and colour; the same pair stores and publishes nothing."""
    with transaction.atomic():
        board = _fields_board(option.field.board_id, 'update_option', actor=actor)
        option = _option_of(board, option, operation='update_option', actor=actor)
        taken = BoardFieldOption.objects.filter(field_id=option.field_id).exclude(pk=option.pk)
        label = _clean_option_label(
            label, taken.values_list('label', flat=True), field_name=option.field.name,
        )
        color = _clean_color(color)
        if (label, color) == (option.label, option.color):
            return option
        option.label, option.color = label, color
        option.save(update_fields=['label', 'color'])
        _fields_changed(board, 'board.option_updated', actor=actor, option_id=option.pk)
    return option


def move_option(option, *, actor, direction):
    """One place up (`'left'`) or down (`'right'`) among the field's options."""
    with transaction.atomic():
        board = _fields_board(option.field.board_id, 'move_option', actor=actor)
        option = _option_of(board, option, operation='move_option', actor=actor)
        rows = _step(
            list(BoardFieldOption.objects.filter(field_id=option.field_id).order_by('position', 'pk')),
            option, direction,
        )
        if rows is None:
            _rejected('move_option', 'edge', actor=actor, board_id=board.pk)
            raise BoardError('Вариант уже первый.' if direction == 'left' else 'Вариант уже последний.')
        _renumber_rows(rows)
        _fields_changed(board, 'board.option_moved', actor=actor, option_id=option.pk)
    return option


def archive_option(option, *, actor):
    """No longer offered; the cards that chose it keep it, shown «(в архиве)»."""
    return _set_option_archived(option, actor=actor, archived=True)


def restore_option(option, *, actor):
    return _set_option_archived(option, actor=actor, archived=False)


def _set_option_archived(option, *, actor, archived):
    operation = 'archive_option' if archived else 'restore_option'
    with transaction.atomic():
        board = _fields_board(option.field.board_id, operation, actor=actor)
        option = _option_of(board, option, operation=operation, actor=actor)
        if option.is_archived == archived:
            return option
        if not archived:
            _refuse_option_limit(option.field, operation, actor=actor)
        option.is_archived = archived
        option.save(update_fields=['is_archived'])
        _fields_changed(
            board, 'board.option_archived' if archived else 'board.option_restored',
            actor=actor, option_id=option.pk,
        )
    return option


def delete_option(option, *, actor):
    """Remove an option no card has chosen; one in use can only be archived."""
    with transaction.atomic():
        board = _fields_board(option.field.board_id, 'delete_option', actor=actor)
        option = _option_of(board, option, operation='delete_option', actor=actor)
        if BoardCardFieldValue.objects.filter(option=option).exists():
            _rejected('delete_option', 'has_values', actor=actor, board_id=board.pk)
            raise BoardError(
                f'Вариант «{option.label}» выбран в карточках — его можно только убрать в архив.'
            )
        option_id, field_id = option.pk, option.field_id
        option.delete()
        _renumber_rows(list(BoardFieldOption.objects.filter(field_id=field_id).order_by('position', 'pk')))
        _fields_changed(board, 'board.option_deleted', actor=actor, option_id=option_id)


# -- values -----------------------------------------------------------------


def _is_empty(raw):
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _parse_text(field, raw):
    text = str(raw).strip()
    if len(text) > FIELD_TEXT_MAX_LENGTH:
        raise FieldValueError(field, f'не длиннее {FIELD_TEXT_MAX_LENGTH} символов.')
    return text


def _parse_number(field, raw):
    """A `Decimal` from «12», «-3,5», «1 234.25»: a comma or a point, spaces
    between digit groups allowed; at most 14 digits before the comma and 4
    after — what `DecimalField(18, 4)` holds. No exponent, no «NaN»."""
    if isinstance(raw, bool):
        raise FieldValueError(field, 'введите число, например 12 или 3,5.')
    text = _NUMBER_SPACES.sub('', str(raw)).replace(',', '.')
    if not _NUMBER.match(text):
        raise FieldValueError(field, 'введите число, например 12 или 3,5.')
    integer, _, fraction = text.lstrip('+-').partition('.')
    if len(integer.lstrip('0')) > NUMBER_INTEGER_DIGITS:
        raise FieldValueError(field, f'не больше {NUMBER_INTEGER_DIGITS} цифр до запятой.')
    if len(fraction.rstrip('0')) > NUMBER_DECIMAL_PLACES:
        raise FieldValueError(field, f'не больше {NUMBER_DECIMAL_PLACES} цифр после запятой.')
    value = Decimal(text)
    return Decimal(0) if value == 0 else value


def _parse_date(field, raw):
    if isinstance(raw, datetime.datetime):
        raw = raw.date()
    if isinstance(raw, datetime.date):
        return raw
    text = str(raw).strip()
    try:
        if not _ISO_DATE.match(text):
            raise ValueError(text)
        return datetime.date.fromisoformat(text)
    except ValueError as exc:
        raise FieldValueError(field, 'дата в формате ГГГГ-ММ-ДД.') from exc


def _parse_option(field, raw, current_option_id):
    """A live option of this very field — or the archived one the card
    already holds, sent back unchanged by its edit form."""
    try:
        option_id = int(getattr(raw, 'pk', raw))
    except (TypeError, ValueError):
        option_id = None
    option = next((option for option in field.options.all() if option.pk == option_id), None)
    if option is None:
        raise FieldValueError(field, 'выберите вариант из списка.')
    if option.is_archived and option.pk != current_option_id:
        raise FieldValueError(field, f'вариант «{option.label}» убран в архив — выберите другой.')
    return option


def _parse_value(field, raw, current_row):
    """The stored form of `raw` for `field`, or `None` for an empty value."""
    if _is_empty(raw):
        return None
    if field.kind == BoardField.Kind.TEXT:
        return _parse_text(field, raw)
    if field.kind == BoardField.Kind.NUMBER:
        return _parse_number(field, raw)
    if field.kind == BoardField.Kind.DATE:
        return _parse_date(field, raw)
    return _parse_option(field, raw, getattr(current_row, 'option_id', None))


def _clean_field_values(board, field_values, current_rows):
    """`{field: parsed value or None}` for the fields `field_values` names.

    Every key must be a field of `board` (an id or an object) — another
    board's, or one deleted meanwhile, is refused. An archived field is left
    out: its value is not touched, whatever the form sent. Read under the
    board lock, the fields with their options in two queries.
    """
    if not field_values:
        return {}
    fields = {
        field.pk: field
        for field in BoardField.objects.filter(board=board).prefetch_related('options')
    }
    parsed = {}
    for key, raw in field_values.items():
        try:
            field_id = int(getattr(key, 'pk', key))
        except (TypeError, ValueError):
            field_id = None
        field = fields.get(field_id)
        if field is None:
            raise BoardError('Поле карточки не найдено на этой доске — возможно, его удалили. Обновите страницу.')
        if field.is_archived:
            continue
        parsed[field] = _parse_value(field, raw, current_rows.get(field.pk))
    return parsed


def _stored_value(row):
    column = BoardCardFieldValue.KIND_COLUMNS[row.field.kind]
    return row.option_id if column == 'option' else getattr(row, column)


def _field_value_changes(parsed, current_rows):
    """The fields whose value really changes, in the fields' order: `[(field, value)]`."""
    changes = []
    for field, value in parsed.items():
        row = current_rows.get(field.pk)
        if value is None:
            if row is not None:
                changes.append((field, None))
            continue
        new = value.pk if field.kind == BoardField.Kind.SELECT else value
        if row is None or _stored_value(row) != new:
            changes.append((field, value))
    changes.sort(key=lambda change: (change[0].position, change[0].pk))
    return changes


def _write_field_values(card, changes, current_rows):
    """Store `changes`: a row created or rewritten, or deleted for an empty value."""
    for field, value in changes:
        row = current_rows.get(field.pk)
        if value is None:
            row.delete()
            continue
        if row is None:
            row = BoardCardFieldValue(card=card, field=field)
        row.card, row.field = card, field
        row.value_text = row.value_number = row.value_date = row.option = None
        column = BoardCardFieldValue.KIND_COLUMNS[field.kind]
        setattr(row, column, value)
        row.clean()
        row.save()


# --------------------------------------------------------------------------
# The shelf
# --------------------------------------------------------------------------


def _open_card_count(board):
    from tasks.models import Task

    return Task.objects.filter(
        source_type=Task.SourceType.BOARD,
        board_card__board=board,
        status__is_final=False,
    ).count()


def archive_board(board, *, actor):
    """Put a finished board on the shelf: read-only, at the same address.

    Refused while any card is still open — the shelf must never hide work in
    progress — so every card on an archived board is done or cancelled.
    Writes `status`, `archived_at` and `archived_by` and nothing else.
    """
    with transaction.atomic():
        board = _lock_board(board.pk)
        if not can_manage_board(actor, board):
            reason = 'already_archived' if board.is_archived else 'not_permitted'
            _rejected('archive_board', reason, actor=actor, board_id=board.pk)
            raise BoardError(
                ARCHIVED_MESSAGE if board.is_archived
                else 'Убрать доску в архив может её владелец или администратор.'
            )
        open_count = _open_card_count(board)
        if open_count:
            _rejected('archive_board', 'open_cards', actor=actor, board_id=board.pk)
            raise BoardError(
                f'Сначала завершите или отмените открытые карточки: {open_count}.'
            )
        board.status = Board.Status.ARCHIVED
        board.archived_at = timezone.now()
        board.archived_by = actor
        board.save(update_fields=['status', 'archived_at', 'archived_by', 'updated_at'])
        emit_board_updated(board.pk, BOARD_CHANGE_BOARD_ARCHIVED)
    log_event(
        logger, 'INFO', 'board.archived',
        board_id=board.pk, actor_user_id=actor.pk, outcome='ok',
    )
    return board


def restore_board(board, *, actor):
    """«Вернуть из архива»: the board is live again, exactly as it was left."""
    with transaction.atomic():
        board = _lock_board(board.pk)
        if not can_restore_board(actor, board):
            _rejected('restore_board', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError(
                'Вернуть из архива можно только доску в архиве — её владельцу '
                'или администратору.'
            )
        board.status = Board.Status.ACTIVE
        board.archived_at = None
        board.archived_by = None
        board.save(update_fields=['status', 'archived_at', 'archived_by', 'updated_at'])
        emit_board_updated(board.pk, BOARD_CHANGE_BOARD_RESTORED)
    log_event(
        logger, 'INFO', 'board.restored',
        board_id=board.pk, actor_user_id=actor.pk, outcome='ok',
    )
    return board


# --------------------------------------------------------------------------
# «Обсуждение»
# --------------------------------------------------------------------------


def post_card_comment(card, *, actor, text):
    """One message in a card's «Обсуждение». No editing, no deletion.

    Locks the board, then the card, and asks `can_comment_card()` after the
    locks — an active member or an administrator, not on an archived board;
    the state of the task does not matter. The text is stripped, required and
    at most `COMMENT_MAX_LENGTH` characters. The исполнители and the card's
    author hear of it in the bell (`notify_board_card_comment()`, never the
    writer), and the board publishes `board.updated(comment_added)`. Logged
    by identifiers only, never the text.
    """
    from notifications.services import notify_board_card_comment
    from tasks.models import Task, TaskAssignee

    text = (text or '').strip()
    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        card.board = board
        _refuse_archived('post_comment', board, actor=actor, card_id=card.pk)
        if not can_comment_card(actor, card):
            _rejected('post_comment', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Писать в обсуждение могут участники доски.')
        if not text:
            raise BoardError('Напишите сообщение.')
        if len(text) > COMMENT_MAX_LENGTH:
            raise BoardError(f'Сообщение — не длиннее {COMMENT_MAX_LENGTH} символов.')
        comment = BoardCardComment.objects.create(card=card, author=actor, text=text)
        task = Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)
        recipient_ids = {
            *TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True),
            card.created_by_id,
        }
        notify_board_card_comment(comment, task, actor, _users(recipient_ids))
        emit_board_updated(board.pk, BOARD_CHANGE_COMMENT_ADDED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.comment_posted',
        board_id=board.pk,
        board_card_id=card.pk,
        comment_id=comment.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return comment
