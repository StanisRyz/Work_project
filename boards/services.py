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
and says nothing. A board's task changed elsewhere (`tasks:complete`, a
reopen, a file) is the task's own `task.*` event and the `boards` sync
revision, never a `board.updated`.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
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
    BOARD_CHANGE_CARD_UPDATED,
    BOARD_CHANGE_COMMENT_ADDED,
    BOARD_CHANGE_MEMBERS_CHANGED,
    BOARD_CHANGE_STRUCTURE_CHANGED,
)

from .columns import DEFAULT_COLUMNS, MAX_COLUMNS
from .models import Board, BoardCard, BoardCardComment, BoardColumn, BoardMember, SubBoard
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


def create_board(*, name, owner, actor, member_ids=(), department=None, description=''):
    """A new board; its owner is always one of its members.

    It starts with one sub-board, «Основная», and its `DEFAULT_COLUMNS`.

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
    requested = {owner.pk, *(int(getattr(value, 'pk', value)) for value in member_ids)}
    users = _active_users(requested)
    if owner.pk not in users:
        raise BoardError('Владельцем доски может быть только активный сотрудник.')
    if set(requested) - set(users):
        _rejected('create_board', 'ineligible_member', actor=actor)
        raise BoardError('Участниками доски могут быть только активные сотрудники.')
    with transaction.atomic():
        board = Board.objects.create(
            name=name,
            description=(description or '').strip(),
            department=department,
            owner=owner,
        )
        BoardMember.objects.bulk_create(
            [BoardMember(board=board, user_id=user_id, added_by=actor) for user_id in sorted(users)]
        )
        _create_sub_board_rows(board, DEFAULT_SUB_BOARD_NAME, position=1, actor=actor)
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


def create_card(
    sub_board, *, actor, title, due_date, assignee_ids, description='', column=None,
):
    """A new card at the end of a working column of `sub_board`, and its task.

    `column` (an object or an id) must be a working column of this very
    sub-board; `None` is its first working column.
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
        card = BoardCard(
            board=board,
            sub_board=sub_board,
            column=column,
            position=_end_position(column),
            title=title,
            description=description,
            created_by=actor,
        )
        card.clean()
        card.save()
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
        # Inside the transaction and after the task and its исполнители exist,
        # so a rollback leaves no notification about a card that never was.
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
                expected_version=None):
    """Correct a live card, and its task with it.

    `expected_version` is the `BoardCard.version` the edit form was drawn with.
    A different current version means somebody else saved in between, and the
    edit is refused with `StaleCardError` rather than written over theirs.
    `None` — a call that is not a form — skips the comparison. An edit that
    stored something raises the version by one; one that changed nothing does
    not.
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
        changed = [
            name for name, value in (('title', title), ('description', description))
            if getattr(card, name) != value
        ]
        # Each of the three writes below is itself a no-op when its part did
        # not change; this is what tells the caller whether anything did.
        task_changed = task.task_text != task_text or task.due_date != due_date
        assignees_changed = set(ids) != current_ids
        stored = bool(changed or task_changed or assignees_changed)
        if stored:
            card.title = title
            card.description = description
            card.version += 1
            card.save(update_fields=[*changed, 'version', 'updated_at'])
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


def move_card(card, *, actor, column, before_card_id=None):
    """Put a live card in a working column of its own sub-board: at the end,
    or before another card.

    `column` is an object or an id; the closing column, a column deleted
    meanwhile and a column of another sub-board are refused — a card never
    leaves its sub-board. `before_card_id` must name another card standing in
    the target column. Positions are spaced by `POSITION_STEP`; when the gap
    is gone the whole column is renumbered under the same board lock.

    A card whose task is closed is refused: the closing column is reached by
    completing the task, and left only by an administrator reopening it
    (`tasks:reopen`).

    A move to where the card already stands — its own column, between the
    same neighbours — writes nothing and announces nothing.
    """
    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        task = _lock_card_task(card)
        _refuse_archived('move_card', board, actor=actor, card_id=card.pk)
        if not can_work_on_board(actor, board):
            _rejected('move_card', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        sub_board = card.sub_board
        target = _working_column(sub_board, column, operation='move_card', actor=actor, card_id=card.pk)
        if task.status.is_final:
            _rejected('move_card', 'task_final', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(
                'Задача карточки закрыта. Вернуть её в работу может только администратор.'
            )
        first_working_id = _first_working_id(sub_board)
        previous_column_id = card.column_id or first_working_id
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
        card.clean()
        if renumbered:
            # No gap left: respace the whole column with the card in its new
            # place. Every card write happens under the board lock held here.
            _renumber(cards[:index] + [card] + cards[index:])
            card.save(update_fields=['column', 'updated_at'])
        else:
            card.position = position
            card.save(update_fields=['column', 'position', 'updated_at'])
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_MOVED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_moved',
        board_id=board.pk,
        sub_board_id=card.sub_board_id,
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
        type(changed[0]).objects.bulk_update(changed, ['position', 'updated_at'])


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
