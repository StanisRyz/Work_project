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
and says nothing. A board's task changed elsewhere (an older attachment of it
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
the next number under the board lock. A working column may carry
«Правила при входе» — pinned people (`set_column_pins()`), a checklist
template, field values, followers — which a card created in it or moved into
it gets through `_apply_column_entry()` in the same transaction;
`move_card()` takes a working column of any sub-board of the card's own
board, and a board's actions «Передать дальше» (`run_board_action()`) move a
card, set its people and values and post into «Чат» in one transaction.

A board has its own card fields («Поля карточек»: text, number, date, a list
of coloured options), set up by its manager — `create_field()` and the rest,
one `structure_changed` each — and filled in by `create_card()`/
`update_card()` through `field_values`, parsed by kind in
`_clean_field_values()`. A field or an option a card holds a value of is
archived, never deleted.

A card carries a «Чек-лист» (`add_checklist_item()` and the rest: whoever
works on the board, an open card only, one `board.updated(checklist_changed)`
and one journal entry each — a reorder writes none) and its followers
(`toggle_card_subscription()`: personal, no event, no entry). Who hears of a
card — a message, a cancellation, a completion — is
`selectors.card_audience()`; the people a message mentions are told once, by
`BOARD_CARD_MENTION`, and follow the card from then on.

A card may hold «Подзадачи» — cards with a `parent`, in no column, one level
deep: `create_subtask()`, `create_subtasks_from_list()` and
`checklist_item_to_subtask()` add them, every other card service works for
them but `move_card()`, and the card's journal records each one added,
completed, reopened and cancelled (`SUBTASK`).

A message of «Чат» carries files (`BoardCardFile`, the board's own — never a
`tasks.TaskAttachment`): `post_card_comment(files=…)` checks them by the one
upload policy, writes them before their rows and removes them again if the
transaction fails; `delete_card_file()` leaves a tombstone row, removes the
file once committed and publishes `board.updated(file_deleted)`.

Cards are linked («Связи», `BoardCardLink`) by `link_cards()`/
`unlink_cards()` — across boards too, both boards locked in id order. A card
waits («ждёт») while an incoming `BLOCKS` comes from an open card; when the
last such blocker is completed or cancelled, `_after_blocker_closed()` tells
the waiting card's исполнители (`BOARD_UNBLOCKED`) inside that transaction.
A column may be followed («🔔 Сообщать о новых карточках»,
`toggle_column_subscription()`): a card entering it — created in it, moved
into it, completed into the closing column — tells its followers
(`_notify_column_entered()`, `BOARD_COLUMN_ENTERED`), each person once per
action.
"""

import datetime
import logging
import re
from decimal import Decimal
from functools import partial

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
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
    BOARD_CHANGE_CHECKLIST_CHANGED,
    BOARD_CHANGE_COMMENT_ADDED,
    BOARD_CHANGE_FILE_DELETED,
    BOARD_CHANGE_MEMBERS_CHANGED,
    BOARD_CHANGE_REQUEST_CHANGED,
    BOARD_CHANGE_STRUCTURE_CHANGED,
)

from .columns import DEFAULT_COLUMNS, MAX_COLUMNS
from .models import (
    CHECKLIST_TEXT_MAX_LENGTH,
    DUE_COMMENT_MAX_LENGTH,
    MAX_CHECKLIST_ITEMS,
    MAX_FILES_PER_MESSAGE,
    MAX_ACTIONS,
    MAX_SUBTASKS,
    MAX_TEMPLATE_ITEMS,
    INTAKE_DUE_DAYS_MAX,
    INTAKE_HINT_MAX_LENGTH,
    REQUEST_DESCRIPTION_MAX_LENGTH,
    REQUEST_TITLE_MAX_LENGTH,
    NORM_DAYS_MAX,
    NORM_DAYS_MIN,
    BOARD_CODE_MAX_LENGTH,
    BOARD_CODE_MIN_LENGTH,
    BOARD_CODE_PATTERN,
    Board,
    BoardCard,
    BoardCardChecklistItem,
    BoardCardComment,
    BoardCardCommentMention,
    BoardCardDueChange,
    BoardCardEvent,
    BoardCardFile,
    BoardCardFieldValue,
    BoardCardLink,
    BoardCardSubscription,
    BoardAction,
    BoardActionAssignee,
    BoardActionFieldRule,
    BoardColumn,
    BoardColumnChecklistTemplate,
    BoardColumnFieldRule,
    BoardColumnFollower,
    BoardColumnPin,
    BoardColumnSubscription,
    BoardDigestSubscription,
    BoardField,
    BoardIntakeHandler,
    BoardRequest,
    BoardFieldColor,
    BoardFieldOption,
    BoardMember,
    SubBoard,
)
from .permissions import (
    active_employee_q,
    board_readers_q,
    can_add_subtask,
    can_cancel_card,
    can_comment_card,
    can_decide_request,
    can_create_board,
    can_delete_card_file,
    can_follow_card,
    can_follow_column,
    can_link_card,
    can_subscribe_digest,
    can_manage_board,
    can_restore_board,
    can_submit_request,
    can_unlink_cards,
    can_view_board,
    can_work_on_board,
    readable_boards_q,
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


class DueReasonError(BoardError):
    """A move of the срок refused for its reason (missing, unknown, inactive)
    or its comment; the form puts it beside «Причина переноса»."""


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
    on somebody who is no longer on the board. So do their subscriptions to
    the board's cards (`BoardCardSubscription`) and columns
    (`BoardColumnSubscription`), their digest of it
    (`BoardDigestSubscription`) and their place among its request handlers
    (`BoardIntakeHandler`).
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
        # Their «Следить» on the cards of this board goes with the membership:
        # `selectors.card_audience()` would leave them out anyway, but a
        # follower who reads nothing should not be listed as one.
        BoardCardSubscription.objects.filter(card__board=board, user=user).delete()
        # So does their «🔔» on the board's columns: a person who no longer
        # reads the board is told nothing about its cards.
        BoardColumnSubscription.objects.filter(column__sub_board__board=board, user=user).delete()
        # And their digest of this board: a person who no longer reads it is
        # mailed nothing about it.
        BoardDigestSubscription.objects.filter(board=board, user=user).delete()
        # And their place among those who sort the board's requests.
        BoardIntakeHandler.objects.filter(board=board, user=user).delete()
        # And their place in the columns' «Подписчики» and the actions'
        # исполнители: neither may put a card on, or under, somebody who
        # no longer reads the board.
        followed_columns = list(
            BoardColumnFollower.objects.filter(column__sub_board__board=board, user=user)
            .values_list('column_id', flat=True)
        )
        BoardColumnFollower.objects.filter(column__sub_board__board=board, user=user).delete()
        if BoardActionAssignee.objects.filter(action__board=board, user=user).exists():
            BoardActionAssignee.objects.filter(action__board=board, user=user).delete()
            SubBoard.objects.filter(board=board).update(updated_at=timezone.now())
        pins = BoardColumnPin.objects.filter(column__sub_board__board=board, user=user)
        pinned_columns = list(pins.values_list('column_id', flat=True)) + followed_columns
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
    working column — those whose column was deleted (`column` NULL).

    Never a subtask: it has no column either, but it lives inside its card,
    so it takes no place in a column, does not count against deleting one
    and is never renumbered with its cards. This is the write side's one
    point of «подзадач в колонках нет» (`selectors._column_tasks()` is the
    read side's).
    """
    condition = Q(column=column)
    if column.pk == first_working_id:
        condition |= Q(column__isnull=True)
    return Q(parent__isnull=True) & condition


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


def _pin_target(column, board, current):
    """The исполнители `column`'s pins give a card that holds `current`.

    `ADD` puts the pinned people beside them, `REPLACE` in their place. Only
    active members of the board count; a `REPLACE` whose pinned people are
    all gone would leave the card with nobody, so it changes nothing.
    """
    pinned = _pinned_ids(column, board)
    if not pinned:
        return set(current)
    if column.pinned_mode == BoardColumn.PinnedMode.REPLACE:
        return pinned
    return set(current) | pinned


def _apply_field_rules(card, rules, *, operation):
    """Give the card the values `rules` (`BoardFieldRule`s, their fields with
    their options) set, and return the names of the fields that changed.

    Each value is parsed by the card's own parse (`_parse_value()`). A field
    archived since, or of another board, is skipped; a value that no longer
    parses — an option archived since — applies nothing and is logged. A
    value the card already holds is left alone unless the rule says
    `overwrite`: a rule sets, it does not reset.
    """
    rules = [
        rule for rule in rules
        if not rule.field.is_archived and rule.field.board_id == card.board_id
    ]
    if not rules:
        return []
    current_rows = {
        row.field_id: row
        for row in BoardCardFieldValue.objects.filter(card=card).select_related('field')
    }
    parsed = {}
    for rule in rules:
        try:
            value = _parse_value(rule.field, rule.value, None)
        except FieldValueError:
            log_event(
                logger, 'INFO', 'board.field_rule_skipped',
                board_id=card.board_id, board_card_id=card.pk, field_id=rule.field_id,
                operation=operation, outcome='skipped',
            )
            continue
        if value is None:
            continue
        if rule.field_id in current_rows and not rule.overwrite:
            continue
        parsed[rule.field] = value
    changes = _field_value_changes(parsed, current_rows)
    _write_field_values(card, changes, current_rows)
    return [field.name for field, _ in changes]


def _apply_checklist_template(card, column, *, actor):
    """Put the column's template lines at the end of the card's «Чек-лист».

    A line whose text the card already holds (whatever the case) is not
    repeated; what does not fit under `MAX_CHECKLIST_ITEMS` is left out.
    Returns how many were added and how many did not fit.
    """
    template = list(
        BoardColumnChecklistTemplate.objects.filter(column=column).order_by('position', 'pk')
    )
    if not template:
        return 0, 0
    items = _checklist_items(card)
    held = {item.text.casefold() for item in items}
    room = MAX_CHECKLIST_ITEMS - len(items)
    position = items[-1].position if items else 0
    new, skipped = [], 0
    for line in template:
        key = line.text.casefold()
        if key in held:
            continue
        held.add(key)
        if len(new) >= room:
            skipped += 1
            continue
        position += 1
        new.append(BoardCardChecklistItem(card=card, text=line.text, position=position, created_by=actor))
    BoardCardChecklistItem.objects.bulk_create(new)
    return len(new), skipped


def _apply_column_followers(card, column, board):
    """Make the column's «Подписчики» followers of the card — those who are
    active readers of the board and do not follow it yet. Returns how many."""
    wanted = set(
        get_user_model().objects.filter(
            board_readers_q(board), board_column_follows__column=column,
        ).values_list('pk', flat=True)
    )
    if not wanted:
        return 0
    following = set(
        BoardCardSubscription.objects.filter(card=card, user_id__in=wanted)
        .values_list('user_id', flat=True)
    )
    new = sorted(wanted - following)
    BoardCardSubscription.objects.bulk_create(
        [BoardCardSubscription(card=card, user_id=user_id) for user_id in new]
    )
    return len(new)


def _apply_column_entry(card, task, column, actor, *, board, current_ids):
    """«Правила при входе» of `column`, for a card that has just entered it.

    Called by `create_card()` (through `_place_new_card()`) for the column a
    card is created in, and by `move_card()` and `run_board_action()` for the
    column a card is moved into — never for a reorder within a column or a
    drop in place, never for a subtask (it stands in no column). Inside the
    caller's transaction, under the board, card and task locks it holds.

    Four rules, in this order:

    * «Закреплённые исполнители» (`_pin_target()`) — through
      `tasks.services.replace_task_assignees()`;
    * «Значения полей» (`BoardColumnFieldRule`, `_apply_field_rules()`);
    * «Шаблон чек-листа» (`BoardColumnChecklistTemplate`,
      `_apply_checklist_template()`);
    * «Подписчики» (`BoardColumnFollower`, `_apply_column_followers()`).

    Whatever they did is one journal entry — `EDITED` with `by_column` (the
    column's name as it is now), `fields` (`assignees`, `custom` with
    `custom_fields`), `checklist_added`, `checklist_skipped` and
    `followers_added` (numbers) — never a text. Nothing done, nothing
    written. Returns the card's исполнители afterwards and those added
    (sorted ids) — the caller tells them — and whether something an edit
    form holds (исполнители, field values) changed.
    """
    from tasks.services import TaskWorkflowError, replace_task_assignees

    current = set(current_ids)
    target = _pin_target(column, board, current)
    assignees_changed = target != current
    if assignees_changed:
        try:
            replace_task_assignees(task, sorted(target), actor=actor)
        except TaskWorkflowError as exc:
            raise BoardError(str(exc)) from exc
    field_names = _apply_field_rules(
        card,
        BoardColumnFieldRule.objects.filter(column=column)
        .select_related('field').prefetch_related('field__options').order_by('pk'),
        operation='column_entry',
    )
    checklist_added, checklist_skipped = _apply_checklist_template(card, column, actor=actor)
    followers_added = _apply_column_followers(card, column, board)
    if assignees_changed or field_names or checklist_added or checklist_skipped or followers_added:
        fields = []
        details = {}
        if assignees_changed:
            fields.append('assignees')
        if field_names:
            fields.append('custom')
            details['custom_fields'] = field_names
        if checklist_added:
            details['checklist_added'] = checklist_added
        if checklist_skipped:
            details['checklist_skipped'] = checklist_skipped
        if followers_added:
            details['followers_added'] = followers_added
        _record(
            card, BoardCardEvent.Kind.EDITED, actor=actor,
            fields=fields, by_column_id=column.pk, by_column=column.name, **details,
        )
    return sorted(target), sorted(target - current), bool(assignees_changed or field_names)


def _next_number(board):
    """The next card number of `board` — read under the board lock.

    The largest ever given plus one: no card is deleted and a cancelled one
    keeps its number, so a number is never handed out twice.
    """
    return (BoardCard.objects.filter(board=board).aggregate(last=Max('number'))['last'] or 0) + 1


