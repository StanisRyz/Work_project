import logging
import time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ecosystem.logging_utils import log_event
from realtime.emitters import emit_task_completed, emit_task_created, emit_task_updated
from references.models import TaskStatus

from .models import Task, TaskAssignee
from .permissions import can_complete_task


logger = logging.getLogger('ecosystem.workflow')


class TaskWorkflowError(Exception):
    pass


def _pk_of(value):
    return getattr(value, 'pk', value if isinstance(value, int) else None)


def replace_task_assignees(task, users, *, actor=None):
    """Set a saved task's assignees to exactly `users`, atomically.

    The single place assignments may change. There is no UI for editing a
    task's assignees yet, so this exists as the safe extension point rather
    than as a feature: any future editing screen must call this instead of
    touching `TaskAssignee` directly, and no signal is involved.

    Bumping `Task.updated_at` is the whole point. Assignments live in a child
    table, so writing them leaves the task row — and therefore the real-time
    tasks revision derived from it — untouched unless the parent is saved
    explicitly. `actor` is accepted for symmetry with the other services and
    for future history/audit use; it is not a permission check.
    """
    target_ids = sorted({user.pk if hasattr(user, 'pk') else int(user) for user in users})
    if not target_ids:
        log_event(
            logger,
            'INFO',
            'task.operation_rejected',
            operation='replace_assignees',
            task_id=_pk_of(task),
            actor_user_id=_pk_of(actor),
            reason='no_assignees',
            outcome='rejected',
        )
        raise TaskWorkflowError('У задачи должен остаться хотя бы один исполнитель.')

    started = time.monotonic()
    with transaction.atomic():
        task = task.__class__.objects.select_for_update().get(pk=task.pk)
        current_ids = set(
            TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True)
        )
        removed = current_ids - set(target_ids)
        added = [user_id for user_id in target_ids if user_id not in current_ids]
        if not removed and not added:
            return task

        if removed:
            TaskAssignee.objects.filter(task=task, user_id__in=removed).delete()
        TaskAssignee.objects.bulk_create(
            [TaskAssignee(task=task, user_id=user_id) for user_id in added]
        )
        # Explicit, never a signal: `updated_at` is `auto_now=True`, which
        # Django only applies to fields listed in `update_fields`.
        task.save(update_fields=['updated_at'])
        emit_task_updated(task, changed_fields=('assignees',))
    # Counts only: who was added or removed is in the task's own records, and
    # a log file is not the place for a list of employees.
    log_event(
        logger,
        'INFO',
        'task.assignees_replaced',
        task_id=task.pk,
        act_id=task.act_id,
        actor_user_id=_pk_of(actor),
        assignee_count=len(target_ids),
        added_count=len(added),
        removed_count=len(removed),
        duration_ms=(time.monotonic() - started) * 1000,
        outcome='ok',
    )
    return task


