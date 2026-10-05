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
from django.db.models import Max
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
    BOARD_CHANGE_MEMBERS_CHANGED,
)

from .columns import WORK_STAGES
from .models import Board, BoardCard, BoardMember
from .permissions import (
    active_employee_q,
    can_cancel_card,
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
    """`{pk: user}` for the active employees among `user_ids`."""
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


def create_board(*, name, department, owner, actor, description='', member_ids=()):
    """A new board; its owner is always one of its members."""
    if not can_create_board(actor):
        _rejected('create_board', 'not_permitted', actor=actor)
        raise BoardError('Создание доски недоступно.')
    name = (name or '').strip()
    if not name:
        raise BoardError('Укажите название доски.')
    if len(name) > 200:
        raise BoardError('Название доски — не длиннее 200 символов.')
    if department is None:
        raise BoardError('Укажите подразделение доски.')
    requested = {owner.pk, *(int(getattr(value, 'pk', value)) for value in member_ids)}
    users = _active_users(requested)
    if owner.pk not in users:
        raise BoardError('Владельцем доски может быть только активный сотрудник.')
    if set(requested) - set(users):
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
            _rejected('add_members', 'inactive_user', actor=actor, board_id=board.pk)
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


def _column(board, stage, *, exclude_card_id=None):
    """The cards stored in one column of `board`, in order.

    Every card whose `stage` names the column, done ones included: a completed
    card keeps its place so that reopening it puts it back where it was.
    """
    cards = BoardCard.objects.filter(board=board, stage=stage)
    if exclude_card_id is not None:
        cards = cards.exclude(pk=exclude_card_id)
    return list(cards.order_by('position', 'pk'))


def _renumber(cards):
    """Respace `cards` (already in their new order) at `POSITION_STEP`."""
    for index, card in enumerate(cards, start=1):
        card.position = index * POSITION_STEP
    BoardCard.objects.bulk_update(cards, ['position'])


def _end_position(board, stage):
    """The position after the last card of a column, renumbering if needed."""
    last = (
        BoardCard.objects.filter(board=board, stage=stage)
        .aggregate(last=Max('position'))['last']
    ) or 0
    if last + POSITION_STEP <= MAX_POSITION:
        return last + POSITION_STEP
    column = _column(board, stage)
    _renumber(column)
    return (len(column) + 1) * POSITION_STEP


def create_card(
    board, *, actor, title, due_date, assignee_ids, description='',
    stage=BoardCard.Stage.TODO,
):
    """A new card at the end of its column, and the one task it is the work of."""
    from notifications.services import notify_board_task_assigned
    from tasks.services import TaskWorkflowError, create_board_card_task

    with transaction.atomic():
        board = _lock_board(board.pk)
        _refuse_archived('create_card', board, actor=actor)
        if not can_work_on_board(actor, board):
            _rejected('create_card', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        title = _clean_title(title)
        description = (description or '').strip()
        if due_date is None:
            raise BoardError('Укажите срок карточки.')
        if stage not in WORK_STAGES:
            raise BoardError('Неизвестная колонка доски.')
        ids = _clean_assignees(board, assignee_ids)
        card = BoardCard.objects.create(
            board=board,
            stage=stage,
            position=_end_position(board, stage),
            title=title,
            description=description,
            created_by=actor,
        )
        try:
            task = create_board_card_task(
                card,
                ids,
                created_by=actor,
                due_date=due_date,
                task_text=compose_task_text(title, description),
                department=board.department,
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
        board_card_id=card.pk,
        task_id=task.pk,
        stage=card.stage,
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


def move_card(card, *, actor, stage, before_card_id=None):
    """Put a live card in a working column: at the end, or before another card.

    `before_card_id` must name another card of the same board standing in the
    target column. Positions are spaced by `POSITION_STEP`; when the gap is
    gone the whole column is renumbered under the same board lock.

    A card whose task is closed is refused: «Готово» is reached by completing
    the task, and left only by an administrator reopening it (`tasks:reopen`).

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
        if stage not in WORK_STAGES:
            _rejected('move_card', 'unknown_stage', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Неизвестная колонка доски.')
        if task.status.is_final:
            _rejected('move_card', 'task_final', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(
                'Задача карточки закрыта. Вернуть её в работу может только администратор.'
            )
        previous_stage = card.stage
        column = _column(board, stage, exclude_card_id=card.pk)
        if before_card_id is None:
            index = len(column)
        else:
            try:
                before_card_id = int(before_card_id)
            except (TypeError, ValueError):
                before_card_id = None
            index = next(
                (i for i, other in enumerate(column) if other.pk == before_card_id),
                None,
            )
            if index is None:
                _rejected('move_card', 'bad_before_card', actor=actor, board_id=board.pk, card_id=card.pk)
                raise BoardError('Карточка, перед которой нужно встать, не найдена в этой колонке.')
        if stage == previous_stage:
            # Where the card stands now: after every other card of its column
            # that sorts before it. The same index is the same place.
            current_index = sum(
                1 for other in column if (other.position, other.pk) < (card.position, card.pk)
            )
            if index == current_index:
                return card
        lower = column[index - 1].position if index > 0 else 0
        if index < len(column):
            upper = column[index].position
            position = (lower + upper) // 2 if upper - lower >= 2 else None
        else:
            position = lower + POSITION_STEP if lower + POSITION_STEP <= MAX_POSITION else None
        renumbered = position is None
        card.stage = stage
        if renumbered:
            # No gap left: respace the whole column with the card in its new
            # place. Every card write happens under the board lock held here.
            _renumber(column[:index] + [card] + column[index:])
            card.save(update_fields=['stage', 'updated_at'])
        else:
            card.position = position
            card.save(update_fields=['stage', 'position', 'updated_at'])
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_MOVED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_moved',
        board_id=board.pk,
        board_card_id=card.pk,
        previous_stage=previous_stage,
        stage=card.stage,
        renumbered=renumbered,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return card


def complete_card(card, *, actor, execution_comment):
    """Finish a card's work — `tasks.services.complete_task()`, nothing more.

    The comment is required and the right is the task's own
    (`can_complete_task()`). `card.stage` is not touched: the card stands in
    «Готово» because its task is completed, and reopening the task returns it
    to the column it came from.
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
    after the locks; nobody is notified, as when an СМК correction withdraws a
    task.
    """
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
