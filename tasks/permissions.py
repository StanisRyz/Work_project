from acts.permissions import has_full_act_access, is_act_admin

from .models import Task


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
)


def can_view_task(task, user):
    return bool(getattr(user, 'is_authenticated', False))


def _tasks_queryset():
    return Task.objects.select_related(*_SOURCE_AWARE_SELECT_RELATED).prefetch_related(
        'assignees__user__userprofile'
    )


def get_visible_tasks_queryset(user):
    """Tasks in the user's working scope."""
    tasks = _tasks_queryset()
    return tasks if has_full_act_access(user) else tasks.filter(assignees__user=user).distinct()


def get_readable_tasks_queryset(user):
    """All tasks readable by an authenticated user."""
    tasks = _tasks_queryset()
    return tasks if getattr(user, 'is_authenticated', False) else tasks.none()


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
        is_act_admin(user) or task.assignees.filter(user=user).exists()
    )


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
    """
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
    here.
    """
    return can_upload_task_attachment(attachment.task, user)