def complete_task(task, user, execution_comment):
    """Complete one shared task once, on behalf of one assigned employee."""
    started = time.monotonic()

    def _rejected(reason, previous_status=None):
        # A second completion, a lost race or a missing right — an ordinary
        # outcome, recorded by reason code. The execution comment is the
        # employee's own text and is never logged.
        log_event(
            logger,
            'INFO',
            'task.operation_rejected',
            operation='complete',
            task_id=_pk_of(task),
            act_id=getattr(task, 'act_id', None),
            actor_user_id=_pk_of(user),
            previous_status=previous_status,
            reason=reason,
            duration_ms=(time.monotonic() - started) * 1000,
            outcome='rejected',
        )

    with transaction.atomic():
        task = task.__class__.objects.select_for_update().prefetch_related('assignees').get(pk=task.pk)
        previous_status = getattr(task.status, 'code', None)
        # Stated separately from the permission check so the refusal is
        # legible in the log: this is not "you may not", it is "this kind of
        # task is not finished this way". `can_complete_task` refuses it too.
        if task.source_type == Task.SourceType.PROTOCOL_APPROVAL:
            _rejected('protocol_approval_not_completable', previous_status)
            raise TaskWorkflowError('Задача согласования протокола не завершается таким образом.')
        if not can_complete_task(task, user):
            _rejected('not_permitted_or_already_completed', previous_status)
            raise TaskWorkflowError('Завершение задачи недоступно.')
        execution_comment = (execution_comment or '').strip()
        if not execution_comment:
            _rejected('empty_execution_comment', previous_status)
            raise TaskWorkflowError('Укажите результат выполнения задачи.')
        try:
            completed_status = TaskStatus.objects.get(code='COMPLETED', is_active=True)
        except TaskStatus.DoesNotExist as exc:
            log_event(
                logger,
                'ERROR',
                'task.operation_failed',
                operation='complete',
                task_id=_pk_of(task),
                act_id=getattr(task, 'act_id', None),
                actor_user_id=_pk_of(user),
                previous_status=previous_status,
                error_type='MissingCompletedTaskStatus',
                outcome='failed',
            )
            raise TaskWorkflowError('Не найден активный статус задачи «Выполнено».') from exc
        task.status = completed_status
        task.completed_by = user
        task.completed_at = timezone.now()
        task.execution_comment = execution_comment
        # `updated_at` is `auto_now=True`, but Django only bumps an auto_now
        # field when it is explicitly listed in `update_fields` — omitting it
        # here would silently leave the timestamp (and the sync revision token
        # derived from it) unchanged despite the field being auto_now.
        task.save(
            update_fields=['status', 'completed_by', 'completed_at', 'execution_comment', 'updated_at']
        )
        # Inside the lock: a second, parallel completion is refused by
        # `can_complete_task` above and never reaches this line.
        emit_task_completed(task)
    log_event(
        logger,
        'INFO',
        'task.completed',
        task_id=task.pk,
        act_id=task.act_id,
        actor_user_id=_pk_of(user),
        assignee_count=len(task.assignees.all()),
        previous_status=previous_status,
        next_status=completed_status.code,
        duration_ms=(time.monotonic() - started) * 1000,
        outcome='ok',
    )
    return task


# --------------------------------------------------------------------------
# Protocol workflow task lifecycle
#
# Protocol tasks are written *only* through these four functions, called only
# from `protocols/services.py` inside the workflow transaction that already
# holds the `Protocol` row lock. They are deliberately not transactional on
# their own: a protocol transition that fails halfway must take every task it
# created with it, so the caller's `atomic()` block is the unit of work.
#
# `complete_task()` stays untouched and keeps refusing `PROTOCOL_APPROVAL`:
# an approval task is closed by the protocol decision, never by an employee
# posting an execution result.
# --------------------------------------------------------------------------


def _active_status(code, label):
    try:
        return TaskStatus.objects.get(code=code, is_active=True)
    except TaskStatus.DoesNotExist as exc:
        log_event(
            logger,
            'ERROR',
            'task.operation_failed',
            operation='resolve_status',
            status_code=code,
            error_type='MissingTaskStatus',
            outcome='failed',
        )
        raise TaskWorkflowError(f'Не найден активный статус задачи «{label}».') from exc


def _save_new_task(task, assignee_ids, *, actor):
    """Validate the source shape, save, attach assignees, announce once."""
    if not assignee_ids:
        raise TaskWorkflowError('У задачи должен быть хотя бы один исполнитель.')
    try:
        # Restates the check constraint in readable form, and adds the
        # cross-table `protocol_action.protocol == protocol` rule SQL cannot.
        task.clean()
    except ValidationError as exc:
        raise TaskWorkflowError('Некорректный источник задачи.') from exc
    task.save()
    TaskAssignee.objects.bulk_create(
        [TaskAssignee(task=task, user_id=user_id) for user_id in assignee_ids]
    )
    # Only once the assignees exist, so the event never describes a half-built
    # task; `publish_after_commit` keeps a rolled-back transaction silent.
    emit_task_created(task, assignee_ids)
    log_event(
        logger,
        'INFO',
        'task.created',
        task_id=task.pk,
        protocol_id=task.protocol_id,
        source_type=task.source_type,
        actor_user_id=_pk_of(actor),
        assignee_count=len(assignee_ids),
        next_status=task.status.code,
        outcome='ok',
    )
    return task