def _new_card(board, sub_board, *, actor, title, description, due_date, ids, values,
              column=None, position, parent=None, request_id=None):
    """The rows of one new card — the `BoardCard`, its field values, its
    `BOARD` task and its `CREATED` entry — inside the caller's transaction,
    under the board lock it holds.

    What `create_card()` and `create_subtask()` share: the next number of the
    board (`_next_number()`, one series for cards and subtasks alike), the
    task through `tasks.services.create_board_card_task()`, the journal
    entry. A card stands in `column`; a subtask (`parent`) in none, at
    `position` in its parent's list. A card made of a request
    (`accept_request()`) names it in its `CREATED` entry (`request_id`). Who
    is told, and what is published, is the caller's.
    """
    from tasks.services import TaskWorkflowError, create_board_card_task

    card = BoardCard(
        board=board,
        sub_board=sub_board,
        column=column,
        parent=parent,
        position=position,
        number=_next_number(board),
        title=title,
        description=description,
        # The first срок — what «перенесён N раз» is counted from.
        original_due_date=due_date,
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
    if parent is None:
        details = _column_snapshot('column', column)
    else:
        # The card it lives in, by id and code — identifiers, never text.
        details = {'parent_id': parent.pk, 'parent': f'{board.code}-{parent.number}'}
    if request_id is not None:
        details['request_id'] = request_id
    _record(card, BoardCardEvent.Kind.CREATED, actor=actor, **details)
    return card, task


def _place_new_card(board, sub_board, *, actor, title, due_date, assignee_ids, description,
                    column, field_values, request_id=None, operation='create_card'):
    """A new card at the end of a working column of `sub_board`, its task,
    its pins and who is told — inside the caller's transaction, under the
    board lock it holds, the right already asked. What `create_card()` and
    `accept_request()` share; publishing is the caller's.
    """
    from notifications.services import notify_board_task_assigned

    sub_board = _sub_board_of(board, sub_board, operation=operation, actor=actor)
    title = _clean_title(title)
    description = (description or '').strip()
    if due_date is None:
        raise BoardError('Укажите срок карточки.')
    if column is None:
        column = _first_working_id(sub_board)
    column = _working_column(sub_board, column, operation=operation, actor=actor)
    ids = _clean_assignees(board, assignee_ids)
    values = _clean_field_values(board, field_values, {})
    card, task = _new_card(
        board, sub_board, actor=actor, title=title, description=description,
        due_date=due_date, ids=ids, values=values, column=column,
        position=_end_position(column), request_id=request_id,
    )
    ids, _, _ = _apply_column_entry(card, task, column, actor, board=board, current_ids=ids)
    # Inside the transaction and after the task and its исполнители exist,
    # so a rollback leaves no notification about a card that never was —
    # and only the people the card really ended up with.
    notify_board_task_assigned(task, actor, _users(ids))
    # The column's followers — but not its исполнители, just told.
    _notify_column_entered(card, task, column, board, actor=actor, told=ids)
    return card, task, column, ids


def create_card(
    sub_board, *, actor, title, due_date, assignee_ids, description='', column=None,
    field_values=None,
):
    """A new card at the end of a working column of `sub_board`, and its task.

    `column` (an object or an id) must be a working column of this very
    sub-board; `None` is its first working column. The card takes the board's
    next number (`_next_number()`, under the board lock) and the people pinned
    to its column (`_apply_column_entry()`, with the column's other entry
    rules); every исполнитель it ends up with is told
    once. `field_values` (`{field id: raw value}`) are the board's own fields,
    parsed by kind (`_clean_field_values()`); an empty one stores nothing.
    The column's followers are told the card entered it
    (`_notify_column_entered()`), except those already told as исполнители.
    """
    with transaction.atomic():
        board = _lock_board(sub_board.board_id)
        _refuse_archived('create_card', board, actor=actor)
        if not can_work_on_board(actor, board):
            _rejected('create_card', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        card, task, column, ids = _place_new_card(
            board, sub_board, actor=actor, title=title, due_date=due_date,
            assignee_ids=assignee_ids, description=description, column=column,
            field_values=field_values,
        )
        sub_board = card.sub_board
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
                expected_version=None, field_values=None, due_reason_id=None, due_comment=''):
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

    **A срок that existed moves only with a reason** (`due_reason_id`, an
    active `references.DeviationReason`; `due_comment` up to
    `DUE_COMMENT_MAX_LENGTH`, optional): without one the whole edit is
    refused with `DueReasonError`, before anything is written. A срок set
    where there was none needs no reason (`reason` NULL). Each move is one
    `BoardCardDueChange` in the edit's transaction; the `EDITED` entry names
    `due_reason_id` (never the comment), and the card's author, исполнители
    and followers but the editor hear of it (`BOARD_DUE_CHANGED`, bell only).
    The срок of a card is required, so a срок is neither cleared nor — on a
    card — ever set from nothing today; the rule says what it would be.
    """
    from notifications.services import notify_board_due_changed, notify_board_task_assigned
    from tasks.models import TaskAssignee
    from tasks.services import (
        TaskWorkflowError,
        replace_task_assignees,
        update_board_card_task,
    )

    from .selectors import card_audience

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
        old_due = task.due_date
        # A reason sent with a срок left as it was is no move and is ignored.
        reason = _clean_due_reason(
            due_reason_id, needed=old_due is not None, actor=actor, board=board, card=card,
        ) if due_changed else None
        due_comment = _clean_due_comment(due_comment) if due_changed else ''
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
            if due_changed:
                # Which reason, by id — never the comment's words.
                details['due_reason_id'] = getattr(reason, 'pk', None)
            if custom_changes:
                # The board's own fields, by their names as they are now.
                fields.append('custom')
                details['custom_fields'] = [field.name for field, _ in custom_changes]
            _record(card, BoardCardEvent.Kind.EDITED, actor=actor, fields=fields, **details)
            if due_changed:
                change = BoardCardDueChange.objects.create(
                    card=card, old_due=old_due, new_due=due_date, reason=reason,
                    comment=due_comment, changed_by=actor,
                )
                if card.original_due_date is None and old_due is None:
                    # A срок set where there was none is the first one.
                    card.original_due_date = due_date
                    card.save(update_fields=['original_due_date'])
                card.board = board
                task.due_date = due_date
                notify_board_due_changed(change, task, actor, card_audience(card, task))
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


def _clean_due_reason(raw, *, needed, actor, board, card):
    """The `DeviationReason` a move of the срок names — refused when one is
    needed and missing, or when the id is not an active reason. `None` when
    the move needs none and none was sent."""
    from references.models import DeviationReason

    try:
        reason_id = int(getattr(raw, 'pk', raw)) if raw not in (None, '') else None
    except (TypeError, ValueError):
        reason_id = None
        if needed:
            raise DueReasonError('Выберите причину переноса срока из списка.') from None
    if reason_id is None:
        if needed:
            _rejected('update_card', 'due_reason_missing', actor=actor, board_id=board.pk, card_id=card.pk)
            raise DueReasonError('Срок переносится только с причиной: выберите причину переноса.')
        return None
    reason = DeviationReason.objects.filter(pk=reason_id, is_active=True).first()
    if reason is None:
        _rejected('update_card', 'due_reason_unknown', actor=actor, board_id=board.pk, card_id=card.pk)
        raise DueReasonError('Такой причины переноса нет среди действующих — выберите другую.')
    return reason


def _clean_due_comment(comment):
    comment = (comment or '').strip()
    if len(comment) > DUE_COMMENT_MAX_LENGTH:
        raise DueReasonError(f'Комментарий к переносу — не длиннее {DUE_COMMENT_MAX_LENGTH} символов.')
    return comment


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


def _place_index(cards, card, pinned, before_card_id, *, operation, actor, board):
    """Where `card` goes among `cards` (the target column, without it): the
    index in the group of its own kind — the pinned cards or the others —
    and that group.

    A pinned card stands among the pinned ones and an unpinned one after
    them, so `before_card_id` naming a card of the other group puts it at
    the edge of its own: the end of the pinned, the start of the others.
    `None` is the end of its group. A card not in the column is refused.
    """
    group = [other for other in cards if other.is_pinned == pinned]
    if before_card_id is None:
        return len(group), group
    try:
        before_card_id = int(before_card_id)
    except (TypeError, ValueError):
        before_card_id = None
    index = next((i for i, other in enumerate(group) if other.pk == before_card_id), None)
    if index is not None:
        return index, group
    if any(other.pk == before_card_id for other in cards):
        return (len(group) if pinned else 0), group
    _rejected(operation, 'bad_before_card', actor=actor, board_id=board.pk, card_id=card.pk)
    raise BoardError('Карточка, перед которой нужно встать, не найдена в этой колонке.')


def _move_locked(board, card, task, target, *, actor, before_card_id=None, operation='move_card',
                 action=None):
    """Put `card` in `target` — the body of `move_card()`, shared with
    `run_board_action()`. Inside the caller's transaction, under the board,
    card and task locks it holds, the right and the state already asked.

    Returns `None` when the card already stands there (nothing written),
    else `(previous_sub_board_id, previous_column_id, renumbered, added,
    edited)`: who the column's entry rules added (not told yet — the caller
    tells them) and whether they changed what an edit form holds. A move
    into another column records one `MOVED` entry (with `action_id` and
    `action`, its name then, when an action made it), runs the column's
    «Правила при входе» (`_apply_column_entry()`) and unpins the card: a pin
    holds a card first in *its* column.
    """
    from tasks.models import TaskAssignee

    previous_sub_board_id = card.sub_board_id
    crosses = target.sub_board_id != previous_sub_board_id
    previous_column_id = card.column_id or _first_working_id(previous_sub_board_id)
    enters = target.pk != previous_column_id
    pinned = card.is_pinned and not enters
    cards = _column(target, exclude_card_id=card.pk)
    index, group = _place_index(
        cards, card, pinned, before_card_id, operation=operation, actor=actor, board=board,
    )
    if not enters:
        # Where the card stands now: after every other card of its group
        # that sorts before it. The same index is the same place.
        current_index = sum(
            1 for other in group if (other.position, other.pk) < (card.position, card.pk)
        )
        if index == current_index:
            return None
    lower = group[index - 1].position if index > 0 else 0
    if index < len(group):
        upper = group[index].position
        position = (lower + upper) // 2 if upper - lower >= 2 else None
    else:
        top = max((other.position for other in cards), default=0) if not pinned else lower
        lower = max(lower, top)
        position = lower + POSITION_STEP if lower + POSITION_STEP <= MAX_POSITION else None
    renumbered = position is None
    card.column = target
    card.sub_board = target.sub_board
    card.is_pinned = pinned
    card.clean()
    if renumbered:
        # No gap left: respace the card's group with the card in its new
        # place. Every card write happens under the board lock held here.
        _renumber(group[:index] + [card] + group[index:])
        card.save(update_fields=['column', 'sub_board', 'is_pinned', 'updated_at'])
    else:
        card.position = position
        card.save(update_fields=['column', 'sub_board', 'position', 'is_pinned', 'updated_at'])
    if crosses:
        # A card's subtasks live inside it: they go to its new sub-board
        # with it, in the same transaction, and keep their place in its list.
        BoardCard.objects.filter(parent=card).update(
            sub_board_id=target.sub_board_id, updated_at=timezone.now(),
        )
    added, edited = [], False
    if enters:
        # A reorder within a column is a move of the tile, not of the work:
        # the journal records where the card went, by name — and, across
        # sub-boards, on which tab, and by which action.
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
        if action is not None:
            details.update({'action_id': action.pk, 'action': action.name})
        _record(card, BoardCardEvent.Kind.MOVED, actor=actor, **details)
        current_ids = sorted(TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True))
        _, added, edited = _apply_column_entry(
            card, task, target, actor, board=board, current_ids=current_ids,
        )
    return previous_sub_board_id, previous_column_id, renumbered, added, edited


def _movable_card(card, operation, *, actor):
    """The board, the card and its task, locked in that order, if `actor`
    may move the card now: a live board, its worker, a card (never a
    subtask) whose task is open."""
    board = _lock_board(card.board_id)
    card = _lock_card(card, board)
    task = _lock_card_task(card)
    _refuse_archived(operation, board, actor=actor, card_id=card.pk)
    if not can_work_on_board(actor, board):
        _rejected(operation, 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError('Работа с карточками этой доски недоступна.')
    if card.parent_id is not None:
        _rejected(operation, 'subtask', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError('Подзадачу не переносят по колонкам: она живёт в своей карточке.')
    return board, card, task


def _refuse_closed_task(task, operation, *, actor, board, card):
    if task.status.is_final:
        _rejected(operation, 'task_final', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError(
            'Задача карточки закрыта. Вернуть её в работу может только администратор.'
        )


def move_card(card, *, actor, column, before_card_id=None):
    """Put a live card in a working column of its board: at the end, or before
    another card.

    `column` is an object or an id, a working column of any sub-board of the
    card's own board; a closing column, a column deleted meanwhile and a
    column of another board are refused. A column of another sub-board moves
    the card there (`sub_board` follows the column; the panel's «Переместить
    в…» puts it at the end, and its number stays). `before_card_id` must name
    another card standing in the target column. Positions are spaced by
    `POSITION_STEP`; when the gap is gone the card's group is renumbered under
    the same board lock. A pinned card (`is_pinned`) moves only among the
    pinned ones of its column, an unpinned one only after them; a card that
    leaves its column is unpinned.

    A card whose task is closed is refused: the closing column is reached by
    completing the task, and left only by an administrator reopening it
    (`reopen_card()`).

    A move to where the card already stands — its own column, between the
    same neighbours — writes nothing and announces nothing. A move into
    another column records one `MOVED` entry (the columns' names as they are
    now, and the sub-boards' when it changes them) and runs the new column's
    «Правила при входе» (`_apply_column_entry()`); the people they added are
    told, and the new column's followers hear the card entered it (once
    each).
    """
    from notifications.services import notify_board_task_assigned

    with transaction.atomic():
        board, card, task = _movable_card(card, 'move_card', actor=actor)
        target = _board_working_column(board, column, operation='move_card', actor=actor, card_id=card.pk)
        _refuse_closed_task(task, 'move_card', actor=actor, board=board, card=card)
        moved = _move_locked(board, card, task, target, actor=actor, before_card_id=before_card_id)
        if moved is None:
            return card
        previous_sub_board_id, previous_column_id, renumbered, added, edited = moved
        if edited:
            # The edit form holds the исполнители and the field values: one
            # drawn before this move must not put the old ones back silently.
            card.version += 1
            card.save(update_fields=['version'])
        if target.pk != previous_column_id:
            if added:
                notify_board_task_assigned(task, actor, _users(added))
            _notify_column_entered(card, task, target, board, actor=actor, told=added)
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
    returns it to the working column it came from. The card's followers and
    its author hear of it in the bell (`BOARD_CARD_COMPLETED`, keyed on the
    completion's time, so a card completed again after a reopening says so
    again); whoever completed it is not told. The closing column's followers
    hear the card entered it, and the cards it blocked that now wait for
    nobody tell their исполнители they may start (`_after_blocker_closed()`).
    A card made of a request tells the request's author it is done
    (`BOARD_REQUEST_DONE`, `_tell_requests_done()`).
    """
    from notifications.services import notify_board_card_completed
    from tasks.services import TaskWorkflowError, complete_task

    from .selectors import card_audience

    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        task = _lock_card_task(card)
        try:
            task = complete_task(task, actor, execution_comment)
        except TaskWorkflowError as exc:
            raise BoardError(str(exc)) from exc
        _record(card, BoardCardEvent.Kind.COMPLETED, actor=actor)
        # Its followers and its author, in the bell; whoever finished it knows.
        card.board = board
        # A subtask's author is the card's people, who hear «все подзадачи
        # выполнены» at the end: a subtask tells its followers only.
        audience = card_audience(card, task, assignees=False, author=card.parent_id is None)
        notify_board_card_completed(task, actor, audience)
        if card.parent_id is not None:
            parent = _record_subtask(card, 'completed', actor=actor)
            _ask_parent_done(parent, board, actor=actor, closed_at=task.completed_at)
        else:
            # The closing column's followers, but not whoever just heard of
            # the completion as a follower or the author.
            closing = next(
                (column for column in _sub_board_columns(card.sub_board) if column.is_done), None,
            )
            if closing is not None:
                _notify_column_entered(
                    card, task, closing, board, actor=actor, told=[user.pk for user in audience],
                )
        _after_blocker_closed(card, board, actor=actor, at=task.completed_at, cancelled=False)
        _tell_requests_done(card, board, actor=actor, at=task.completed_at)
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
    written to `column`), and the journal names that column. The open cards
    it blocks wait for it again, silently.
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
        if card.parent_id is not None:
            # A subtask returns to its card's list, not to a column.
            card.board = board
            _record(card, BoardCardEvent.Kind.REOPENED, actor=actor)
            _record_subtask(card, 'reopened', actor=actor)
        else:
            working = [column for column in _sub_board_columns(card.sub_board) if not column.is_done]
            column = next((column for column in working if column.pk == card.column_id), None)
            if column is None and working:
                column = working[0]
            _record(card, BoardCardEvent.Kind.REOPENED, actor=actor, **_column_snapshot('column', column))
        # The cards it blocks wait again — without a word to anybody; only
        # their tiles («⛔ ждёт …») change, on their own boards too.
        _announce_blocked(card, board)
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
    after the locks; its исполнители and its followers get one bell entry
    each (`notify_board_task_cancelled()`), never an email. A cancelled
    blocker blocks nothing any more (`_after_blocker_closed()`).
    """
    from notifications.services import notify_board_task_cancelled
    from tasks.services import TaskWorkflowError, cancel_board_card_task

    from .selectors import card_audience

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
        # Its исполнители and followers, in the bell only; whoever cancelled
        # is not told.
        notify_board_task_cancelled(task, actor, card_audience(card, task, author=False))
        _record(card, BoardCardEvent.Kind.CANCELLED, actor=actor)
        if card.parent_id is not None:
            parent = _record_subtask(card, 'cancelled', actor=actor)
            _ask_parent_done(parent, board, actor=actor, closed_at=task.cancelled_at)
        _after_blocker_closed(card, board, actor=actor, at=task.cancelled_at, cancelled=True)
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
# «Подзадачи»
# --------------------------------------------------------------------------
#
# A subtask is a real card — its own number from the board's one series, its
# исполнители, срок, «Чат», «Лог» and `BOARD` task — that lives inside its
# card (`BoardCard.parent`) and in no column. One level: a subtask has none
# of its own. Created by `create_subtask()`, `create_subtasks_from_list()` and
# `checklist_item_to_subtask()`, each one board lock → card → task, on an open
# card of a live board, by whoever works on the board (`can_add_subtask()`),
# at most `MAX_SUBTASKS` per card. Everything else a subtask does is the
# card's own service — `update_card()`, `complete_card()`, `reopen_card()`,
# `cancel_card()`, «Чат», the files, the «Чек-лист» — except `move_card()`,
# which refuses it. The parent's journal records each subtask added,
# completed, reopened and cancelled (`SUBTASK`), in that change's own
# transaction; when the last open subtask of an open card closes, its author
# and исполнители are asked «завершить?» (`BOARD_SUBTASKS_DONE`) — the card
# is never closed by itself, and closing a card with open subtasks is
# allowed: they stay in work with their own исполнители.


def _record_subtask(card, action, *, actor):
    """The parent's `SUBTASK` entry about `card` (a subtask, its `board`
    attached) — inside the caller's transaction. Returns the parent."""
    parent = BoardCard.objects.get(pk=card.parent_id)
    _record(parent, BoardCardEvent.Kind.SUBTASK, actor=actor, action=action, subtask_id=card.pk, code=card.code)
    return parent


