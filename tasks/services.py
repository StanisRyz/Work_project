import logging
import time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ecosystem.logging_utils import log_event
from realtime.emitters import emit_task_completed, emit_task_created, emit_task_updated
from references.models import TaskStatus

from .models import Task, TaskAssignee
from .permissions import can_complete_task, can_upload_task_attachment


logger = logging.getLogger('ecosystem.workflow')
attachment_logger = logging.getLogger('ecosystem.attachments')


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
        if task.is_routing_task:
            reason = (
                'protocol_approval_not_completable'
                if task.source_type == Task.SourceType.PROTOCOL_APPROVAL
                else 'act_workflow_not_completable'
            )
            _rejected(reason, previous_status)
            raise TaskWorkflowError(
                'Задача согласования протокола не завершается таким образом.'
                if task.source_type == Task.SourceType.PROTOCOL_APPROVAL
                else 'Этап обработки акта закрывается действием в самом акте.'
            )
        if not can_complete_task(task, user):
            _rejected('not_permitted_or_already_completed', previous_status)
            raise TaskWorkflowError('Завершение задачи недоступно.')
        execution_comment = (execution_comment or '').strip()
        if not execution_comment:
            _rejected('empty_execution_comment', previous_status)
            raise TaskWorkflowError('Укажите результат выполнения задачи.')
        # The attachment requirement, checked here and nowhere else: the page
        # only announces it, and the button is never hidden or disabled for it.
        # `requires_attachment` is this task's own snapshot, so a later edit of
        # the protocol decision or corrective action behind it changes nothing;
        # and the attachments are this task's own, so a split исполнитель
        # cannot be carried by a colleague's separate task. Who uploaded the
        # file, how many there are and what kind they are is deliberately not
        # asked — one attachment existing is the whole rule.
        if task.requires_attachment and not task.attachments.exists():
            _rejected('missing_required_attachment', previous_status)
            raise TaskWorkflowError('Для выполнения этой задачи необходимо добавить вложение.')
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
        act_id=task.act_id,
        protocol_id=task.protocol_id,
        source_type=task.source_type,
        workflow_stage=task.workflow_stage or None,
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
        # A snapshot, taken now and never read back: the corrective action stays
        # editable until this task exists, and a completed task must keep saying
        # what was required of it. A split action gives every one of its tasks
        # the same requirement, so each исполнитель satisfies it on their own
        # task; a shared one is satisfied by any single attachment on it.
        requires_attachment=action.requires_attachment,
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
        # A snapshot, exactly as `create_act_action_task()` takes one: the
        # decision stays editable until this task exists, and both execution
        # modes carry the same requirement — one shared task satisfied by any
        # single attachment, or one per assignee each satisfied on its own.
        # `PROTOCOL_APPROVAL` never gets it: an approval is not executed here.
        requires_attachment=action.requires_attachment,
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    return _save_new_task(task, unique_ids, actor=created_by)


# --------------------------------------------------------------------------
# СМК workflow task lifecycle
#
# Written *only* through `create_smk_action_task()` and `cancel_smk_action_tasks()`,
# called only from `smk/services.py` inside the transaction that stores or
# corrects the record. Not transactional on their own, for the same reason the
# protocol functions above are not: a record that fails halfway must take every
# task it created with it — and a correction that fails must give the cancelled
# ones back.
# --------------------------------------------------------------------------