def create_act_action_task(action, assignee_ids, *, created_by, individual_assignee_id=None):
    """The real task an act corrective action becomes once the act is approved.

    Two shapes of the same call, exactly as `create_protocol_action_task()` has:
    without `individual_assignee_id` this is the shared task the corrective
    action has always produced, carrying every assignee; with one it is a task
    split off for that single person, and then the assignee list must be
    exactly them — a split task whose `TaskAssignee` rows disagreed with the
    name on the task would be completable by someone the task does not
    represent.

    The act, the root analysis, the wording, the department and the deadline
    are read from the corrective action itself, so a caller cannot pair a task
    with the wrong act; `Task.clean()` inside `_save_new_task()` then re-checks
    that whole chain and that the individual really is an assignee of it. The
    two uniqueness rules are the constraints on `Task`, not a check here.

    This owns the task, not the decision to make one: how many to create, and
    whether the act may be approved at all, stay in `acts/services.py`.
    """
    unique_ids = sorted(set(assignee_ids))
    if individual_assignee_id is not None and unique_ids != [individual_assignee_id]:
        raise TaskWorkflowError(
            'Персональная задача по акту создаётся ровно на одного исполнителя.'
        )
    task = Task(
        # Stated, not inferred: the source type is what the task registry, the
        # completion guard and the source constraint read.
        source_type=Task.SourceType.ACT,
        act=action.root_analysis.act,
        root_analysis=action.root_analysis,
        source_action=action,
        individual_assignee_id=individual_assignee_id,
        task_text=action.comment,
        department=action.department,
        due_date=action.due_date,
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    return _save_new_task(task, unique_ids, actor=created_by)


def create_protocol_approval_task(protocol, approver, *, department, due_date, created_by, task_text):
    """One `PROTOCOL_APPROVAL` task: one protocol, one approver, no action."""
    task = Task(
        source_type=Task.SourceType.PROTOCOL_APPROVAL,
        protocol=protocol,
        task_text=task_text,
        department=department,
        due_date=due_date,
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    return _save_new_task(task, [approver.pk], actor=created_by)


def create_protocol_action_task(
    protocol, action, assignee_ids, *, created_by, individual_assignee_id=None
):
    """The real task a protocol decision becomes once the protocol archives.

    Two shapes of the same call. Without `individual_assignee_id` this is the
    shared task the decision has always produced, carrying every assignee.
    With one it is a task split off for that single person, and then the
    assignee list must be exactly them: a split task whose `TaskAssignee` rows
    disagreed with the name on the task would be completable by someone the
    task does not represent.

    Neither uniqueness rule is re-checked here — the two constraints on `Task`
    are the guarantee that a decision has at most one shared task and at most
    one task per assignee, and that the individual really is an assignee of the
    decision is `Task.clean()`'s cross-table rule inside `_save_new_task()`.
    """
    unique_ids = sorted(set(assignee_ids))
    if individual_assignee_id is not None and unique_ids != [individual_assignee_id]:
        raise TaskWorkflowError(
            'Персональная задача протокола создаётся ровно на одного исполнителя.'
        )
    task = Task(
        source_type=Task.SourceType.PROTOCOL_ACTION,
        protocol=protocol,
        protocol_action=action,
        individual_assignee_id=individual_assignee_id,
        task_text=action.task_text,
        department=action.department,
        due_date=action.due_date,
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    return _save_new_task(task, unique_ids, actor=created_by)


def _close_approval_task(task, *, approver, decided_at, reason):
    if task is None:
        return None
    if task.source_type != Task.SourceType.PROTOCOL_APPROVAL:
        raise TaskWorkflowError('Так закрывается только задача согласования протокола.')
    task.status = _active_status('COMPLETED', 'Выполнено')
    task.completed_by = approver
    task.completed_at = decided_at
    # `auto_now` fields are only bumped when named in `update_fields`.
    task.save(update_fields=['status', 'completed_by', 'completed_at', 'updated_at'])
    emit_task_completed(task)
    log_event(
        logger,
        'INFO',
        'task.protocol_approval_closed',
        task_id=task.pk,
        protocol_id=task.protocol_id,
        actor_user_id=_pk_of(approver),
        reason=reason,
        next_status='COMPLETED',
        outcome='ok',
    )
    return task


def complete_protocol_approval_task(task, approver, decided_at):
    """Close an approval task because that person approved.

    No execution comment: the decision itself is the result, and it is recorded
    on the `ProtocolApproval` row, not in the task's free text.
    """
    return _close_approval_task(task, approver=approver, decided_at=decided_at, reason='approved')


def cancel_protocol_approval_task(task, decided_at):
    """Close an approval task that is no longer needed, claiming nothing.

    Used when the protocol goes back for revision: the remaining approvers no
    longer have anything to sign, so `completed_by` stays NULL rather than
    naming someone who never decided.
    """
    return _close_approval_task(task, approver=None, decided_at=decided_at, reason='cancelled')
