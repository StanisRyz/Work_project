from django.db.models import Q

from acts.permissions import has_full_act_access, is_act_admin
# The one `tasks` module that asks `boards`; `tasks.selectors` takes
# `can_use_boards` from here.
from boards.permissions import (  # noqa: F401 — can_use_boards is re-exported
    can_use_boards,
    can_view_board,
    has_full_board_access,
    readable_board_ids,
)

from .models import ROUTING_SOURCE_TYPES, Task


# The source relations are nullable, so every one of them is LEFT JOINed here:
# selecting them is safe for a task that has none, and the columns simply come
# back as NULL. Read-side code must therefore never assume `task.act` or
# `task.root_analysis` is present — ask `task.source_type` instead.
_SOURCE_AWARE_SELECT_RELATED = (
    'status', 'department', 'completed_by', 'act', 'act__status', 'root_analysis',
    # The protocol type carries the label «Качество №7», and the reverse
    # one-to-one `protocol_approval` carries the real outcome of an approval
    # queue entry; both are read for every registry row.
    'protocol__protocol_type', 'protocol_action', 'protocol_approval',
    # The СМК record is what the registry's «Источник» column names for an
    # `SMK` task; the measure behind it is not read there.
    'smk_source',
    # The same for a `BUG` task: the report is what «Источник» names and links.
    'bug_report',
    # And for the three «Документация» sources: the document the version
    # belongs to is what «Источник» names.
    'document_version__document',
    # And for a `BOARD` task: the board's name and id build its source label
    # and the link to the card.
    'board_card__board',
)


def can_view_task(task, user):
    """Every authenticated user — a `BOARD` task only for a reader of its board."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if task.source_type != Task.SourceType.BOARD:
        return True
    return can_view_board(user, task.board_card.board)


def _tasks_queryset():
    return Task.objects.select_related(*_SOURCE_AWARE_SELECT_RELATED).prefetch_related(
        'assignees__user__userprofile'
    )


def _without_boards_unless_allowed(tasks, user):
    """A `BOARD` task is a board's work: shown only to whoever reads that board.

    Full board access reads them all; anybody else the tasks of the boards
    they are a member of (`boards.permissions.readable_board_ids()`, one
    subquery, never restated) — so the registry with every tab and its Excel,
    the task page, the quick search, «Мои задачи», the dashboard counts and the
    `tasks` sync revision all follow it. `board_card` is a forward foreign key,
    so the condition repeats no row.
    """
    if has_full_board_access(user):
        return tasks
    return tasks.filter(
        ~Q(source_type=Task.SourceType.BOARD)
        | Q(board_card__board_id__in=readable_board_ids(user))
    )


def get_visible_tasks_queryset(user):
    """Tasks in the user's working scope."""
    tasks = _without_boards_unless_allowed(_tasks_queryset(), user)
    return tasks if has_full_act_access(user) else tasks.filter(assignees__user=user).distinct()


def get_readable_tasks_queryset(user):
    """Every task an authenticated user reads: all of them, a `BOARD` one with board access."""
    if not getattr(user, 'is_authenticated', False):
        return _tasks_queryset().none()
    return _without_boards_unless_allowed(_tasks_queryset(), user)


def _is_assignee(task, user):
    """Whether `user` is one of the task's assignees.

    The same `TaskAssignee` rows either way: read from the task's prefetched
    `assignees` when the caller already loaded them (a board reads every
    card's исполнители in one query), asked of the database otherwise.
    """
    prefetched = getattr(task, '_prefetched_objects_cache', {}).get('assignees')
    if prefetched is not None:
        return any(assignee.user_id == user.pk for assignee in prefetched)
    return task.assignees.filter(user=user).exists()


def can_complete_task(task, user):
    """Who may finish this task through the ordinary completion flow.

    A routing task is never completable here. Agreeing to a protocol is its
    own decision with its own endpoint, and closing it by posting an execution
    result would silently approve a document; an act stage is closed by moving
    the act itself. Stated once, in the one place both the view and the service
    ask.
    """
    if task.is_routing_task:
        return False
    return task.status.code == 'IN_PROGRESS' and (
        is_act_admin(user) or _is_assignee(task, user)
    )