def create_smk_action_task(
    source, action, assignee_ids, *, created_by, individual_assignee_id=None
):
    """The real task an СМК корректирующее мероприятие becomes.

    The same two shapes `create_act_action_task()` and
    `create_protocol_action_task()` have. Without `individual_assignee_id` this
    is the shared task the measure produces, carrying every исполнитель; with
    one it is a task split off for that single person, and then the assignee
    list must be exactly them — a split task whose `TaskAssignee` rows
    disagreed with the name on the task would be completable by someone the
    task does not represent.

    The wording, the department and the deadline are read from the measure
    itself, so a caller cannot pair a task with the wrong record;
    `Task.clean()` inside `_save_new_task()` then re-checks that the measure
    really belongs to the record it was given and that the individual really is
    one of its исполнители.

    «At most one shared task per measure, and at most one per исполнитель» is
    the pair of constraints on `Task`, not a check here.
    """
    unique_ids = sorted(set(assignee_ids))
    if individual_assignee_id is not None and unique_ids != [individual_assignee_id]:
        raise TaskWorkflowError(
            'Персональная задача СМК создаётся ровно на одного исполнителя.'
        )
    task = Task(
        source_type=Task.SourceType.SMK,
        smk_source=source,
        smk_action=action,
        individual_assignee_id=individual_assignee_id,
        task_text=action.task_text,
        department=action.department,
        due_date=action.due_date,
        # A snapshot, exactly as the act and protocol variants take one: the
        # measure is stored a moment earlier in the same transaction, and a
        # completed task must keep saying what was required of it. The
        # requirement itself is enforced only by `complete_task()`.
        requires_attachment=action.requires_attachment,
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    return _save_new_task(task, unique_ids, actor=created_by)


def create_bug_report_task(report, assignee_ids, *, created_by, due_date):
    """The real task a «Сообщить об ошибке» report becomes.

    One shape only: a bug is one piece of work, shared by everybody flagged
    «Ответственный за ошибки», so whoever fixes it closes it for the rest.
    There is no `individual_assignee` argument — splitting a single bug between
    five people would ask five of them to fix it once each — and no
    `department`, because the assignees are chosen by a flag rather than by a
    unit and may sit in any number of them.

    The wording is the report's own message, read from the report itself so a
    caller cannot pair a task with the wrong text; the deadline is passed in,
    because how long a bug may wait is `bugs/services.py`'s policy and not this
    module's. «One task per report» is `unique_bug_report_task`, not a check
    here.
    """
    task = Task(
        source_type=Task.SourceType.BUG,
        bug_report=report,
        task_text=report.message,
        due_date=due_date,
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    return _save_new_task(task, sorted(set(assignee_ids)), actor=created_by)


def cancel_smk_action_tasks(actions, *, actor, reason):
    """Close the live tasks of the given СМК мероприятия, without completing one.

    Called by `smk.services.update_smk_source()` for exactly the measures a
    correction changed or removed — never for the whole record: a measure that
    came back unchanged keeps the task its исполнитель already holds. The rows
    stay — nothing is deleted — and `reason` is the sentence a person reads on
    the task page.

    Tasks in a final status are left exactly as they are: a completed task is a
    thing that really happened, and cancelling it would rewrite that. So is a
    task already cancelled by an earlier correction.

    Returns the tasks it closed, so the caller can name them in the record's
    history.
    """
    action_ids = [action.pk for action in actions]
    if not action_ids:
        return []
    cancelled_status = _active_status('CANCELLED', 'Отменена')
    cancelled_at = timezone.now()
    closed = []
    tasks = (
        Task.objects.select_for_update()
        .filter(source_type=Task.SourceType.SMK, smk_action_id__in=action_ids)
        .exclude(status__is_final=True)
        .order_by('pk')
    )
    for task in tasks:
        previous_status = task.status.code
        task.status = cancelled_status
        task.cancelled_at = cancelled_at
        task.cancelled_by = actor
        task.cancellation_reason = reason
        # `auto_now` is only applied to fields named in `update_fields`, so
        # `updated_at` — and the real-time revision token derived from it — is
        # listed explicitly, exactly as `complete_task()` lists it.
        task.save(
            update_fields=[
                'status', 'cancelled_at', 'cancelled_by', 'cancellation_reason',
                'updated_at',
            ]
        )
        emit_task_updated(task, changed_fields=('status',))
        log_event(
            logger,
            'INFO',
            'task.cancelled',
            task_id=task.pk,
            source_type=task.source_type,
            smk_source_id=task.smk_source_id,
            actor_user_id=_pk_of(actor),
            previous_status=previous_status,
            next_status=cancelled_status.code,
            outcome='ok',
        )
        closed.append(task)
    return closed


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


# --------------------------------------------------------------------------
# Act workflow task lifecycle
#
# The act's route made visible in «Задачи». One active routing task per act at
# a time, naming the stage the act is waiting on and assigned to every active
# holder of the role that has to act. Written *only* through these functions,
# called only from `acts/services.py` inside the transaction that already holds
# the act row lock — which is also what makes "at most one active task per act"
# true without a database constraint: two transitions of one act cannot run at
# the same time.
#
# These tasks are never completed by an employee. `complete_task()` and
# `can_complete_task()` both refuse them, exactly as they refuse a protocol
# approval task: the real action is «внести решение КО», «выполнить анализ ТО»
# or «утвердить акт», and it is taken on the act.
# --------------------------------------------------------------------------


# What the person is being asked to do, per stage. One sentence, no act number:
# the task's source link leads to the act itself.
WORKFLOW_STAGE_TEXT = {
    Task.WorkflowStage.KO_REVIEW: 'Рассмотреть акт и внести решение КО.',
    Task.WorkflowStage.TO_ANALYSIS: 'Выполнить анализ ТО по акту.',
    Task.WorkflowStage.OTK_REVIEW: 'Проверить акт и утвердить его или вернуть в ТО.',
    Task.WorkflowStage.OTK_REWORK: 'Доработать акт после возврата и передать его в КО.',
}


def _workflow_task_department(assignees):
    """The department to show on a routing task, or `None`.

    A stage belongs to a *role*, not to one department, so there is nothing
    authoritative to store: the first assignee who has a department lends its
    name to the row, and an installation where nobody has one simply shows no
    department. Never a reason to refuse an act transition.
    """
    for user in assignees:
        profile = getattr(user, 'userprofile', None)
        department = getattr(profile, 'department', None) if profile is not None else None
        if department is not None:
            return department
    return None


def close_act_workflow_tasks(act, *, closed_at=None, reason='stage_finished'):
    """Close every active routing task of this act. Returns how many.

    Called before opening the next stage's task and on approval, when there is
    no next stage. `completed_by` stays NULL on purpose: nobody "performed" a
    queue entry — the act moved, and the entry stopped being relevant.
    """
    closed_at = closed_at or timezone.now()
    tasks = list(
        Task.objects.select_for_update()
        .filter(act=act, source_type=Task.SourceType.ACT_WORKFLOW)
        .exclude(status__code='COMPLETED')
    )
    if not tasks:
        return 0
    completed = _active_status('COMPLETED', 'Выполнено')
    for task in tasks:
        task.status = completed
        task.completed_at = closed_at
        # `auto_now` fields are only bumped when named in `update_fields`.
        task.save(update_fields=['status', 'completed_at', 'updated_at'])
        emit_task_completed(task)
        log_event(
            logger,
            'INFO',
            'task.act_workflow_closed',
            task_id=task.pk,
            act_id=task.act_id,
            workflow_stage=task.workflow_stage,
            reason=reason,
            next_status='COMPLETED',
            outcome='ok',
        )
    return len(tasks)


def create_act_workflow_task(act, stage, assignees, *, created_by, due_date=None):
    """One shared routing task for the stage this act is now waiting on.

    `assignees` is every active holder of the role that has to act. An empty
    list creates nothing and is *not* an error: a plant with no active КО
    employee must still be able to send an act to КО, exactly as it already
    receives no notification. The act's own deadline is the task's deadline —
    the stage is what has to happen before it.
    """
    assignees = [user for user in assignees if user is not None]
    if not assignees:
        log_event(
            logger,
            'INFO',
            'task.act_workflow_skipped',
            act_id=_pk_of(act),
            workflow_stage=stage,
            reason='no_active_assignees',
            outcome='skipped',
        )
        return None
    task = Task(
        source_type=Task.SourceType.ACT_WORKFLOW,
        act=act,
        workflow_stage=stage,
        task_text=WORKFLOW_STAGE_TEXT[stage],
        department=_workflow_task_department(assignees),
        due_date=due_date or act.due_date or timezone.localdate(),
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    return _save_new_task(task, sorted({user.pk for user in assignees}), actor=created_by)


def move_act_workflow_task(act, stage, assignees, *, created_by, reason='stage_changed'):
    """Close the act's current routing task and open the next one, in order.

    The single entry point `acts/services.py` uses for every transition, so the
    queue can never show two stages of one act at once, nor keep showing a
    stage the act has left. `stage=None` closes without opening anything —
    approval, where the act's route ends.
    """
    close_act_workflow_tasks(act, reason=reason)
    if stage is None:
        return None
    return create_act_workflow_task(act, stage, assignees, created_by=created_by)


def active_users_for_role(role):
    """Active users whose active profile carries `role`.

    The same rule `notifications.services` routes act events by — an inactive
    account or an inactive profile holds no role — expressed once here so the
    task queue and the notification cannot address different people.
    """
    from django.contrib.auth import get_user_model

    return list(
        get_user_model()
        .objects.select_related('userprofile__department')
        .filter(is_active=True, userprofile__is_active=True, userprofile__role=role)
        .order_by('pk')
    )


# --------------------------------------------------------------------------
# The ПДО rejection task
#
# When КО prohibits the use of defective «Цех МП» products, someone has to
# plan replacements. That is real, executable work — an ordinary task ПДО
# finishes with an execution comment — and deliberately not an `ACT` task: it
# comes from the КО decision, months before the ТО analysis exists, so there
# is no corrective action to hang it on.
#
# Recipients are resolved by *organisational department*, not by role: the
# people who plan production are the employees of Department `PDO`, whatever
# role each of them holds. A Руководитель working there is included; a ПДО-role
# user filed under another department is not.
# --------------------------------------------------------------------------


# The department that plans replacement products. A code, not a name: names are
# editable in Admin, the code is the identifier the seed migration writes.
PDO_DEPARTMENT_CODE = 'PDO'

# One rejected defect, one line — the task page renders the text with
# `linebreaksbr`, so the sentences stay separate facts.
LINE_SEPARATOR = '\n'


def _rejection_value(value, fallback='—'):
    """A printable value for a field that legacy data may have left blank.

    The «Цех МП» profile requires all of these, so on current data the fallback
    never shows. It exists because an act stored years ago must not be able to
    abort a КО transition over an empty string.
    """
    text = str(value).strip() if value not in (None, '') else ''
    return text or fallback


def describe_rejected_defect(act, defect):
    """One sentence about one prohibited «Цех МП» defect.

    «<номенклатура> забраковано <N> шт. по заказу №<заказ>, ЗНП №<ЗНП>,
    Партия №<партия>.» — the wording the plant uses. Quantities are never
    added together across defects: two ЗНП rows are two facts, and a synthetic
    total would describe a batch that does not exist.
    """
    return (
        f'{_rejection_value(act.nomenclature)} забраковано '
        f'{_rejection_value(defect.nonconforming_quantity, "—")} шт. '
        f'по заказу №{_rejection_value(act.order_number)}, '
        f'ЗНП №{_rejection_value(defect.znp_number)}, '
        f'Партия №{_rejection_value(defect.party_number)}.'
    )


def get_pdo_recipients():
    """Active employees of the active Department `PDO`, ordered by pk.

    By department, not by role — planning replacement products is what that
    department does, and its members' roles are irrelevant. An absent or
    deactivated department yields nobody, which the caller treats as "skip",
    never as a failure.
    """
    from django.contrib.auth import get_user_model

    return list(
        get_user_model()
        .objects.select_related('userprofile__department')
        .filter(
            is_active=True,
            userprofile__is_active=True,
            userprofile__department__is_active=True,
            userprofile__department__code=PDO_DEPARTMENT_CODE,
        )
        .order_by('pk')
    )


def ensure_act_rejection_task(act, defects, *, created_by):
    """The one ПДО rejection task for this act, created at most once.

    `defects` is every already-saved defect of the act whose КО decision
    prohibits use and whose workshop is «Цех МП», in the act's own order. An
    empty list creates nothing. One task for the whole act — one sentence per
    defect, one line each — because ПДО plans a replacement for the act, not
    a separate errand per ЗНП row.

    Idempotent on two levels: the existence check below covers the ordinary
    repeat, and `unique_act_rejection_task` covers the race the check cannot
    see. Called from inside the КО transition's `atomic()` block under the act
    row lock, so it rolls back with the transition — and an
    `IntegrityError` from anything *other* than that unique index is left to
    propagate rather than swallowed.
    """
    if not defects:
        return None
    if Task.objects.filter(act=act, source_type=Task.SourceType.ACT_REJECTION).exists():
        log_event(
            logger,
            'INFO',
            'task.act_rejection_skipped',
            act_id=_pk_of(act),
            reason='already_exists',
            outcome='skipped',
        )
        return None

    recipients = get_pdo_recipients()
    if not recipients:
        # Never a reason to refuse the КО decision: the act still has to move
        # to ТО, and a plant with no ПДО account simply gets no task.
        log_event(
            logger,
            'INFO',
            'task.act_rejection_skipped',
            act_id=_pk_of(act),
            reason='no_pdo_recipients',
            outcome='skipped',
        )
        return None

    department = recipients[0].userprofile.department
    task = Task(
        source_type=Task.SourceType.ACT_REJECTION,
        act=act,
        task_text=LINE_SEPARATOR.join(
            describe_rejected_defect(act, defect) for defect in defects
        ),
        department=department,
        # The act's own review deadline. Legacy rows may have none, and a
        # missing deadline must not abort the transition either.
        due_date=act.due_date or timezone.localdate(),
        created_by=created_by,
        status=_active_status('IN_PROGRESS', 'В работе'),
    )
    task = _save_new_task(task, [user.pk for user in recipients], actor=created_by)
    # Only after the task and its assignees exist, and only on the one call
    # that really created it — the early returns above are what keeps a
    # repeated or concurrent КО transition from notifying ПДО twice.
    from notifications.services import notify_act_rejection_task_assigned

    notify_act_rejection_task_assigned(task, created_by, recipients)
    return task


# --------------------------------------------------------------------------
# Task attachments
#
# Optional files on an ordinary, executable task. Upload is its own request,
# never part of completion, so a task is still finished with its execution
# comment and no file at all. The one upload policy in
# `ecosystem.attachments` decides what may be stored; `tasks.permissions`
# decides who may store it; this decides nothing and only writes.
# --------------------------------------------------------------------------


def add_task_attachment(task, user, uploaded_file):
    """Attach one file to an ordinary task, atomically.

    Storage is not transactional with the database, so the file is written
    first and removed again if the row cannot be saved — the act attachment
    service does exactly this, and an orphaned file is worse than a failed
    upload. The permission is re-checked under the task's row lock, because a
    task completed meanwhile no longer accepts uploads.
    """
    from .models import TaskAttachment

    if not can_upload_task_attachment(task, user):
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.access_denied',
            task_id=_pk_of(task),
            user_id=_pk_of(user),
            operation='upload',
            outcome='denied',
        )
        raise TaskWorkflowError('Добавление вложения к задаче недоступно.')

    attachment = TaskAttachment(
        task=task,
        uploaded_by=user if getattr(user, 'is_authenticated', False) else None,
        original_name=uploaded_file.name,
        file_size=getattr(uploaded_file, 'size', 0) or 0,
        content_type=getattr(uploaded_file, 'content_type', '') or '',
    )
    file_written = False
    try:
        attachment.file.save(uploaded_file.name, uploaded_file, save=False)
        file_written = True
        with transaction.atomic():
            locked = (
                Task.objects.select_for_update()
                .select_related('status')
                .prefetch_related('assignees')
                .get(pk=task.pk)
            )
            if not can_upload_task_attachment(locked, user):
                raise TaskWorkflowError('Добавление вложения к задаче недоступно.')
            attachment.task = locked
            attachment.save()
    except Exception:
        if file_written:
            _delete_task_attachment_file(attachment)
        raise

    # Identifiers and a size only — never the file's name or its path.
    log_event(
        attachment_logger,
        'INFO',
        'attachment.uploaded',
        attachment_id=attachment.pk,
        task_id=_pk_of(task),
        user_id=_pk_of(user),
        size_bytes=attachment.file_size,
        operation='upload',
        outcome='ok',
    )
    return attachment


def _delete_task_attachment_file(attachment):
    """Best-effort cleanup of a file whose row was never stored."""
    try:
        attachment.file.storage.delete(attachment.file.name)
    except Exception as exc:  # noqa: BLE001 - storage cleanup is best-effort
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.storage_failed',
            task_id=getattr(attachment, 'task_id', None),
            operation='upload_rollback',
            error_type=type(exc).__name__,
            outcome='orphan_cleanup_failed',
        )