def _ask_parent_done(parent, board, *, actor, closed_at):
    """«Все подзадачи карточки ZAP-12 выполнены»: once the subtask just
    closed was the last open one, at least one of them was really completed,
    and the card itself is still in work — a question to its author and its
    исполнители (`BOARD_SUBTASKS_DONE`), never a completion."""
    from notifications.services import notify_board_subtasks_done
    from tasks.models import Task

    from .selectors import card_audience

    parent_task = (
        Task.objects.select_related('status')
        .filter(source_type=Task.SourceType.BOARD, board_card=parent).first()
    )
    if parent_task is None or parent_task.status.code != 'IN_PROGRESS':
        return
    states = list(
        Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__parent=parent)
        .values_list('status__is_final', 'status__code')
    )
    if any(not is_final for is_final, _code in states) or all(code != 'COMPLETED' for _final, code in states):
        return
    parent.board = board
    notify_board_subtasks_done(
        parent_task, actor, card_audience(parent, parent_task, subscribers=False), closed_at=closed_at,
    )


def _subtask_parent(card, operation, *, actor):
    """The board, the card subtasks are added to and its task, locked in
    that order, if `actor` may add a subtask to it now."""
    board = _lock_board(card.board_id)
    card = _lock_card(card, board)
    task = _lock_card_task(card)
    card.board = board
    _refuse_archived(operation, board, actor=actor, card_id=card.pk)
    if not can_work_on_board(actor, board):
        _rejected(operation, 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError('Работа с карточками этой доски недоступна.')
    if card.parent_id is not None:
        _rejected(operation, 'nested_subtask', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError('У подзадачи не бывает подзадач — добавьте их в саму карточку.')
    if not can_add_subtask(actor, card, task, can_work=True):
        _rejected(operation, 'task_final', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError('Карточка закрыта — подзадачи добавляют только в карточку в работе.')
    return board, card, task


def _refuse_subtask_limit(board, card, adding, operation, *, actor):
    """At most `MAX_SUBTASKS` per card, cancelled ones included: a refusal
    for the whole request. Returns how many the card has now."""
    current = BoardCard.objects.filter(parent=card).count()
    if current + adding > MAX_SUBTASKS:
        _rejected(operation, 'limit', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError(
            f'У карточки может быть не больше {MAX_SUBTASKS} подзадач: сейчас {current}'
            + (f', добавляется {adding}.' if adding > 1 else '.')
        )
    return current


def _parent_assignee_ids(board, task):
    """The parent's исполнители who may still be put on a subtask — its
    default; an empty answer is a refusal."""
    from tasks.models import TaskAssignee

    ids = sorted(
        BoardMember.objects.filter(
            active_employee_q('user__'), board=board,
            user_id__in=TaskAssignee.objects.filter(task=task).values('user_id'),
        ).values_list('user_id', flat=True)
    )
    if not ids:
        raise BoardError('У карточки нет исполнителей, которые сейчас на доске: укажите исполнителя подзадачи.')
    return ids


def _add_subtask(board, parent, parent_task, *, actor, title, ids, due_date, position,
                 description='', values=None):
    """One subtask of `parent` — `_new_card()` with no column — its
    исполнители told and the parent's `SUBTASK` entry, inside the caller's
    transaction under the locks it holds. `ids` are already checked."""
    from notifications.services import notify_board_task_assigned

    subtask, task = _new_card(
        board, parent.sub_board, actor=actor, title=title, description=description,
        due_date=due_date, ids=ids, values=values or {}, column=None, position=position,
        parent=parent,
    )
    subtask.board = board
    subtask.parent = parent
    _record(parent, BoardCardEvent.Kind.SUBTASK, actor=actor, action='added', subtask_id=subtask.pk, code=subtask.code)
    notify_board_task_assigned(task, actor, _users(ids))
    return subtask, task


def _next_subtask_position(card):
    return (BoardCard.objects.filter(parent=card).aggregate(last=Max('position'))['last'] or 0) + 1


def create_subtask(parent, *, actor, title, assignees=None, due_date=None, description='',
                   field_values=None):
    """«+ Подзадача»: a new subtask at the end of `parent`'s list.

    Whoever works on the board (`can_add_subtask()`), on an open card that is
    not itself a subtask, never on an archived board, at most `MAX_SUBTASKS`.
    `assignees` and `due_date` are what the form sends — it starts from the
    parent's; `None` here means exactly that default (the parent's
    исполнители still on the board, the parent's срок), so a caller with no
    form gets the same. An empty list of исполнители is refused, as by
    `create_card()`. A срок later than the parent's is not refused — the
    form warns. The subtask takes the board's next number, its исполнители
    are told (`BOARD_TASK_ASSIGNED`), its journal gets `CREATED` and the
    parent's `SUBTASK` (`added`); one `board.updated(card_created)` with the
    subtask's id.
    """
    with transaction.atomic():
        board, parent, parent_task = _subtask_parent(parent, 'create_subtask', actor=actor)
        title = _clean_title(title)
        description = (description or '').strip()
        ids = (
            _parent_assignee_ids(board, parent_task) if assignees is None
            else _clean_assignees(board, assignees)
        )
        if due_date is None:
            due_date = parent_task.due_date
        values = _clean_field_values(board, field_values, {})
        _refuse_subtask_limit(board, parent, 1, 'create_subtask', actor=actor)
        subtask, task = _add_subtask(
            board, parent, parent_task, actor=actor, title=title, ids=ids, due_date=due_date,
            position=_next_subtask_position(parent), description=description, values=values,
        )
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_CREATED, subtask.pk)
    log_event(
        logger,
        'INFO',
        'board.subtask_created',
        board_id=board.pk,
        board_card_id=subtask.pk,
        parent_card_id=parent.pk,
        task_id=task.pk,
        actor_user_id=actor.pk,
        assignee_count=len(ids),
        outcome='ok',
    )
    return subtask


def create_subtasks_from_list(parent, *, actor, text):
    """«Добавить списком»: one subtask per non-empty line of `text`.

    Each with the parent's исполнители (those still on the board) and its
    срок, in the order of the lines. All or nothing, in one transaction: a
    line over `TITLE_MAX_LENGTH` or more lines than the limit leaves refuses
    the whole list, before anything is written. One `board.updated` for the
    whole list (`card_created`, the parent's id). Returns the subtasks.
    """
    lines = [line.strip() for line in (text or '').splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise BoardError('Напишите хотя бы одну строку — одна строка, одна подзадача.')
    for index, line in enumerate(lines, start=1):
        if len(line) > TITLE_MAX_LENGTH:
            raise BoardError(f'Строка {index} длиннее {TITLE_MAX_LENGTH} символов — сократите её.')
    with transaction.atomic():
        board, parent, parent_task = _subtask_parent(parent, 'create_subtasks_from_list', actor=actor)
        _refuse_subtask_limit(board, parent, len(lines), 'create_subtasks_from_list', actor=actor)
        ids = _parent_assignee_ids(board, parent_task)
        position = _next_subtask_position(parent)
        subtasks = []
        for offset, line in enumerate(lines):
            subtask, _task = _add_subtask(
                board, parent, parent_task, actor=actor, title=line, ids=ids,
                due_date=parent_task.due_date, position=position + offset,
            )
            subtasks.append(subtask)
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_CREATED, parent.pk)
    log_event(
        logger,
        'INFO',
        'board.subtasks_created',
        board_id=board.pk,
        parent_card_id=parent.pk,
        subtask_count=len(subtasks),
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return subtasks


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
        _refuse_action_target(
            board, BoardColumn.objects.filter(sub_board=sub_board).values_list('pk', flat=True),
            'delete_sub_board', actor=actor,
        )
        sub_board_id = sub_board.pk
        BoardColumn.objects.filter(sub_board=sub_board).delete()
        sub_board.delete()
        _renumber_rows(list(SubBoard.objects.filter(board=board).order_by('position', 'pk')))
        _structure_changed(board, 'board.sub_board_deleted', actor=actor, sub_board_id=sub_board_id)


def _refuse_action_target(board, column_ids, operation, *, actor):
    """Refuse to delete columns an action of the board leads to — archived
    actions included (`BoardAction.target_column` is `PROTECT`)."""
    names = list(
        BoardAction.objects.filter(board=board, target_column_id__in=list(column_ids))
        .order_by('position', 'pk').values_list('name', flat=True)
    )
    if names:
        _rejected(operation, 'action_target', actor=actor, board_id=board.pk)
        quoted = ', '.join(f'«{name}»' for name in names)
        raise BoardError(
            f'На эту колонку ведут действия доски: {quoted}. Сначала выберите для них '
            'другую колонку на странице «Действия».'
        )


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
        _refuse_action_target(board, [column.pk], 'delete_column', actor=actor)
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
    next card created in the column or moved into it (`_apply_column_entry()`).
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


def set_column_norm(column, *, actor, days):
    """«Норматив этапа» of a working column: a card should stand in it at
    most `days` working days (`ecosystem.workdays`); `None` (or an empty
    value) removes the norm.

    The manager's, like every other part of the structure: one board lock,
    never an archived board, never the closing column (a completed card has
    no stage left to be late in), 1 to `NORM_DAYS_MAX` working days. The same
    norm again stores and announces nothing; a change publishes one
    `board.updated(structure_changed)` and touches the column's `updated_at`
    (the `boards` sync revision). Only the current norm is kept: the stage
    path and the report read past stays against it.
    """
    if days in (None, ''):
        days = None
    else:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise BoardError('Укажите число рабочих дней или оставьте поле пустым.') from None
    with transaction.atomic():
        board = _manageable_board(column.sub_board.board_id, 'set_column_norm', actor=actor)
        column = _column_of(board, column, operation='set_column_norm', actor=actor)
        if column.is_done:
            _rejected('set_column_norm', 'done_column', actor=actor, board_id=board.pk)
            raise BoardError(
                'В завершающей колонке работа уже выполнена — норматива этапа у неё нет.'
            )
        if days is not None and not NORM_DAYS_MIN <= days <= NORM_DAYS_MAX:
            _rejected('set_column_norm', 'out_of_range', actor=actor, board_id=board.pk)
            raise BoardError(
                f'Норматив этапа — число рабочих дней от {NORM_DAYS_MIN} до {NORM_DAYS_MAX} '
                'или пусто.'
            )
        if column.norm_working_days == days:
            return column
        column.norm_working_days = days
        column.save(update_fields=['norm_working_days', 'updated_at'])
        _structure_changed(
            board, 'board.column_norm_changed', actor=actor,
            column_id=column.pk, norm_working_days=days,
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
# «Сумма в колонке»: how many number fields of a board a column header sums.
MAX_SUMMED_FIELDS = 2
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


def _refuse_ruled_field(board, field, operation, *, actor, what):
    """Refuse to delete a field, or change its kind, while a column's entry
    rule or an action sets it (`BoardFieldRule.field` is `PROTECT`)."""
    if (
        BoardColumnFieldRule.objects.filter(field=field).exists()
        or BoardActionFieldRule.objects.filter(field=field).exists()
    ):
        _rejected(operation, 'field_in_rules', actor=actor, board_id=board.pk)
        raise BoardError(
            f'Поле «{field.name}» ставят правила колонок или действия доски — {what} нельзя. '
            'Сначала уберите его из правил.'
        )


def _refuse_summed(board, kind, sum_in_column, operation, *, actor, exclude_pk=None):
    """«Сумма в колонке» only for a number field, and on at most
    `MAX_SUMMED_FIELDS` live fields of the board: a column header has room
    for two sums, not for every number on the card."""
    if not sum_in_column:
        return
    if kind != BoardField.Kind.NUMBER:
        _rejected(operation, 'sum_not_number', actor=actor, board_id=board.pk)
        raise BoardError('Сумму в колонке считают только для поля вида «Число».')
    summed = BoardField.objects.filter(board=board, is_archived=False, sum_in_column=True)
    if exclude_pk is not None:
        summed = summed.exclude(pk=exclude_pk)
    if summed.count() >= MAX_SUMMED_FIELDS:
        _rejected(operation, 'sum_limit', actor=actor, board_id=board.pk)
        raise BoardError(
            f'Сумму в колонке можно считать не больше чем по {MAX_SUMMED_FIELDS} полям доски.'
        )


def create_field(board, *, actor, name, kind, show_on_tile=True, options=(), sum_in_column=False):
    """A new field at the end of the board's fields.

    `options` — `(label, colour)` pairs — only for a list (`SELECT`): any
    other kind takes none. At most `MAX_FIELDS` live fields per board and
    `MAX_OPTIONS` options per field. `sum_in_column` («Сумма в колонке»): a
    number field only, at most `MAX_SUMMED_FIELDS` per board.
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
        sum_in_column = bool(sum_in_column)
        _refuse_summed(board, kind, sum_in_column, 'create_field', actor=actor)
        last = BoardField.objects.filter(board=board).aggregate(last=Max('position'))['last'] or 0
        field = BoardField.objects.create(
            board=board, name=name, kind=kind, position=last + 1, show_on_tile=bool(show_on_tile),
            sum_in_column=sum_in_column,
        )
        BoardFieldOption.objects.bulk_create([
            BoardFieldOption(field=field, label=label, color=color, position=index)
            for index, (label, color) in enumerate(cleaned, start=1)
        ])
        _fields_changed(board, 'board.field_created', actor=actor, field_id=field.pk)
    return field


def update_field(field, *, actor, name, show_on_tile, kind=None, sum_in_column=None):
    """A field's name, whether its tile shows it, whether its column headers
    sum it, and — while no card holds a value of it — its kind. `kind=None`
    keeps the kind, `sum_in_column=None` the sum.

    A list turned into another kind loses its options (none can be used: the
    field has no values); a number turned into another kind stops being
    summed. The same values store and publish nothing.
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
        if kind != field.kind:
            _refuse_ruled_field(board, field, 'update_field', actor=actor, what='менять его вид')
        if sum_in_column is None:
            sum_in_column = field.sum_in_column and kind == BoardField.Kind.NUMBER
        sum_in_column = bool(sum_in_column)
        if sum_in_column and not field.sum_in_column or kind != field.kind:
            _refuse_summed(
                board, kind, sum_in_column, 'update_field', actor=actor, exclude_pk=field.pk,
            )
        changed = [
            attribute for attribute, value in (
                ('name', name), ('kind', kind), ('show_on_tile', show_on_tile),
                ('sum_in_column', sum_in_column),
            )
            if getattr(field, attribute) != value
        ]
        if not changed:
            return field
        if 'kind' in changed and field.kind == BoardField.Kind.SELECT:
            BoardFieldOption.objects.filter(field=field).delete()
        field.name, field.kind, field.show_on_tile = name, kind, show_on_tile
        field.sum_in_column = sum_in_column
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
        # An archived field is summed nowhere, and comes back without its sum:
        # turning it on again is asked within `MAX_SUMMED_FIELDS`.
        field.sum_in_column = False
        field.save(update_fields=['is_archived', 'sum_in_column', 'updated_at'])
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
        _refuse_ruled_field(board, field, 'delete_field', actor=actor, what='удалить его')
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
        ruled = Q(field_id=option.field_id, value=str(option.pk))
        if BoardColumnFieldRule.objects.filter(ruled).exists() or BoardActionFieldRule.objects.filter(ruled).exists():
            _rejected('delete_option', 'option_in_rules', actor=actor, board_id=board.pk)
            raise BoardError(
                f'Вариант «{option.label}» ставят правила колонок или действия доски — '
                'сначала уберите его из правил.'
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
    Refused too while a request waits in «Входящие» (`NEW`): its author would
    wait for an answer that cannot come. Writes `status`, `archived_at` and
    `archived_by` and nothing else.
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
        # Nor a request nobody answered: its author would wait for ever.
        new_requests = BoardRequest.objects.filter(board=board, status=BoardRequest.Status.NEW).count()
        if new_requests:
            _rejected('archive_board', 'new_requests', actor=actor, board_id=board.pk)
            raise BoardError(f'Сначала разберите новые заявки во «Входящих»: {new_requests}.')
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


def _clean_message_files(files):
    """The files of one message, refused by the one upload policy before
    anything is written: at most `MAX_FILES_PER_MESSAGE`, each of an allowed
    type and size (`ecosystem.attachments`). The refusal names the file —
    the person who chose it reads it, no log line does."""
    from django.core.exceptions import ValidationError

    from ecosystem.attachments import validate_attachment_upload

    files = [item for item in (files or ()) if item]
    if len(files) > MAX_FILES_PER_MESSAGE:
        raise BoardError(f'К одному сообщению — не больше {MAX_FILES_PER_MESSAGE} файлов.')
    for item in files:
        try:
            validate_attachment_upload(item)
        except ValidationError as exc:
            raise BoardError(f'Файл «{item.name}»: {" ".join(exc.messages)}') from exc
    return files


def _remove_stored_file(storage, name, *, card_id, file_id=None, operation):
    """Best-effort removal of one stored file of «Чат»: after a rollback, or
    once a deletion has committed. Identifiers only in the log."""
    if not name:
        return
    try:
        storage.delete(name)
    except OSError as exc:
        log_event(
            logger,
            'ERROR',
            'board.file_cleanup_failed',
            board_card_id=card_id,
            board_card_file_id=file_id,
            operation=operation,
            error_type=type(exc).__name__,
            outcome='orphaned_file',
        )


def post_card_comment(card, *, actor, text, mentions=(), files=()):
    """One message in a card's «Чат»: text, files or both. No editing, no
    deletion of the message.

    Locks the board, then the card, and asks `can_comment_card()` after the
    locks — an active member or an administrator, not on an archived board;
    the state of the task does not matter, so a closed card is discussed and
    takes files too. The text is stripped and at most `COMMENT_MAX_LENGTH`
    characters; it may be empty only beside at least one file.

    `files` are uploaded files (`BoardCardFile`, at most
    `MAX_FILES_PER_MESSAGE`), each checked by the project's one upload policy
    (`ecosystem.attachments`: type and size) before anything is written — a
    refusal stores no row and leaves no file. Storage is not transactional,
    so the files are written first, then the rows; if anything after that
    fails, the files written are removed again before the error goes on.

    `mentions` are the ids «@» put beside the message (`mention=<id>`). Only
    the readers of the board who are active employees are kept
    (`board_readers_q()`) — a foreign, inactive, repeated or malformed id and
    the writer's own are dropped without a word — and stored as
    `BoardCardCommentMention` rows in the same transaction; each of them
    follows the card from now on (`BoardCardSubscription`, if not already).

    Who is told, one notification per person per message: everybody
    mentioned gets `BOARD_CARD_MENTION` (bell and mail); the card's audience
    (`selectors.card_audience()`: its исполнители, its author, its followers)
    gets `BOARD_CARD_COMMENT` in the bell — except those mentioned, who have
    theirs already, and the writer, who is told nothing. Neither names the
    message nor a file: a file's name is somebody's text too. The board
    publishes one `board.updated(comment_added)` per message, files or not.
    Logged by identifiers and sizes only, never the text or a name.
    """
    from notifications.services import notify_board_card_comment, notify_board_card_mention
    from tasks.models import Task

    from .selectors import card_audience

    text = (text or '').strip()
    requested = set()
    for value in mentions or ():
        try:
            requested.add(int(getattr(value, 'pk', value)))
        except (TypeError, ValueError):
            continue
    written = []   # (storage, name) of every file put on the disk so far
    try:
        with transaction.atomic():
            board = _lock_board(card.board_id)
            card = _lock_card(card, board)
            card.board = board
            _refuse_archived('post_comment', board, actor=actor, card_id=card.pk)
            if not can_comment_card(actor, card):
                _rejected('post_comment', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
                raise BoardError('Писать в обсуждение могут участники доски.')
            uploads = _clean_message_files(files)
            if not text and not uploads:
                raise BoardError('Напишите сообщение или прикрепите файл.')
            if len(text) > COMMENT_MAX_LENGTH:
                raise BoardError(f'Сообщение — не длиннее {COMMENT_MAX_LENGTH} символов.')
            # The files first, then the rows: a row never points at nothing.
            stored = []
            for upload in uploads:
                card_file = BoardCardFile(
                    card=card,
                    uploaded_by=actor,
                    original_name=(upload.name or 'файл')[:255],
                    size=getattr(upload, 'size', 0) or 0,
                    content_type=(getattr(upload, 'content_type', '') or '')[:120],
                )
                card_file.file.save(upload.name, upload, save=False)
                written.append((card_file.file.storage, card_file.file.name))
                stored.append(card_file)
            comment = BoardCardComment.objects.create(card=card, author=actor, text=text)
            for card_file in stored:
                card_file.comment = comment
                card_file.save()
            requested.discard(actor.pk)
            mentioned = (
                list(
                    get_user_model().objects.filter(board_readers_q(board), pk__in=requested)
                    .distinct().order_by('pk')
                ) if requested else []
            )
            if mentioned:
                BoardCardCommentMention.objects.bulk_create(
                    [BoardCardCommentMention(comment=comment, user=user) for user in mentioned]
                )
                following = set(
                    BoardCardSubscription.objects.filter(card=card, user__in=mentioned)
                    .values_list('user_id', flat=True)
                )
                BoardCardSubscription.objects.bulk_create(
                    [BoardCardSubscription(card=card, user=user) for user in mentioned if user.pk not in following]
                )
            task = Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)
            mentioned_ids = {user.pk for user in mentioned}
            notify_board_card_mention(comment, task, actor, mentioned)
            notify_board_card_comment(
                comment, task, actor,
                [user for user in card_audience(card, task) if user.pk not in mentioned_ids],
            )
            emit_board_updated(board.pk, BOARD_CHANGE_COMMENT_ADDED, card.pk)
    except Exception:
        for storage, name in written:
            _remove_stored_file(storage, name, card_id=card.pk, operation='post_comment_rollback')
        raise
    log_event(
        logger,
        'INFO',
        'board.comment_posted',
        board_id=board.pk,
        board_card_id=card.pk,
        comment_id=comment.pk,
        mention_count=len(mentioned),
        file_count=len(stored),
        size_bytes=sum(card_file.size for card_file in stored),
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return comment


def delete_card_file(card_file, *, actor):
    """«×» beside a file of «Чат»: the file goes, its row stays as a tombstone.

    Locks the board, then the card, then the file row, and asks
    `can_delete_card_file()` after the locks: whoever uploaded it while still
    working on the board, or an administrator; an archived board is refused
    first. The row keeps who uploaded what and when, and gets
    `deleted_at`/`deleted_by` and an empty `file`, so the message still says
    «Файл удалён»; the file leaves the disk only once the transaction has
    committed (`on_commit`), so a rollback never leaves a row without its
    file. The card's `updated_at` moves (the `boards` sync revision), and the
    board publishes one `board.updated(file_deleted)`.

    Returns `True` when the file was deleted now and `False` when it already
    was — deleting twice is not an error, and the second time stores and
    publishes nothing.
    """
    with transaction.atomic():
        board = _lock_board(card_file.card.board_id)
        card = _lock_card(card_file.card, board)
        card.board = board
        _refuse_archived('delete_file', board, actor=actor, card_id=card.pk)
        try:
            locked = BoardCardFile.objects.select_for_update().get(pk=card_file.pk, card=card)
        except BoardCardFile.DoesNotExist as exc:
            raise BoardError('Файл не найден в этой карточке.') from exc
        if locked.deleted_at is not None:
            return False
        if not can_delete_card_file(actor, locked, board):
            _rejected('delete_file', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Удалить файл может тот, кто его прикрепил, или администратор.')
        storage, name = locked.file.storage, locked.file.name
        locked.deleted_at = timezone.now()
        locked.deleted_by = actor
        locked.file = ''
        locked.save(update_fields=['deleted_at', 'deleted_by', 'file'])
        card.save(update_fields=['updated_at'])
        transaction.on_commit(partial(
            _remove_stored_file, storage, name,
            card_id=card.pk, file_id=locked.pk, operation='delete_file',
        ))
        emit_board_updated(board.pk, BOARD_CHANGE_FILE_DELETED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.file_deleted',
        board_id=board.pk,
        board_card_id=card.pk,
        board_card_file_id=locked.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return True


# --------------------------------------------------------------------------
# «Следить»
# --------------------------------------------------------------------------


def toggle_card_subscription(card, *, actor, subscribe=None):
    """«Следить» / «Вы следите» on a card: follow it, or stop.

    Any reader of the board (`can_follow_card()`), whatever the state of the
    card, never on an archived board. `subscribe` is the state asked for —
    the button posts it, so a double click or a stale tab asks the same thing
    twice and changes nothing the second time; `None` flips. Personal: no
    `board.updated` (nobody else sees it change) and no journal entry.
    Returns whether `actor` follows the card afterwards.
    """
    with transaction.atomic():
        board = _lock_board(card.board_id)
        card = _lock_card(card, board)
        card.board = board
        _refuse_archived('toggle_subscription', board, actor=actor, card_id=card.pk)
        if not can_follow_card(actor, card):
            _rejected('toggle_subscription', 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError('Следить за карточкой могут читатели доски.')
        rows = BoardCardSubscription.objects.filter(card=card, user=actor)
        current = rows.exists()
        wanted = (not current) if subscribe is None else bool(subscribe)
        if wanted == current:
            return current
        if wanted:
            BoardCardSubscription.objects.create(card=card, user=actor)
        else:
            rows.delete()
    log_event(
        logger,
        'INFO',
        'board.card_subscription_changed',
        board_id=board.pk,
        board_card_id=card.pk,
        actor_user_id=actor.pk,
        subscribed=wanted,
        outcome='ok',
    )
    return wanted


# --------------------------------------------------------------------------
# «Чек-лист»
# --------------------------------------------------------------------------
#
# A working list of steps on a card — «Запуск заказа»: a few items, ticked
# one by one. Whoever works on the board changes it (`can_work_on_board()`),
# on an open card only: a completed or cancelled card keeps its list as it
# was, and an archived board changes nothing. Each write locks the board,
# then the card (and reads its task under them), and re-asks the right and
# the state after the locks. The same text, the same tick, a move past the
# edge store and announce nothing; anything else touches the card's
# `updated_at` (the `boards` sync revision), writes one journal entry
# (`CHECKLIST`: the action, the item's id and «сделано/всего» after it, never
# the text; a reorder writes none) and publishes one
# `board.updated(checklist_changed)`. Deleting an item deletes it — this is a
# working list, not a record; the journal keeps that it happened.

def _checklist_card(card, operation, *, actor):
    """The board, the card and its task, locked in that order, if `actor`
    may change the card's «Чек-лист» now."""
    board = _lock_board(card.board_id)
    card = _lock_card(card, board)
    task = _lock_card_task(card)
    card.board = board
    _refuse_archived(operation, board, actor=actor, card_id=card.pk)
    if not can_work_on_board(actor, board):
        _rejected(operation, 'not_permitted', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError('Работа с карточками этой доски недоступна.')
    if task.status.code != 'IN_PROGRESS':
        _rejected(operation, 'task_final', actor=actor, board_id=board.pk, card_id=card.pk)
        raise BoardError('Задача карточки закрыта — чек-лист больше не меняется.')
    return board, card, task


def _checklist_item(card, item, operation, *, actor):
    """`item` (an object or an id), re-read under the card's lock, only if
    it is on this very card."""
    item_id = getattr(item, 'pk', item)
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        item_id = None
    found = (
        BoardCardChecklistItem.objects.filter(pk=item_id, card=card).first()
        if item_id is not None else None
    )
    if found is None:
        _rejected(operation, 'unknown_item', actor=actor, board_id=card.board_id, card_id=card.pk)
        raise BoardError('Пункт чек-листа не найден — возможно, его удалили. Обновите страницу.')
    return found


def _clean_checklist_text(text):
    text = (text or '').strip()
    if not text:
        raise BoardError('Напишите пункт чек-листа.')
    if len(text) > CHECKLIST_TEXT_MAX_LENGTH:
        raise BoardError(f'Пункт чек-листа — не длиннее {CHECKLIST_TEXT_MAX_LENGTH} символов.')
    return text


def _checklist_items(card):
    return list(BoardCardChecklistItem.objects.filter(card=card).order_by('position', 'pk'))


def _checklist_changed(board, card, action, *, actor, item_id, **details):
    """What every write of a «Чек-лист» ends with: the card's `updated_at`,
    the journal entry (none for a reorder), one event and the log line.
    `details` are added to the entry — identifiers only."""
    counts = BoardCardChecklistItem.objects.filter(card=card).aggregate(
        total=Count('pk'), done=Count('pk', filter=Q(is_done=True)),
    )
    card.save(update_fields=['updated_at'])
    if action != 'moved':
        _record(
            card, BoardCardEvent.Kind.CHECKLIST, actor=actor,
            action=action, item_id=item_id, done=counts['done'], total=counts['total'], **details,
        )
    emit_board_updated(board.pk, BOARD_CHANGE_CHECKLIST_CHANGED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.checklist_changed',
        board_id=board.pk,
        board_card_id=card.pk,
        checklist_item_id=item_id,
        action=action,
        done_count=counts['done'],
        total_count=counts['total'],
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return counts['done'], counts['total']


def add_checklist_item(card, *, actor, text):
    """A new item at the end of the card's «Чек-лист», not ticked.

    Trimmed, required, at most `CHECKLIST_TEXT_MAX_LENGTH`; at most
    `MAX_CHECKLIST_ITEMS` items per card.
    """
    with transaction.atomic():
        board, card, _task = _checklist_card(card, 'add_checklist_item', actor=actor)
        text = _clean_checklist_text(text)
        items = _checklist_items(card)
        if len(items) >= MAX_CHECKLIST_ITEMS:
            _rejected('add_checklist_item', 'limit', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(f'В чек-листе может быть не больше {MAX_CHECKLIST_ITEMS} пунктов.')
        item = BoardCardChecklistItem.objects.create(
            card=card,
            text=text,
            position=(items[-1].position if items else 0) + 1,
            created_by=actor,
        )
        _checklist_changed(board, card, 'added', actor=actor, item_id=item.pk)
    return item


def rename_checklist_item(item, *, actor, text):
    """New wording for an item; the same wording changes nothing."""
    with transaction.atomic():
        board, card, _task = _checklist_card(item.card, 'rename_checklist_item', actor=actor)
        item = _checklist_item(card, item, 'rename_checklist_item', actor=actor)
        text = _clean_checklist_text(text)
        if text == item.text:
            return item
        item.text = text
        item.save(update_fields=['text'])
        _checklist_changed(board, card, 'renamed', actor=actor, item_id=item.pk)
    return item


def toggle_checklist_item(item, *, actor, done=None):
    """Tick an item, or take the tick off.

    `done` is the state asked for — the checkbox posts it, so a double click
    or a stale tab asks the same thing twice and the second time changes
    nothing; `None` flips. A tick records who and when (`done_by`/`done_at`);
    taking it off clears both.
    """
    with transaction.atomic():
        board, card, _task = _checklist_card(item.card, 'toggle_checklist_item', actor=actor)
        item = _checklist_item(card, item, 'toggle_checklist_item', actor=actor)
        wanted = (not item.is_done) if done is None else bool(done)
        if wanted == item.is_done:
            return item
        item.is_done = wanted
        item.done_by = actor if wanted else None
        item.done_at = timezone.now() if wanted else None
        item.save(update_fields=['is_done', 'done_by', 'done_at'])
        _checklist_changed(board, card, 'done' if wanted else 'undone', actor=actor, item_id=item.pk)
    return item


def move_checklist_item(item, *, actor, direction):
    """One place up (`'up'`) or down (`'down'`); at the edge, nothing.

    A reorder is no journal entry — the list says the same — but it is an
    event: every open panel shows the new order.
    """
    if direction not in ('up', 'down'):
        raise BoardError('Неизвестное направление.')
    with transaction.atomic():
        board, card, _task = _checklist_card(item.card, 'move_checklist_item', actor=actor)
        item = _checklist_item(card, item, 'move_checklist_item', actor=actor)
        items = _checklist_items(card)
        index = next(i for i, other in enumerate(items) if other.pk == item.pk)
        other = index - 1 if direction == 'up' else index + 1
        if other < 0 or other >= len(items):
            return item
        items[index], items[other] = items[other], items[index]
        _renumber_checklist(items)
        _checklist_changed(board, card, 'moved', actor=actor, item_id=item.pk)
    return item


def delete_checklist_item(item, *, actor):
    """Delete an item for real; the ones after it move up a place."""
    with transaction.atomic():
        board, card, _task = _checklist_card(item.card, 'delete_checklist_item', actor=actor)
        item = _checklist_item(card, item, 'delete_checklist_item', actor=actor)
        item_id = item.pk
        item.delete()
        _renumber_checklist(_checklist_items(card))
        _checklist_changed(board, card, 'deleted', actor=actor, item_id=item_id)


def _renumber_checklist(items):
    """Positions 1, 2, 3, … for `items` in their new order — only the rows
    whose position really changes are written."""
    changed = []
    for index, row in enumerate(items, start=1):
        if row.position != index:
            row.position = index
            changed.append(row)
    if changed:
        BoardCardChecklistItem.objects.bulk_update(changed, ['position'])


def checklist_item_to_subtask(item, *, actor):
    """«В подзадачу» of an item of a card's «Чек-лист»: a subtask with the
    item's text, and the item gone — one transaction.

    The right is both: changing the list (`can_edit_checklist()`) and adding
    a subtask (`can_add_subtask()`: the card open, not itself a subtask,
    under `MAX_SUBTASKS`). The subtask gets the card's исполнители and срок,
    as from a list. The card's journal gets the `CHECKLIST` entry (`action`
    `to_subtask`, the item's id and the subtask's code) and the `SUBTASK` one
    (`added`), the subtask its `CREATED`; one `board.updated(checklist_changed)`.
    """
    with transaction.atomic():
        board, card, task = _subtask_parent(item.card, 'checklist_to_subtask', actor=actor)
        item = _checklist_item(card, item, 'checklist_to_subtask', actor=actor)
        _refuse_subtask_limit(board, card, 1, 'checklist_to_subtask', actor=actor)
        subtask, _sub_task = _add_subtask(
            board, card, task, actor=actor, title=_clean_title(item.text),
            ids=_parent_assignee_ids(board, task), due_date=task.due_date,
            position=_next_subtask_position(card),
        )
        item_id = item.pk
        item.delete()
        _renumber_checklist(_checklist_items(card))
        _checklist_changed(
            board, card, 'to_subtask', actor=actor, item_id=item_id,
            subtask_id=subtask.pk, code=subtask.code,
        )
    return subtask


# --------------------------------------------------------------------------
# Напоминания о сроке
# --------------------------------------------------------------------------


def send_due_reminders(today=None):
    """What `manage.py board_due_reminders` does once a day.

    Every open `BOARD` task (`IN_PROGRESS`) of a live board — a card or a
    subtask alike:
    - due today or on the next working day
      (`ecosystem.workdays.add_working_days(today, 1)`: a Friday reaches
      Monday) → «Завтра срок карточки ZAP-12» (`BOARD_DUE_SOON`) to its
      исполнители;
    - past its срок → «Карточка ZAP-12 просрочена» (`BOARD_OVERDUE`) to its
      исполнители, author and followers.
    Only people who still read the board (`selectors.card_audience()`). Both
    are keyed on the task and its срок, so running again the same day creates
    nothing, and a срок moved asks again. Writes notifications and nothing
    else — no journal entry, no `board.updated`. Returns
    `{'due_soon': n, 'overdue': n}`, the notifications created; one log line,
    numbers only.
    """
    from ecosystem.workdays import add_working_days
    from notifications.services import notify_board_due_soon, notify_board_overdue
    from tasks.models import Task

    from .selectors import card_audience

    today = today or timezone.localdate()
    horizon = add_working_days(today, 1)
    open_tasks = (
        Task.objects.filter(
            source_type=Task.SourceType.BOARD,
            status__code='IN_PROGRESS',
            board_card__board__status=Board.Status.ACTIVE,
        )
        .select_related('board_card__board', 'board_card__parent')
        .order_by('due_date', 'pk')
    )
    created = {'due_soon': 0, 'overdue': 0}
    for task in open_tasks.filter(due_date__gte=today, due_date__lte=horizon):
        recipients = card_audience(task.board_card, task, author=False, subscribers=False)
        created['due_soon'] += len(notify_board_due_soon(task, recipients))
    for task in open_tasks.filter(due_date__lt=today):
        created['overdue'] += len(notify_board_overdue(task, card_audience(task.board_card, task)))
    log_event(
        logger,
        'INFO',
        'board.due_reminders',
        due_soon_count=created['due_soon'],
        overdue_count=created['overdue'],
        outcome='ok',
    )
    return created


# --------------------------------------------------------------------------
# «Связи»: links between cards, and the blocking they carry
# --------------------------------------------------------------------------
#
# A link is made from a card (`link_cards()`) to another one named by its
# code («СНБ-14»), on this board or another the author reads, and removed
# (`unlink_cards()`) from either end. Both write under the locks of both
# boards (in id order, so two links made at once from the two ends never wait
# on each other) and then both cards; both cards' journals get one entry
# (`LINKED`/`UNLINKED`: the link's kind, the direction, the other card's id
# and code — never its title), both cards' `updated_at` moves (the `boards`
# sync revision) and each board hears one `board.updated(card_updated)` with
# its own card's id.
#
# «Ждёт» is a `BLOCKS` link from a card still `IN_PROGRESS`. When a blocker
# is completed or cancelled, `_after_blocker_closed()` — inside that very
# transaction — tells the people of every card it blocked that no longer
# waits for anybody (`BOARD_UNBLOCKED`); reopening a blocker blocks them
# again and tells nobody. Either way the boards of the cards it blocks hear
# `board.updated(card_updated)`, since their tiles («⛔ ждёт …») change.

# What a person picks beside the other card's code: «Ждёт» stores a `BLOCKS`
# link the other way round (the other card blocks this one).
LINK_WAITS = 'WAITS'
LINK_CHOICES = (
    (LINK_WAITS, 'Ждёт'),
    (BoardCardLink.Kind.BLOCKS, 'Блокирует'),
    (BoardCardLink.Kind.RELATES, 'Связана с'),
    (BoardCardLink.Kind.DUPLICATES, 'Дублирует'),
)
# One refusal for a code no card answers to and for a card of a board the
# author does not read: the second must not be told apart from the first.
LINK_NOT_FOUND = 'Карточка не найдена.'


def _linked_card_id(code, actor):
    """The id of the card `code` names on a board `actor` reads, or `None`.

    Through `tasks.selectors.board_card_code_filter()` — the one parser of a
    card's code — over the `BOARD` tasks, one task per card.
    """
    from tasks.models import Task
    from tasks.selectors import board_card_code_filter

    condition = board_card_code_filter(code)
    if condition is None:
        return None
    return (
        Task.objects.filter(condition)
        .filter(readable_boards_q(actor, 'board_card__board_id'))
        .values_list('board_card_id', flat=True)
        .first()
    )


def _lock_link_cards(card_ids):
    """`{card id: card}` with `board` attached, both boards and then both
    cards locked, each pair in id order. Missing ids are simply absent."""
    board_ids = sorted(set(
        BoardCard.objects.filter(pk__in=card_ids).values_list('board_id', flat=True)
    ))
    boards = {board_id: _lock_board(board_id) for board_id in board_ids}
    cards = {
        card.pk: card
        for card in BoardCard.objects.select_for_update().filter(pk__in=card_ids).order_by('pk')
    }
    for card in cards.values():
        card.board = boards[card.board_id]
    return cards


def _link_written(link, kind, *, actor, from_card, to_card):
    """The journal entries, the sync revision and the events of one link
    made or removed — inside the caller's transaction."""
    _record(
        from_card, kind, actor=actor,
        link=link.kind, direction='out', other_id=to_card.pk, other_code=to_card.code,
    )
    _record(
        to_card, kind, actor=actor,
        link=link.kind, direction='in', other_id=from_card.pk, other_code=from_card.code,
    )
    BoardCard.objects.filter(pk__in=[from_card.pk, to_card.pk]).update(updated_at=timezone.now())
    emit_board_updated(from_card.board_id, BOARD_CHANGE_CARD_UPDATED, from_card.pk)
    if to_card.board_id != from_card.board_id:
        emit_board_updated(to_card.board_id, BOARD_CHANGE_CARD_UPDATED, to_card.pk)


def link_cards(card, *, actor, other_code, kind):
    """Link `card` to the card named `other_code` («СНБ-14»).

    `kind` is one of `LINK_CHOICES`, read from `card`'s side: «Ждёт»
    (`WAITS`: the other card blocks this one), «Блокирует» (`BLOCKS`),
    «Связана с» (`RELATES`, stored with the smaller id as `from_card`, never
    twice) and «Дублирует» (`DUPLICATES`: this card repeats the other). The
    right is `can_link_card()` on `card` and `can_view_board()` on the
    other's board — a card nobody may read and a code nobody answers to are
    one refusal, «Карточка не найдена». Not a card with itself, not the same
    link twice, and not a `BLOCKS` against one going the other way. Subtasks
    may be linked like cards. Returns the link.
    """
    if kind not in {value for value, _label in LINK_CHOICES}:
        raise BoardError('Выберите вид связи.')
    other_id = _linked_card_id(other_code, actor)
    if other_id is None:
        _rejected('link_cards', 'not_found', actor=actor, board_id=card.board_id, card_id=card.pk)
        raise BoardError(LINK_NOT_FOUND)
    with transaction.atomic():
        cards = _lock_link_cards([card.pk, other_id])
        own = cards.get(card.pk)
        other = cards.get(other_id)
        if own is None or other is None:
            raise BoardError(LINK_NOT_FOUND)
        board = own.board
        _refuse_archived('link_cards', board, actor=actor, card_id=own.pk)
        if not can_link_card(actor, own):
            _rejected('link_cards', 'not_permitted', actor=actor, board_id=board.pk, card_id=own.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        if not can_view_board(actor, other.board):
            _rejected('link_cards', 'not_found', actor=actor, board_id=board.pk, card_id=own.pk)
            raise BoardError(LINK_NOT_FOUND)
        if own.pk == other.pk:
            _rejected('link_cards', 'self', actor=actor, board_id=board.pk, card_id=own.pk)
            raise BoardError('Карточку нельзя связать с самой собой.')
        if kind == LINK_WAITS:
            stored, from_card, to_card = BoardCardLink.Kind.BLOCKS, other, own
        elif kind == BoardCardLink.Kind.RELATES:
            stored = kind
            from_card, to_card = sorted((own, other), key=lambda item: item.pk)
        else:
            stored, from_card, to_card = kind, own, other
        if BoardCardLink.objects.filter(from_card=from_card, to_card=to_card, kind=stored).exists():
            _rejected('link_cards', 'duplicate', actor=actor, board_id=board.pk, card_id=own.pk)
            raise BoardError('Такая связь уже есть.')
        if stored == BoardCardLink.Kind.BLOCKS and BoardCardLink.objects.filter(
            from_card=to_card, to_card=from_card, kind=stored,
        ).exists():
            _rejected('link_cards', 'cycle', actor=actor, board_id=board.pk, card_id=own.pk)
            raise BoardError(
                f'{to_card.code} уже блокирует {from_card.code} — две карточки не могут ждать друг друга.'
            )
        try:
            with transaction.atomic():
                link = BoardCardLink.objects.create(
                    from_card=from_card, to_card=to_card, kind=stored, created_by=actor,
                )
        except IntegrityError as exc:
            raise BoardError('Такая связь уже есть.') from exc
        _link_written(link, BoardCardEvent.Kind.LINKED, actor=actor, from_card=from_card, to_card=to_card)
    log_event(
        logger,
        'INFO',
        'board.cards_linked',
        board_id=board.pk,
        board_card_id=own.pk,
        other_board_id=other.board_id,
        other_card_id=other.pk,
        link_id=link.pk,
        kind=stored,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return link


def unlink_cards(link, *, actor):
    """Remove a link, from either of its cards: whoever works on one card's
    board and reads the other's (`can_unlink_cards()`). A link removed
    meanwhile is a refusal, not a second removal."""
    link_id = getattr(link, 'pk', link)
    ends = BoardCardLink.objects.filter(pk=link_id).values_list('from_card_id', 'to_card_id').first()
    if ends is None:
        raise BoardError('Связь уже удалена.')
    with transaction.atomic():
        cards = _lock_link_cards(list(ends))
        link = BoardCardLink.objects.select_for_update().filter(pk=link_id).first()
        if link is None or len(cards) != 2:
            raise BoardError('Связь уже удалена.')
        link.from_card = cards[link.from_card_id]
        link.to_card = cards[link.to_card_id]
        if not can_unlink_cards(actor, link):
            board = link.from_card.board
            _rejected('unlink_cards', 'not_permitted', actor=actor, board_id=board.pk, card_id=link.from_card_id)
            if board.is_archived or link.to_card.board.is_archived:
                raise BoardError(ARCHIVED_MESSAGE)
            raise BoardError('Удалить связь может тот, кто работает с одной из карточек.')
        link.delete()
        link.pk = link_id
        _link_written(
            link, BoardCardEvent.Kind.UNLINKED, actor=actor,
            from_card=link.from_card, to_card=link.to_card,
        )
    log_event(
        logger,
        'INFO',
        'board.cards_unlinked',
        board_id=link.from_card.board_id,
        board_card_id=link.from_card_id,
        other_board_id=link.to_card.board_id,
        other_card_id=link.to_card_id,
        link_id=link_id,
        kind=link.kind,
        actor_user_id=actor.pk,
        outcome='ok',
    )


def _blocked_tasks(card):
    """The open `BOARD` tasks of the cards `card` blocks, cards and boards
    joined — what its closing or reopening concerns."""
    from tasks.models import Task

    return list(
        Task.objects.filter(
            source_type=Task.SourceType.BOARD,
            status__code='IN_PROGRESS',
            board_card__incoming_links__from_card=card,
            board_card__incoming_links__kind=BoardCardLink.Kind.BLOCKS,
        )
        .select_related('board_card__board')
        .distinct()
        .order_by('board_card_id')
    )


def _announce_blocked(card, board, tasks=None):
    """`board.updated(card_updated)` for every *other* board holding a card
    `card` blocks — its tile changed. The card's own board hears the caller's
    own event."""
    if tasks is None:
        tasks = _blocked_tasks(card)
    announced = {board.pk}
    for task in tasks:
        board_id = task.board_card.board_id
        if board_id not in announced:
            announced.add(board_id)
            emit_board_updated(board_id, BOARD_CHANGE_CARD_UPDATED, task.board_card_id)


def _after_blocker_closed(card, board, *, actor, at, cancelled):
    """`card` (its task just completed or cancelled, under the caller's
    locks) blocks nobody any more: every open card it blocked that now waits
    for no open card tells its исполнители «можно начинать»
    (`BOARD_UNBLOCKED`), inside the caller's transaction.

    Who reads `card`'s board reads its code in the text; anybody else reads
    «карточка другой доски». Whoever closed it is not told.
    """
    from notifications.services import notify_board_unblocked

    from .selectors import card_audience, open_blockers_q

    tasks = _blocked_tasks(card)
    for task in tasks:
        blocked = task.board_card
        if BoardCardLink.objects.filter(open_blockers_q(), to_card=blocked).exists():
            continue
        recipients = card_audience(blocked, task, author=False, subscribers=False)
        if not recipients:
            continue
        readers = set(
            get_user_model().objects.filter(
                board_readers_q(board.pk), pk__in=[user.pk for user in recipients],
            ).values_list('pk', flat=True)
        )
        for group, code in (
            ([user for user in recipients if user.pk in readers], card.code),
            ([user for user in recipients if user.pk not in readers], ''),
        ):
            if group:
                notify_board_unblocked(
                    task, actor, group,
                    blocker_id=card.pk, blocker_code=code, cancelled=cancelled, at=at,
                )
    _announce_blocked(card, board, tasks)


# --------------------------------------------------------------------------
# «🔔 Сообщать о новых карточках»: following a column
# --------------------------------------------------------------------------


def toggle_column_subscription(column, *, actor, subscribe=None):
    """Follow a column — a working one or the closing one — or stop.

    Any reader of the board (`can_follow_column()`), never on an archived
    board. `subscribe` is the state asked for, so a double click asks the same
    thing twice; `None` flips. Personal: no `board.updated` and no journal
    entry. Returns whether `actor` follows the column afterwards.
    """
    column_id = getattr(column, 'pk', column)
    board_id = BoardColumn.objects.filter(pk=column_id).values_list('sub_board__board_id', flat=True).first()
    if board_id is None:
        raise BoardError('Колонка не найдена на этой доске — возможно, её удалили.')
    with transaction.atomic():
        board = _lock_board(board_id)
        column = _column_of(board, column_id, operation='toggle_column_subscription', actor=actor)
        _refuse_archived('toggle_column_subscription', board, actor=actor)
        if not can_follow_column(actor, board):
            _rejected('toggle_column_subscription', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Следить за колонкой могут читатели доски.')
        rows = BoardColumnSubscription.objects.filter(column=column, user=actor)
        current = rows.exists()
        wanted = (not current) if subscribe is None else bool(subscribe)
        if wanted == current:
            return current
        if wanted:
            BoardColumnSubscription.objects.create(column=column, user=actor)
        else:
            rows.delete()
    log_event(
        logger,
        'INFO',
        'board.column_subscription_changed',
        board_id=board.pk,
        column_id=column.pk,
        actor_user_id=actor.pk,
        subscribed=wanted,
        outcome='ok',
    )
    return wanted


def _notify_column_entered(card, task, column, board, *, actor, told=()):
    """«Карточка ZAP-12 вошла в колонку …» for `column`'s followers who still
    read the board, inside the caller's transaction.

    `told` are the people the same action already tells (the исполнители of
    a new card, those a move added, a completion's audience): one action, one
    notification per person. Whoever acted is left out by the notification
    itself. A subtask enters no column.
    """
    from notifications.services import notify_board_column_entered

    if card.parent_id is not None:
        return
    recipients = list(
        get_user_model().objects.filter(
            board_readers_q(board.pk),
            pk__in=BoardColumnSubscription.objects.filter(column=column).values('user_id'),
        )
        .exclude(pk__in=[getattr(user, 'pk', user) for user in told])
        .distinct()
        .order_by('pk')
    )
    if recipients:
        notify_board_column_entered(task, actor, recipients, column=column, at=timezone.now())


# --------------------------------------------------------------------------
# «Дайджест на почту»
# --------------------------------------------------------------------------


def set_digest_subscription(board, *, actor, frequency):
    """«Дайджест на почту: ежедневно / еженедельно / выключен» of a board.

    `frequency` is `DAILY`, `WEEKLY`, or empty (`''`/`None`) to stop. Any
    reader of a live board (`can_subscribe_digest()`), under the board lock;
    personal — no event, no journal entry. Changing the frequency keeps the
    day it was last handled, so switching does not send twice in one day.
    Returns the frequency afterwards (`''` when off). The letters themselves
    are `boards.digest.send_digests()`, run by `manage.py board_digest`.
    """
    frequency = (frequency or '').strip().upper()
    if frequency and frequency not in BoardDigestSubscription.Frequency.values:
        raise BoardError('Выберите, как часто присылать дайджест.')
    with transaction.atomic():
        board = _lock_board(board.pk)
        _refuse_archived('set_digest_subscription', board, actor=actor)
        if not can_subscribe_digest(actor, board):
            _rejected('set_digest_subscription', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Дайджест доски получают её читатели.')
        current = BoardDigestSubscription.objects.filter(board=board, user=actor).first()
        if not frequency:
            if current is None:
                return ''
            current.delete()
        elif current is None:
            BoardDigestSubscription.objects.create(board=board, user=actor, frequency=frequency)
        elif current.frequency == frequency:
            return frequency
        else:
            current.frequency = frequency
            current.save(update_fields=['frequency'])
    log_event(
        logger,
        'INFO',
        'board.digest_subscription_changed',
        board_id=board.pk,
        actor_user_id=actor.pk,
        frequency=frequency or 'OFF',
        outcome='ok',
    )
    return frequency



# --------------------------------------------------------------------------
# «Приём заявок»: the board's intake, its requests and «Входящие»
# --------------------------------------------------------------------------

REQUEST_ALREADY_DECIDED = 'Заявку уже разобрали.'
REQUEST_WITHDRAWN = 'Автор отозвал заявку.'


def _tell_requests_done(card, board, *, actor, at):
    """`BOARD_REQUEST_DONE` to the author of the request the card was made
    of — inside `complete_card()`'s transaction. A card made of no request
    (nearly every one) costs one query and tells nobody."""
    from notifications.services import notify_board_request_done

    for board_request in BoardRequest.objects.filter(card=card, status=BoardRequest.Status.ACCEPTED).select_related(
        'author',
    ):
        board_request.board = board
        board_request.card = card
        notify_board_request_done(board_request, actor, completed_at=at)


def intake_target(board):
    """`(sub_board, column)` where an accepted request stands by default:
    the board's `intake_column` while it is a working column of this board,
    else the first working column of the first sub-board. Two queries at
    most; `(None, None)` for a board with no working column at all."""
    if board.intake_column_id:
        column = (
            BoardColumn.objects.select_related('sub_board')
            .filter(pk=board.intake_column_id, sub_board__board=board, is_done=False).first()
        )
        if column is not None:
            return column.sub_board, column
    column = (
        BoardColumn.objects.select_related('sub_board')
        .filter(sub_board__board=board, is_done=False)
        .order_by('sub_board__position', 'sub_board_id', 'position', 'pk').first()
    )
    return (column.sub_board, column) if column is not None else (None, None)


def intake_recipients(board):
    """Who hears of a new request: the board's handlers who are still active
    members, else — nobody named, or nobody left — its owner while an active
    employee. One query each."""
    handlers = list(
        get_user_model().objects.filter(
            active_employee_q(),
            board_intake_roles__board=board,
            board_memberships__board=board,
        ).distinct().order_by('pk')
    )
    if handlers:
        return handlers
    return list(get_user_model().objects.filter(active_employee_q(), pk=board.owner_id))


def _clean_intake_column(board, column, *, actor):
    if column in (None, ''):
        return None
    return _board_working_column(board, column, operation='update_intake', actor=actor)


def _clean_intake_due_days(days):
    if days in (None, ''):
        return None
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise BoardError('Срок по заявке — число рабочих дней или пусто.') from None
    if not 0 <= days <= INTAKE_DUE_DAYS_MAX:
        raise BoardError(f'Срок по заявке — от 0 до {INTAKE_DUE_DAYS_MAX} рабочих дней или пусто.')
    return days


def _clean_intake_handlers(board, handler_ids):
    ids = set()
    for value in handler_ids or ():
        try:
            ids.add(int(getattr(value, 'pk', value)))
        except (TypeError, ValueError):
            raise BoardError('Разбирающими могут быть только активные участники доски.') from None
    members = set(
        BoardMember.objects.filter(active_employee_q('user__'), board=board, user_id__in=ids)
        .values_list('user_id', flat=True)
    )
    if ids - members:
        raise BoardError('Разбирающими могут быть только активные участники доски.')
    return ids


def update_intake(board, *, actor, enabled, column=None, due_days=None, hint='',
                  handler_ids=(), form_fields=None):
    """«Приём заявок» of a board — the manager's (`can_manage_board()`), under
    the board lock, never on an archived board.

    `enabled`; `column` — a working column of any sub-board of the board
    (`intake_sub_board` follows it), empty for the default (`intake_target()`);
    `due_days` — 0 to `INTAKE_DUE_DAYS_MAX` working days for the срок of a
    request that asks no date, empty for none; `hint` — at most
    `INTAKE_HINT_MAX_LENGTH`; `handler_ids` — active members told of new
    requests (none: the owner); `form_fields` — `{field id: (in the form,
    required)}` for the board's live fields, a field left out off, `None` to
    leave the fields alone. A required field is in the form.

    The same settings again store and publish nothing (`False`). A change
    touches the sub-boards' `updated_at` (the `boards` sync revision: the
    tabs draw «Входящие») and publishes one `board.updated(structure_changed)`
    (`True`).
    """
    with transaction.atomic():
        board = _lock_board(board.pk)
        _refuse_archived('update_intake', board, actor=actor)
        if not can_manage_board(actor, board):
            _rejected('update_intake', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Приём заявок настраивает владелец доски или администратор.')
        column = _clean_intake_column(board, column, actor=actor)
        due_days = _clean_intake_due_days(due_days)
        hint = (hint or '').strip()
        if len(hint) > INTAKE_HINT_MAX_LENGTH:
            raise BoardError(f'Подсказка — не длиннее {INTAKE_HINT_MAX_LENGTH} символов.')
        handler_ids = _clean_intake_handlers(board, handler_ids)
        fields = list(BoardField.objects.filter(board=board, is_archived=False))
        wanted = {}
        if form_fields is not None:
            requested = {}
            for key, value in form_fields.items():
                try:
                    requested[int(getattr(key, 'pk', key))] = value
                except (TypeError, ValueError):
                    continue
            known = {field.pk for field in fields}
            if set(requested) - known:
                raise BoardError('Поле карточки не найдено на этой доске — возможно, его удалили. Обновите страницу.')
            for field in fields:
                in_form, required = requested.get(field.pk, (False, False))
                wanted[field.pk] = (bool(in_form) or bool(required), bool(required))
        settings_now = (
            board.intake_enabled, board.intake_column_id, board.intake_due_days, board.intake_hint,
        )
        settings_new = (bool(enabled), getattr(column, 'pk', None), due_days, hint)
        current_handlers = set(
            BoardIntakeHandler.objects.filter(board=board).values_list('user_id', flat=True)
        )
        changed_fields = [
            field for field in fields
            if field.pk in wanted and (field.in_request_form, field.required_in_request) != wanted[field.pk]
        ]
        if settings_now == settings_new and current_handlers == handler_ids and not changed_fields:
            return False
        board.intake_enabled = bool(enabled)
        board.intake_column = column
        board.intake_sub_board = column.sub_board if column is not None else None
        board.intake_due_days = due_days
        board.intake_hint = hint
        board.save(update_fields=[
            'intake_enabled', 'intake_column', 'intake_sub_board', 'intake_due_days', 'intake_hint',
            'updated_at',
        ])
        if current_handlers != handler_ids:
            BoardIntakeHandler.objects.filter(board=board).exclude(user_id__in=handler_ids).delete()
            BoardIntakeHandler.objects.bulk_create(
                [BoardIntakeHandler(board=board, user_id=user_id) for user_id in sorted(handler_ids - current_handlers)]
            )
        now = timezone.now()
        for field in changed_fields:
            field.in_request_form, field.required_in_request = wanted[field.pk]
            field.updated_at = now
        if changed_fields:
            BoardField.objects.bulk_update(
                changed_fields, ['in_request_form', 'required_in_request', 'updated_at'],
            )
        SubBoard.objects.filter(board=board).update(updated_at=now)
        _structure_changed(
            board, 'board.intake_changed', actor=actor,
            intake_enabled=board.intake_enabled, handler_count=len(handler_ids),
            form_field_count=sum(1 for value in wanted.values() if value[0]),
        )
    return True


def request_form_fields(board):
    """The live fields of `board` asked in its request form, in order, with
    their options — one query and one prefetch."""
    return list(
        BoardField.objects.filter(board=board, is_archived=False, in_request_form=True)
        .prefetch_related('options').order_by('position', 'pk')
    )


def _stored_request_value(field, value):
    """What `BoardRequest.field_values` keeps of a parsed value: JSON, and
    exactly what a card's form would send back."""
    if field.kind == BoardField.Kind.SELECT:
        return value.pk
    if field.kind == BoardField.Kind.DATE:
        return value.isoformat()
    if field.kind == BoardField.Kind.NUMBER:
        return format(value.normalize(), 'f') if value != 0 else '0'
    return value


def _clean_request_values(board, field_values):
    """`{str(field id): value}` for the fields of the request form — every
    value through the card's own parse (`_parse_value()`), a required one
    refused empty with `FieldValueError` (its field named). A key that is not
    a field of the form is ignored: nothing else is asked."""
    field_values = field_values or {}
    raw_by_id = {}
    for key, raw in field_values.items():
        try:
            raw_by_id[int(getattr(key, 'pk', key))] = raw
        except (TypeError, ValueError):
            continue
    stored = {}
    for field in request_form_fields(board):
        value = _parse_value(field, raw_by_id.get(field.pk), None)
        if value is None:
            if field.required_in_request:
                raise FieldValueError(field, 'обязательное поле заявки.')
            continue
        stored[str(field.pk)] = _stored_request_value(field, value)
    return stored


def submit_request(board, *, author, title, description='', desired_date=None, field_values=None):
    """«Подать заявку»: any active employee, a member of the board or not, to
    a live board with «Приём заявок» on (`can_submit_request()`), asked under
    the board lock.

    The title is required (at most `REQUEST_TITLE_MAX_LENGTH`), the
    description optional (at most `REQUEST_DESCRIPTION_MAX_LENGTH`), the
    desired date optional; the board's fields are those of its form, each
    parsed as a card's value is and a required one refused empty. The
    request is `NEW`; the board's handlers hear of it (`BOARD_REQUEST_NEW`)
    and the board publishes `board.updated(request_changed)` — its
    «Входящие (N)» moves.
    """
    from notifications.services import notify_board_request_new

    with transaction.atomic():
        board = _lock_board(board.pk)
        if board.is_archived:
            _rejected('submit_request', 'board_archived', actor=author, board_id=board.pk)
            raise BoardError('Доска в архиве — заявки на неё не принимаются.')
        if not board.intake_enabled:
            _rejected('submit_request', 'intake_off', actor=author, board_id=board.pk)
            raise BoardError('Эта доска сейчас не принимает заявки.')
        if not can_submit_request(author, board):
            _rejected('submit_request', 'not_permitted', actor=author, board_id=board.pk)
            raise BoardError('Подать заявку может активный сотрудник.')
        title = (title or '').strip()
        if not title:
            raise BoardError('Укажите, что нужно сделать, — название заявки.')
        if len(title) > REQUEST_TITLE_MAX_LENGTH:
            raise BoardError(f'Название заявки — не длиннее {REQUEST_TITLE_MAX_LENGTH} символов.')
        description = (description or '').strip()
        if len(description) > REQUEST_DESCRIPTION_MAX_LENGTH:
            raise BoardError(f'Описание заявки — не длиннее {REQUEST_DESCRIPTION_MAX_LENGTH} символов.')
        values = _clean_request_values(board, field_values)
        board_request = BoardRequest.objects.create(
            board=board, author=author, title=title, description=description,
            desired_date=desired_date, field_values=values,
        )
        notify_board_request_new(board_request, author, intake_recipients(board))
        emit_board_updated(board.pk, BOARD_CHANGE_REQUEST_CHANGED)
    log_event(
        logger, 'INFO', 'board.request_submitted',
        board_id=board.pk, request_id=board_request.pk, actor_user_id=author.pk,
        field_count=len(values), outcome='ok',
    )
    return board_request


def _lock_request(board_request, board):
    try:
        return BoardRequest.objects.select_for_update().get(pk=board_request.pk, board=board)
    except BoardRequest.DoesNotExist as exc:
        raise BoardError('Заявка не найдена на этой доске.') from exc


def _refuse_decided(board_request, operation, *, actor):
    if board_request.status == BoardRequest.Status.NEW:
        return
    _rejected(operation, 'already_decided', actor=actor, board_id=board_request.board_id)
    raise BoardError(
        REQUEST_WITHDRAWN if board_request.status == BoardRequest.Status.WITHDRAWN
        else REQUEST_ALREADY_DECIDED
    )


def withdraw_request(board_request, *, actor):
    """«Отозвать»: the author, while the request is `NEW` — on any board,
    an archived one too (it waits nowhere). `WITHDRAWN`, with who and when;
    nobody is told, and the board's «Входящие» moves."""
    with transaction.atomic():
        board = _lock_board(board_request.board_id)
        board_request = _lock_request(board_request, board)
        if board_request.author_id != getattr(actor, 'pk', None):
            _rejected('withdraw_request', 'not_author', actor=actor, board_id=board.pk)
            raise BoardError('Отозвать заявку может только её автор.')
        _refuse_decided(board_request, 'withdraw_request', actor=actor)
        board_request.status = BoardRequest.Status.WITHDRAWN
        board_request.decided_by = actor
        board_request.decided_at = timezone.now()
        board_request.save(update_fields=['status', 'decided_by', 'decided_at', 'updated_at'])
        emit_board_updated(board.pk, BOARD_CHANGE_REQUEST_CHANGED)
    log_event(
        logger, 'INFO', 'board.request_withdrawn',
        board_id=board.pk, request_id=board_request.pk, actor_user_id=actor.pk, outcome='ok',
    )
    return board_request


def _decidable(board_request, operation, *, actor):
    """The board and the request, locked in that order, if `actor` may decide
    it now: not on an archived board, `can_decide_request()`, a `NEW`
    request — else a `BoardError` («Заявку уже разобрали»)."""
    board = _lock_board(board_request.board_id)
    _refuse_archived(operation, board, actor=actor)
    if not can_decide_request(actor, board):
        _rejected(operation, 'not_permitted', actor=actor, board_id=board.pk)
        raise BoardError('Разбирают заявки участники доски.')
    board_request = _lock_request(board_request, board)
    _refuse_decided(board_request, operation, actor=actor)
    board_request.board = board
    return board, board_request


def _decided(board_request, status, *, actor, comment='', card=None, duplicate_of=None):
    board_request.status = status
    board_request.card = card
    board_request.duplicate_of = duplicate_of
    board_request.decision_comment = comment
    board_request.decided_by = actor
    board_request.decided_at = timezone.now()
    board_request.full_clean(exclude=['field_values'])
    board_request.save(update_fields=[
        'status', 'card', 'duplicate_of', 'decision_comment', 'decided_by', 'decided_at', 'updated_at',
    ])


def request_default_due(board, board_request, today=None):
    """The срок «Принять» offers: the date the author asked for, else
    `intake_due_days` working days from today, else none."""
    from ecosystem.workdays import add_working_days

    if board_request.desired_date is not None:
        return board_request.desired_date
    if board.intake_due_days is not None:
        return add_working_days(today or timezone.localdate(), board.intake_due_days)
    return None


def _request_values_for_card(board, stored):
    """The request's field values a new card may still take: fields still
    live on this board, and a list option not archived since — what no
    longer applies is left out rather than refusing the card."""
    if not stored:
        return {}
    fields = {
        field.pk: field
        for field in BoardField.objects.filter(board=board, is_archived=False).prefetch_related('options')
    }
    values = {}
    for key, value in stored.items():
        try:
            field = fields.get(int(key))
        except (TypeError, ValueError):
            continue
        if field is None:
            continue
        if field.kind == BoardField.Kind.SELECT:
            option = next((option for option in field.options.all() if option.pk == value), None)
            if option is None or option.is_archived:
                continue
        values[field.pk] = value
    return values


def first_request_message(board_request):
    """The first message of a card made of a request, in the accepting
    member's name: what the author asked, word for word — the card's own
    description may be corrected when it is accepted."""
    from accounts.templatetags.people import person_name

    return f'Заявка от {person_name(board_request.author)}: {board_request.description or board_request.title}'


def accept_request(board_request, *, actor, assignee_ids, column=None, due_date=None,
                   title=None, description=None):
    """«Принять»: a card of the request, on this board.

    `column` — a working column of any sub-board of the board, `None` for
    `intake_target()`; `due_date` — `None` for `request_default_due()`;
    `title`/`description` — `None` for the request's own. The card is
    `create_card()`'s (`_place_new_card()`: its pins, `BOARD_TASK_ASSIGNED`,
    the column's followers) with the request's field values that still apply
    (`_request_values_for_card()`), and its `CREATED` entry names the request
    (`request_id`). Its «Чат» opens with the request's text in the accepting
    member's name (`first_request_message()`) — no notification for it: the
    card's people were just told of the card. The request is `ACCEPTED` with
    its card, its author hears it (`BOARD_REQUEST_ACCEPTED`), and the board
    publishes one `board.updated(request_changed)` naming the new card.
    """
    from notifications.services import notify_board_request_decided

    with transaction.atomic():
        board, board_request = _decidable(board_request, 'accept_request', actor=actor)
        if column in (None, ''):
            sub_board, column = intake_target(board)
            if column is None:
                raise BoardError('На доске нет рабочей колонки для заявки.')
        else:
            column = _board_working_column(board, column, operation='accept_request', actor=actor)
            sub_board = column.sub_board
        if due_date is None:
            due_date = request_default_due(board, board_request)
        card, task, column, ids = _place_new_card(
            board, sub_board, actor=actor,
            title=board_request.title if title is None else title,
            description=board_request.description if description is None else description,
            due_date=due_date, assignee_ids=assignee_ids, column=column,
            field_values=_request_values_for_card(board, board_request.field_values),
            request_id=board_request.pk, operation='accept_request',
        )
        BoardCardComment.objects.create(card=card, author=actor, text=first_request_message(board_request))
        _decided(board_request, BoardRequest.Status.ACCEPTED, actor=actor, card=card)
        notify_board_request_decided(board_request, actor)
        emit_board_updated(board.pk, BOARD_CHANGE_REQUEST_CHANGED, card.pk)
    log_event(
        logger, 'INFO', 'board.request_accepted',
        board_id=board.pk, request_id=board_request.pk, board_card_id=card.pk,
        task_id=task.pk, column_id=column.pk, actor_user_id=actor.pk, assignee_count=len(ids),
        outcome='ok',
    )
    return card


def reject_request(board_request, *, actor, reason):
    """«Отклонить»: a reason is required (at most `COMMENT_MAX_LENGTH`) and
    kept on the request for its author; `REJECTED`, the author told
    (`BOARD_REQUEST_REJECTED`, the reason not in the text)."""
    from notifications.services import notify_board_request_decided

    reason = (reason or '').strip()
    with transaction.atomic():
        board, board_request = _decidable(board_request, 'reject_request', actor=actor)
        if not reason:
            raise BoardError('Укажите причину отказа — её прочитает автор заявки.')
        if len(reason) > COMMENT_MAX_LENGTH:
            raise BoardError(f'Причина — не длиннее {COMMENT_MAX_LENGTH} символов.')
        _decided(board_request, BoardRequest.Status.REJECTED, actor=actor, comment=reason)
        notify_board_request_decided(board_request, actor)
        emit_board_updated(board.pk, BOARD_CHANGE_REQUEST_CHANGED)
    log_event(
        logger, 'INFO', 'board.request_rejected',
        board_id=board.pk, request_id=board_request.pk, actor_user_id=actor.pk, outcome='ok',
    )
    return board_request


def mark_duplicate(board_request, *, actor, card_code, comment=''):
    """«Дубль»: the request repeats a card of **this** board, named by its
    code («ZAP-7», any case — `tasks.selectors.board_card_code_filter()`).
    A code of another board, or of nobody, is refused. `DUPLICATE` with that
    card, the author told (`BOARD_REQUEST_DUPLICATE`)."""
    from notifications.services import notify_board_request_decided
    from tasks.models import Task
    from tasks.selectors import board_card_code_filter

    comment = (comment or '').strip()
    with transaction.atomic():
        board, board_request = _decidable(board_request, 'mark_duplicate', actor=actor)
        condition = board_card_code_filter((card_code or '').strip())
        task = (
            Task.objects.filter(condition, board_card__board=board).select_related('board_card').first()
            if condition is not None else None
        )
        if task is None:
            _rejected('mark_duplicate', 'card_not_found', actor=actor, board_id=board.pk)
            raise BoardError('Карточка с таким кодом не найдена на этой доске.')
        if len(comment) > COMMENT_MAX_LENGTH:
            raise BoardError(f'Комментарий — не длиннее {COMMENT_MAX_LENGTH} символов.')
        duplicate_of = task.board_card
        duplicate_of.board = board
        _decided(
            board_request, BoardRequest.Status.DUPLICATE, actor=actor, comment=comment,
            duplicate_of=duplicate_of,
        )
        notify_board_request_decided(board_request, actor)
        emit_board_updated(board.pk, BOARD_CHANGE_REQUEST_CHANGED, duplicate_of.pk)
    log_event(
        logger, 'INFO', 'board.request_duplicate',
        board_id=board.pk, request_id=board_request.pk, board_card_id=duplicate_of.pk,
        actor_user_id=actor.pk, outcome='ok',
    )
    return board_request


# --------------------------------------------------------------------------
# «📌 Закрепить» and «Списком»
# --------------------------------------------------------------------------


def set_card_pinned(card, *, actor, pinned):
    """«📌 Закрепить» / «Открепить»: the card first in its working column.

    Whoever works on the board (`can_work_on_board()`), an open card that is
    no subtask, never on an archived board. A pinned card goes to the end of
    the pinned ones; an unpinned one to the start of the others — where it
    stood, just below them. `pinned` is the state asked for (the button
    posts it): the same state stores and publishes nothing. A placement of
    the tile, like a reorder: no journal entry and no version step, one
    `board.updated(card_updated)`.
    """
    pinned = bool(pinned)
    with transaction.atomic():
        board, card, task = _movable_card(card, 'pin_card', actor=actor)
        _refuse_closed_task(task, 'pin_card', actor=actor, board=board, card=card)
        if card.is_pinned == pinned:
            return card
        column = _working_column(
            card.sub_board, card.column_id or _first_working_id(card.sub_board_id),
            operation='pin_card', actor=actor, card_id=card.pk,
        )
        cards = _column(column, exclude_card_id=card.pk)
        group = [other for other in cards if other.is_pinned == pinned]
        card.is_pinned = pinned
        if pinned:
            card.position = _end_position(column)
            card.save(update_fields=['is_pinned', 'position', 'updated_at'])
        elif group and group[0].position >= 2:
            card.position = group[0].position // 2
            card.save(update_fields=['is_pinned', 'position', 'updated_at'])
        else:
            card.save(update_fields=['is_pinned', 'updated_at'])
            _renumber([card] + group)
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_UPDATED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.card_pinned' if pinned else 'board.card_unpinned',
        board_id=board.pk,
        board_card_id=card.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return card


MAX_LIST_CARDS = 30


def create_cards_from_list(sub_board, *, actor, text, due_date, column=None):
    """«Списком» in «+ Карточка»: one card per non-empty line of `text`.

    Each is `create_card()`'s own body (`_place_new_card()`): at the end of
    `column` (`None` — the first working one), in the order of the lines,
    with whoever pressed as its исполнитель and `due_date` as its срок (a
    card's task always has one), the column's «Правила при входе» and its
    followers told. All or nothing, in one transaction: no line, a line over
    `TITLE_MAX_LENGTH` or more than `MAX_LIST_CARDS` lines refuse the whole
    list before anything is written. One `board.updated(card_created)` for
    the whole list. Returns the cards.
    """
    lines = [line.strip() for line in (text or '').splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise BoardError('Напишите хотя бы одну строку — одна строка, одна карточка.')
    if len(lines) > MAX_LIST_CARDS:
        raise BoardError(f'Списком — не больше {MAX_LIST_CARDS} карточек за раз, а строк {len(lines)}.')
    for index, line in enumerate(lines, start=1):
        if len(line) > TITLE_MAX_LENGTH:
            raise BoardError(f'Строка {index} длиннее {TITLE_MAX_LENGTH} символов — сократите её.')
    with transaction.atomic():
        board = _lock_board(sub_board.board_id)
        _refuse_archived('create_cards_from_list', board, actor=actor)
        if not can_work_on_board(actor, board):
            _rejected('create_cards_from_list', 'not_permitted', actor=actor, board_id=board.pk)
            raise BoardError('Работа с карточками этой доски недоступна.')
        cards = []
        for line in lines:
            card, _task, column, _ids = _place_new_card(
                board, sub_board, actor=actor, title=line, due_date=due_date,
                assignee_ids=[actor.pk], description='', column=column, field_values=None,
                operation='create_cards_from_list',
            )
            column = column.pk
            cards.append(card)
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_CREATED, cards[0].pk)
    log_event(
        logger,
        'INFO',
        'board.cards_created',
        board_id=board.pk,
        sub_board_id=cards[0].sub_board_id,
        column_id=column,
        card_count=len(cards),
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return cards


# --------------------------------------------------------------------------
# «Правила при входе» of a working column
# --------------------------------------------------------------------------
#
# The manager's, like the pins (`set_column_pins()`) they stand beside: one
# board lock, never an archived board, never the closing column. Each change
# saves the column's `updated_at` (the structure aggregate of the sync
# revision) and publishes one `board.updated(structure_changed)`; the same
# settings again store and publish nothing. What they do to a card is
# `_apply_column_entry()`.


def _rules_column(column, operation, *, actor):
    """The board (locked) and `column`, if `actor` may set its entry rules."""
    column_id = getattr(column, 'pk', column)
    board_id = BoardColumn.objects.filter(pk=column_id).values_list('sub_board__board_id', flat=True).first()
    if board_id is None:
        raise BoardError('Колонка не найдена — возможно, её удалили.')
    board = _manageable_board(board_id, operation, actor=actor)
    column = _column_of(board, column_id, operation=operation, actor=actor)
    if column.is_done:
        _rejected(operation, 'done_column', actor=actor, board_id=board.pk)
        raise BoardError(
            'В завершающую колонку карточка попадает выполненной — правил при входе у неё нет.'
        )
    return board, column


def _rules_changed(board, column, event, *, actor, **ids):
    column.save(update_fields=['updated_at'])
    _structure_changed(board, event, actor=actor, column_id=column.pk, **ids)


def _template_rows(column):
    return list(BoardColumnChecklistTemplate.objects.filter(column=column).order_by('position', 'pk'))


def add_template_item(column, *, actor, text):
    """A line at the end of the column's «Шаблон чек-листа».

    Trimmed, required, at most `CHECKLIST_TEXT_MAX_LENGTH`; at most
    `MAX_TEMPLATE_ITEMS` lines; a line the template already holds (whatever
    the case) is refused — a card would get it once anyway.
    """
    with transaction.atomic():
        board, column = _rules_column(column, 'add_template_item', actor=actor)
        text = _clean_checklist_text(text)
        rows = _template_rows(column)
        if len(rows) >= MAX_TEMPLATE_ITEMS:
            _rejected('add_template_item', 'limit', actor=actor, board_id=board.pk)
            raise BoardError(f'В шаблоне может быть не больше {MAX_TEMPLATE_ITEMS} пунктов.')
        if text.casefold() in {row.text.casefold() for row in rows}:
            raise BoardError(f'Пункт «{text}» в шаблоне уже есть.')
        item = BoardColumnChecklistTemplate.objects.create(
            column=column, text=text, position=len(rows) + 1,
        )
        _rules_changed(board, column, 'board.column_template_changed', actor=actor, template_item_id=item.pk)
    return item


def _template_item(item, operation, *, actor):
    item_id = getattr(item, 'pk', item)
    column_id = BoardColumnChecklistTemplate.objects.filter(pk=item_id).values_list('column_id', flat=True).first()
    if column_id is None:
        raise BoardError('Пункт шаблона не найден — возможно, его удалили. Обновите страницу.')
    board, column = _rules_column(column_id, operation, actor=actor)
    found = BoardColumnChecklistTemplate.objects.filter(pk=item_id, column=column).first()
    if found is None:
        raise BoardError('Пункт шаблона не найден — возможно, его удалили. Обновите страницу.')
    return board, column, found


def move_template_item(item, *, actor, direction):
    """A template line one place up (`'left'`) or down (`'right'`)."""
    with transaction.atomic():
        board, column, item = _template_item(item, 'move_template_item', actor=actor)
        rows = _step(_template_rows(column), item, direction)
        if rows is None:
            return item
        for index, row in enumerate(rows, start=1):
            row.position = index
        BoardColumnChecklistTemplate.objects.bulk_update(rows, ['position'])
        _rules_changed(board, column, 'board.column_template_changed', actor=actor, template_item_id=item.pk)
    return item


def delete_template_item(item, *, actor):
    """«×» beside a template line. The cards that got it keep their item."""
    with transaction.atomic():
        board, column, item = _template_item(item, 'delete_template_item', actor=actor)
        item_id = item.pk
        item.delete()
        rows = _template_rows(column)
        for index, row in enumerate(rows, start=1):
            row.position = index
        BoardColumnChecklistTemplate.objects.bulk_update(rows, ['position'])
        _rules_changed(board, column, 'board.column_template_changed', actor=actor, template_item_id=item_id)


def _rule_field(board, field, operation, *, actor):
    """`field` (an object or an id) as a live field of `board`, its options read."""
    field_id = getattr(field, 'pk', field)
    try:
        field_id = int(field_id)
    except (TypeError, ValueError):
        field_id = None
    found = (
        BoardField.objects.filter(pk=field_id, board=board).prefetch_related('options').first()
        if field_id is not None else None
    )
    if found is None or found.is_archived:
        _rejected(operation, 'unknown_field', actor=actor, board_id=board.pk)
        raise BoardError('Поле не найдено на этой доске или убрано в архив.')
    return found


def _clean_rule_value(field, raw):
    """The raw value a rule stores — the canonical form of what a card form
    would send — or `''` for an empty one. A value that does not parse is a
    `FieldValueError` naming the field."""
    value = _parse_value(field, raw, None)
    if value is None:
        return ''
    return str(_stored_request_value(field, value))


def set_column_field_rules(column, *, actor, values):
    """«Значения полей»: the values fields get when a card enters `column`.

    `values` is `{field (an object or an id): (raw value, overwrite)}` for
    the fields the form shows — each a live field of this board, its value
    parsed as a card's would be (a refusal is a `FieldValueError` naming the
    field, before anything is written); an empty value removes that field's
    rule, a field left out keeps its rule. `overwrite` — replace a value the
    card already holds. Returns whether anything changed; the same values
    and flags store and publish nothing.
    """
    with transaction.atomic():
        board, column = _rules_column(column, 'set_column_field_rules', actor=actor)
        wanted = {}
        for key, (raw, overwrite) in (values or {}).items():
            field = _rule_field(board, key, 'set_column_field_rules', actor=actor)
            wanted[field] = (_clean_rule_value(field, raw), bool(overwrite))
        current = {
            rule.field_id: rule
            for rule in BoardColumnFieldRule.objects.filter(column=column, field__in=list(wanted))
        }
        changed = False
        for field, (raw, overwrite) in wanted.items():
            rule = current.get(field.pk)
            if not raw:
                if rule is not None:
                    rule.delete()
                    changed = True
                continue
            if rule is not None and rule.value == raw and rule.overwrite == overwrite:
                continue
            if rule is None:
                rule = BoardColumnFieldRule(column=column, field=field)
            rule.value, rule.overwrite = raw, overwrite
            rule.save()
            changed = True
        if changed:
            _rules_changed(board, column, 'board.column_field_rules_changed', actor=actor)
    return changed


def set_column_followers(column, *, actor, user_ids):
    """«Подписчики»: who follows every card that enters `column`.

    Only active readers of the board (`board_readers_q()`); anybody else
    sent by hand is refused. An empty list removes everybody. The cards
    already standing in the column are not touched.
    """
    with transaction.atomic():
        board, column = _rules_column(column, 'set_column_followers', actor=actor)
        requested = {int(getattr(value, 'pk', value)) for value in user_ids or ()}
        readers = set(
            get_user_model().objects.filter(board_readers_q(board), pk__in=requested)
            .values_list('pk', flat=True)
        )
        if requested - readers:
            _rejected('set_column_followers', 'not_a_reader', actor=actor, board_id=board.pk)
            raise BoardError('Подписать на карточки колонки можно только активных читателей доски.')
        current = set(BoardColumnFollower.objects.filter(column=column).values_list('user_id', flat=True))
        if current == requested:
            return False
        BoardColumnFollower.objects.filter(column=column, user_id__in=current - requested).delete()
        BoardColumnFollower.objects.bulk_create(
            [BoardColumnFollower(column=column, user_id=user_id) for user_id in sorted(requested - current)]
        )
        _rules_changed(
            board, column, 'board.column_followers_changed', actor=actor, follower_count=len(requested),
        )
    return True


# --------------------------------------------------------------------------
# «Передать дальше»: a board's actions
# --------------------------------------------------------------------------
#
# Set up on «Действия» by whoever manages the board: one board lock, never
# an archived board, one `board.updated(structure_changed)` per change (the
# buttons are in every card's panel, so the sub-boards' `updated_at` moves
# too — the sync revision's structure aggregate), the same settings again
# nothing. Pressed by whoever works on the board: `run_board_action()`.


def _actions_changed(board, event, *, actor, **ids):
    SubBoard.objects.filter(board=board).update(updated_at=timezone.now())
    _structure_changed(board, event, actor=actor, **ids)


def _action_of(board, action, *, operation, actor):
    action_id = getattr(action, 'pk', action)
    try:
        action_id = int(action_id)
    except (TypeError, ValueError):
        action_id = None
    found = BoardAction.objects.filter(pk=action_id, board=board).first() if action_id is not None else None
    if found is None:
        _rejected(operation, 'unknown_action', actor=actor, board_id=board.pk)
        raise BoardError('Действие не найдено на этой доске — возможно, его изменили. Обновите страницу.')
    return found


def _live_action_count(board, *, exclude_pk=None):
    actions = BoardAction.objects.filter(board=board, is_archived=False)
    if exclude_pk is not None:
        actions = actions.exclude(pk=exclude_pk)
    return actions.count()


def _clean_action(board, *, name, target_column, assignee_mode, assignee_ids, field_values,
                  comment_required, message_template, operation, actor, exclude_pk=None):
    """Everything an action holds, checked under the board lock: a dict of
    its columns plus `assignee_ids` (sorted) and `rules` (`{field: (raw,
    overwrite)}`)."""
    from .models import ACTION_MESSAGE_MAX_LENGTH, ACTION_NAME_MAX_LENGTH

    name = _clean_name(name, max_length=ACTION_NAME_MAX_LENGTH, what='действия')
    others = BoardAction.objects.filter(board=board, is_archived=False)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    if name.casefold() in {other.casefold() for other in others.values_list('name', flat=True)}:
        raise BoardError(f'Действие «{name}» на этой доске уже есть.')
    column = _board_working_column(board, target_column, operation=operation, actor=actor)
    if assignee_mode not in BoardAction.AssigneeMode.values:
        raise BoardError('Неизвестный режим исполнителей.')
    ids = []
    if assignee_mode != BoardAction.AssigneeMode.KEEP:
        ids = _clean_assignees(board, assignee_ids)
    rules = {}
    for key, (raw, overwrite) in (field_values or {}).items():
        field = _rule_field(board, key, operation, actor=actor)
        value = _clean_rule_value(field, raw)
        if value:
            rules[field] = (value, bool(overwrite))
    message_template = (message_template or '').strip()
    if len(message_template) > ACTION_MESSAGE_MAX_LENGTH:
        raise BoardError(f'Сообщение в чат — не длиннее {ACTION_MESSAGE_MAX_LENGTH} символов.')
    return {
        'name': name,
        'target_column': column,
        'assignee_mode': assignee_mode,
        'comment_required': bool(comment_required),
        'message_template': message_template,
        'assignee_ids': ids,
        'rules': rules,
    }


def _action_state(action):
    """What an action holds, comparable: its columns, people and rules."""
    return (
        action.name, action.target_column_id, action.assignee_mode, action.comment_required,
        action.message_template,
        tuple(sorted(BoardActionAssignee.objects.filter(action=action).values_list('user_id', flat=True))),
        tuple(sorted(
            BoardActionFieldRule.objects.filter(action=action).values_list('field_id', 'value', 'overwrite')
        )),
    )


def _cleaned_state(cleaned):
    return (
        cleaned['name'], cleaned['target_column'].pk, cleaned['assignee_mode'],
        cleaned['comment_required'], cleaned['message_template'], tuple(cleaned['assignee_ids']),
        tuple(sorted((field.pk, value, overwrite) for field, (value, overwrite) in cleaned['rules'].items())),
    )


def _write_action(action, cleaned):
    action.name = cleaned['name']
    action.target_column = cleaned['target_column']
    action.assignee_mode = cleaned['assignee_mode']
    action.comment_required = cleaned['comment_required']
    action.message_template = cleaned['message_template']
    action.save()
    BoardActionAssignee.objects.filter(action=action).delete()
    BoardActionAssignee.objects.bulk_create(
        [BoardActionAssignee(action=action, user_id=user_id) for user_id in cleaned['assignee_ids']]
    )
    BoardActionFieldRule.objects.filter(action=action).delete()
    BoardActionFieldRule.objects.bulk_create([
        BoardActionFieldRule(action=action, field=field, value=value, overwrite=overwrite)
        for field, (value, overwrite) in cleaned['rules'].items()
    ])


def create_action(board, *, actor, name, target_column, assignee_mode=BoardAction.AssigneeMode.KEEP,
                  assignee_ids=(), field_values=None, comment_required=False, message_template=''):
    """A new button «Передать дальше», the last of the board's.

    `target_column` is a working column of any sub-board of this board;
    `assignee_ids` the members `ADD`/`REPLACE` put on the card (required
    then, ignored for `KEEP`); `field_values` `{field: (raw, overwrite)}`,
    each parsed as a card's value (an empty one is no rule). At most
    `MAX_ACTIONS` live actions per board.
    """
    with transaction.atomic():
        board = _manageable_board(board.pk, 'create_action', actor=actor)
        if _live_action_count(board) >= MAX_ACTIONS:
            _rejected('create_action', 'limit', actor=actor, board_id=board.pk)
            raise BoardError(f'На доске уже {MAX_ACTIONS} действий — больше нельзя. Уберите лишнее в архив.')
        cleaned = _clean_action(
            board, name=name, target_column=target_column, assignee_mode=assignee_mode,
            assignee_ids=assignee_ids, field_values=field_values, comment_required=comment_required,
            message_template=message_template, operation='create_action', actor=actor,
        )
        position = (BoardAction.objects.filter(board=board).aggregate(last=Max('position'))['last'] or 0) + 1
        action = BoardAction(board=board, position=position, created_by=actor)
        _write_action(action, cleaned)
        _renumber_rows(list(BoardAction.objects.filter(board=board).order_by('position', 'pk')))
        _actions_changed(board, 'board.action_created', actor=actor, action_id=action.pk)
    return action


def update_action(action, *, actor, name, target_column, assignee_mode, assignee_ids=(),
                  field_values=None, comment_required=False, message_template=''):
    """Everything an action holds, at once — an archived one too (to point
    it at another column, say). The same settings store and publish nothing."""
    with transaction.atomic():
        board = _manageable_board(action.board_id, 'update_action', actor=actor)
        action = _action_of(board, action, operation='update_action', actor=actor)
        cleaned = _clean_action(
            board, name=name, target_column=target_column, assignee_mode=assignee_mode,
            assignee_ids=assignee_ids, field_values=field_values, comment_required=comment_required,
            message_template=message_template, operation='update_action', actor=actor,
            exclude_pk=action.pk,
        )
        if _action_state(action) == _cleaned_state(cleaned):
            return action
        _write_action(action, cleaned)
        _actions_changed(board, 'board.action_updated', actor=actor, action_id=action.pk)
    return action


def move_action(action, *, actor, direction):
    """One place towards the start (`'left'`) or the end (`'right'`)."""
    with transaction.atomic():
        board = _manageable_board(action.board_id, 'move_action', actor=actor)
        action = _action_of(board, action, operation='move_action', actor=actor)
        rows = _step(list(BoardAction.objects.filter(board=board).order_by('position', 'pk')), action, direction)
        if rows is None:
            return action
        _renumber_rows(rows)
        _actions_changed(board, 'board.action_moved', actor=actor, action_id=action.pk)
    return action


def archive_action(action, *, actor, archived=True):
    """«В архив» / «Вернуть»: an archived action is no button. Returning one
    is refused at `MAX_ACTIONS` live, or while its name is taken."""
    archived = bool(archived)
    with transaction.atomic():
        board = _manageable_board(action.board_id, 'archive_action', actor=actor)
        action = _action_of(board, action, operation='archive_action', actor=actor)
        if action.is_archived == archived:
            return action
        if not archived:
            if _live_action_count(board) >= MAX_ACTIONS:
                _rejected('archive_action', 'limit', actor=actor, board_id=board.pk)
                raise BoardError(f'На доске уже {MAX_ACTIONS} действий — вернуть ещё одно нельзя.')
            taken = BoardAction.objects.filter(board=board, is_archived=False).values_list('name', flat=True)
            if action.name.casefold() in {name.casefold() for name in taken}:
                raise BoardError(f'Действие «{action.name}» на этой доске уже есть — переименуйте одно из них.')
        action.is_archived = archived
        action.save(update_fields=['is_archived', 'updated_at'])
        _actions_changed(
            board, 'board.action_archived' if archived else 'board.action_restored',
            actor=actor, action_id=action.pk,
        )
    return action


def action_message(action, card, column, comment=''):
    """The message an action posts into «Чат»: its template, «{код}» and
    «{колонка}» put in, and the comment under it. `''` for neither."""
    text = (action.message_template or '').replace('{код}', card.code).replace('{колонка}', column.name).strip()
    comment = (comment or '').strip()
    return '\n\n'.join(part for part in (text, comment) if part)


def run_board_action(card, action, *, actor, comment=''):
    """Press «Передать в ПДО» on a card: all of it, or nothing.

    In one transaction, under the board → card → task locks, and only for
    whoever works on the board, on an open card that is no subtask, with a
    live action of this board whose column the card is not standing in:

    1. the move into `target_column` — `move_card()`'s own body
       (`_move_locked()`), the column's «Правила при входе» included, with
       one `MOVED` entry naming the action (`action_id`, `action`);
    2. the исполнители by `assignee_mode` (`KEEP` / `ADD` / `REPLACE` — a
       `REPLACE` whose people are all gone changes nothing), through
       `replace_task_assignees()`;
    3. the action's field values (`_apply_field_rules()`);
    4. the message — the template and the comment
       (`action_message()`) — in «Чат», from the presser, with the ordinary
       `BOARD_CARD_COMMENT` to the card's audience.

    Steps 2 and 3 are one `EDITED` entry «По действию «…»». Everybody the
    card gained is told once (`BOARD_TASK_ASSIGNED`), the new column's
    followers hear the card entered it. One `board.updated(card_moved)`.
    `comment_required` refuses an empty comment; any refusal or error in any
    step rolls everything back.
    """
    from notifications.services import notify_board_card_comment, notify_board_task_assigned
    from tasks.models import TaskAssignee
    from tasks.services import TaskWorkflowError, replace_task_assignees

    from .selectors import card_audience

    comment = (comment or '').strip()
    with transaction.atomic():
        board, card, task = _movable_card(card, 'run_action', actor=actor)
        card.board = board
        action = _action_of(board, action, operation='run_action', actor=actor)
        if action.is_archived:
            _rejected('run_action', 'archived_action', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(f'Действие «{action.name}» убрано в архив. Обновите страницу.')
        _refuse_closed_task(task, 'run_action', actor=actor, board=board, card=card)
        if action.comment_required and not comment:
            _rejected('run_action', 'comment_required', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(f'Для действия «{action.name}» нужен комментарий.')
        target = _board_working_column(
            board, action.target_column_id, operation='run_action', actor=actor, card_id=card.pk,
        )
        if target.pk == (card.column_id or _first_working_id(card.sub_board_id)):
            _rejected('run_action', 'same_column', actor=actor, board_id=board.pk, card_id=card.pk)
            raise BoardError(f'Карточка уже стоит в колонке «{target.name}».')
        message = action_message(action, card, target, comment)
        if len(message) > COMMENT_MAX_LENGTH:
            raise BoardError(f'Сообщение — не длиннее {COMMENT_MAX_LENGTH} символов.')
        assigned = lambda: set(TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True))  # noqa: E731
        initial = assigned()
        previous_sub_board_id, previous_column_id, _renumbered, _added, edited = _move_locked(
            board, card, task, target, actor=actor, operation='run_action', action=action,
        )
        # 2. The action's people.
        current = assigned()
        wanted = current
        if action.assignee_mode != BoardAction.AssigneeMode.KEEP:
            people = set(
                BoardActionAssignee.objects.filter(
                    active_employee_q('user__'), action=action, user__board_memberships__board=board,
                ).values_list('user_id', flat=True)
            )
            if action.assignee_mode == BoardAction.AssigneeMode.ADD:
                wanted = current | people
            elif people:
                wanted = people
        assignees_changed = wanted != current
        if assignees_changed:
            try:
                replace_task_assignees(task, sorted(wanted), actor=actor)
            except TaskWorkflowError as exc:
                raise BoardError(str(exc)) from exc
        # 3. The action's field values.
        field_names = _apply_field_rules(
            card,
            BoardActionFieldRule.objects.filter(action=action)
            .select_related('field').prefetch_related('field__options').order_by('pk'),
            operation='run_action',
        )
        if assignees_changed or field_names:
            fields, details = [], {}
            if assignees_changed:
                fields.append('assignees')
            if field_names:
                fields.append('custom')
                details['custom_fields'] = field_names
            _record(
                card, BoardCardEvent.Kind.EDITED, actor=actor,
                fields=fields, by_action_id=action.pk, by_action=action.name, **details,
            )
        if edited or assignees_changed or field_names:
            card.version += 1
            card.save(update_fields=['version'])
        added = sorted(assigned() - initial)
        if added:
            notify_board_task_assigned(task, actor, _users(added))
        _notify_column_entered(card, task, target, board, actor=actor, told=added)
        # 4. The message, as the presser's own.
        posted = None
        if message:
            posted = BoardCardComment.objects.create(card=card, author=actor, text=message)
            notify_board_card_comment(posted, task, actor, card_audience(card, task))
        emit_board_updated(board.pk, BOARD_CHANGE_CARD_MOVED, card.pk)
    log_event(
        logger,
        'INFO',
        'board.action_run',
        board_id=board.pk,
        board_card_id=card.pk,
        action_id=action.pk,
        previous_sub_board_id=previous_sub_board_id,
        previous_column_id=previous_column_id,
        column_id=target.pk,
        assignee_count=len(wanted),
        field_count=len(field_names),
        comment_id=getattr(posted, 'pk', None),
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return card