def completable_task_ids(task_ids, user):
    """The ids among `task_ids` that `can_complete_task()` answers True for.

    The same rule, written as one query for a whole list — a board asks it for
    every open card at once instead of once per tile. Keep the two in step:
    not a routing entry, `IN_PROGRESS`, and an assignee or the administrative
    fallback.
    """
    if not getattr(user, 'is_authenticated', False):
        return set()
    tasks = Task.objects.filter(pk__in=list(task_ids), status__code='IN_PROGRESS').exclude(
        source_type__in=ROUTING_SOURCE_TYPES,
    )
    if not is_act_admin(user):
        tasks = tasks.filter(assignees__user=user)
    return set(tasks.values_list('pk', flat=True))


def can_reopen_task(task, user):
    """Who may take a finished task back out of «Архив», and which task.

    Administrative and nothing weaker — `is_act_admin()`, the same answer every
    other administrative question in the project gets, so the role «Администратор»
    and a superuser qualify and a руководитель does not. Reopening rewrites a
    fact the registry already reported as finished; the исполнитель who closed
    it by mistake asks an administrator rather than undoing it themselves.

    Two kinds of closed task are refused whoever asks:

    * a routing entry (`PROTOCOL_APPROVAL`, `ACT_WORKFLOW`) — it is not work
      anybody performs. It is closed by the protocol or the act moving, and
      putting it back would leave the queue claiming a stage the document has
      already left. This is the same rule `can_complete_task()` applies, from
      the same `is_routing_task`;
    * a `CANCELLED` one — it was withdrawn because the document behind it was
      corrected, and the replacement task is already assigned. Reviving it
      would ask for the same work twice.

    So `COMPLETED` is the only status this answers `True` for, which also makes
    it the exact inverse of `can_complete_task()`: never both at once.
    """
    if task.is_routing_task:
        return False
    return task.status.code == 'COMPLETED' and is_act_admin(user)


def can_upload_task_attachment(task, user):
    """Who may attach a file to this task.

    The same people who may finish it: an assignee of an active ordinary task,
    plus the administrative fallback `can_complete_task()` already applies. A
    routing task (`PROTOCOL_APPROVAL`, `ACT_WORKFLOW`) is excluded by that
    check too — its real action, and any file it needs, belong to the source
    document.

    Deliberately *not* wider than completion: every authenticated user may read
    a task, and read access has never granted a write.

    Never a `BOARD` task: a card's files are attached to a message of its
    «Чат» (the board's own files), and its task takes no new attachment. The
    ones it already has stay, and are removed by `can_delete_task_attachment()`
    as before.
    """
    if task.source_type == Task.SourceType.BOARD:
        return False
    return can_complete_task(task, user)


def can_download_task_attachment(attachment, user):
    """Whoever may read the task may read its files.

    Reading a task is open to every authenticated user, so this is that same
    answer — asked through the task, never through the attachment row, and
    never by trusting the URL.
    """
    return can_view_task(attachment.task, user)


def can_delete_task_attachment(attachment, user):
    """Who may remove a file already attached to a task.

    The same answer as uploading one — an assignee of the task plus the
    administrative fallback — and, because that answer is `can_complete_task()`,
    it also carries the rule that matters most here: only an `IN_PROGRESS` task
    accepts the change. A completed or cancelled task keeps every file it has,
    so its attachment history cannot be rewritten after the fact, and a routing
    task is excluded exactly as it is for upload.

    Deliberately *not* «whoever uploaded it»: a task is shared work, and an
    assignee correcting a colleague's mis-uploaded file is the normal case.
    Reading a task is open to every authenticated user and still grants nothing
    here. Asked of `can_complete_task()` directly rather than of the upload
    rule: a `BOARD` task takes no new file, but the files it got before keep
    being removable by the same people as ever.
    """
    return can_complete_task(attachment.task, user)
