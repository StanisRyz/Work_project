import logging
import time
from contextlib import contextmanager
from functools import partial

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import UserProfile
from ecosystem.logging_utils import log_event
from realtime.emitters import (
    emit_act_status_changed,
    emit_act_updated,
    emit_comment_created,
    emit_task_created,
)

from .models import Act, ActAttachment, ActComment, ActCorrectiveAction, ActCorrectiveActionAssignee, ActHistoryEvent, ActNumberSequence, ActRootAnalysis, get_act_status
from .permissions import (
    can_apply_ko_decision,
    can_apply_to_analysis,
    can_close_act,
    can_delete_attachment,
    can_edit_act,
    can_return_to_otk,
    can_return_to_ko,
    can_return_to_to,
    can_approve_act,
    can_add_attachment,
    can_send_to_ko,
    can_view_act,
    is_act_admin,
    get_user_role,
    get_visible_acts_queryset,
)


logger = logging.getLogger('ecosystem.workflow')
attachment_logger = logging.getLogger('ecosystem.attachments')


class ActWorkflowError(Exception):
    pass


@contextmanager
def _workflow_logging(action, act_or_pk, user):
    """Log one act transition: started, then completed / rejected / failed.

    Only identifiers, status codes, a duration and an outcome are recorded.
    Never the return comment, the KO decision text, the root cause, the
    corrective-action text, or any customer or party data — the act's own
    `ActHistoryEvent` records remain the business audit trail, and these lines
    exist purely to diagnose *why* an operation behaved as it did.

    `previous_status` is captured lazily inside the block, because the caller
    only learns the authoritative status after the row lock is taken.
    """
    state = {'previous_status': None, 'next_status': None, 'act_id': _pk_of(act_or_pk)}
    started = time.monotonic()
    log_event(
        logger,
        'DEBUG',
        'workflow.transition_started',
        action=action,
        act_id=state['act_id'],
        actor_user_id=_pk_of(user),
    )
    try:
        yield state
    except ActWorkflowError as exc:
        # A refusal is an ordinary outcome — a stale tab, a lost race, a role
        # without the right. The message is the application's own fixed text,
        # never user input, but the outcome code is what matters here.
        log_event(
            logger,
            'INFO',
            'workflow.transition_rejected',
            action=action,
            act_id=state['act_id'],
            actor_user_id=_pk_of(user),
            previous_status=state['previous_status'],
            duration_ms=(time.monotonic() - started) * 1000,
            reason=type(exc).__name__,
            outcome='rejected',
        )
        raise
    except Exception as exc:
        log_event(
            logger,
            'ERROR',
            'workflow.transition_failed',
            action=action,
            act_id=state['act_id'],
            actor_user_id=_pk_of(user),
            previous_status=state['previous_status'],
            duration_ms=(time.monotonic() - started) * 1000,
            error_type=type(exc).__name__,
            outcome='failed',
            exc_info=True,
        )
        raise
    else:
        log_event(
            logger,
            'INFO',
            'workflow.transition_completed',
            action=action,
            act_id=state['act_id'],
            actor_user_id=_pk_of(user),
            previous_status=state['previous_status'],
            next_status=state['next_status'],
            duration_ms=(time.monotonic() - started) * 1000,
            outcome='ok',
        )


def _pk_of(value):
    return getattr(value, 'pk', value if isinstance(value, int) else None)


def _status_code_of(status):
    return getattr(status, 'code', None)


def lock_act_for_update(act_or_pk):
    """Load and row-lock the current act, returning a fresh instance.

    Must be called inside `transaction.atomic()`. The caller's argument may be
    a stale in-memory act: the returned instance is always re-read from the
    database, so permission and status re-checks run against current state, not
    against whatever the request happened to load earlier.

    Lock order across the module is fixed: Act first, then its dependent
    defects / analyses / corrective actions, then tasks, then history and
    notification records.
    """
    pk = getattr(act_or_pk, 'pk', act_or_pk)
    # Deliberately no select_related(): on PostgreSQL a joined SELECT ... FOR
    # UPDATE would also lock the shared ActStatus reference rows and serialise
    # unrelated acts that happen to share a status. `status` is loaded lazily.
    return Act.objects.select_for_update().get(pk=pk)


def _lock_act_defects(act):
    """Row-lock this act's defects, after the act itself is already locked."""
    # Locked without select_related so no joined reference table is locked too.
    locked_ids = list(act.defects.select_for_update().order_by('pk').values_list('pk', flat=True))
    return list(
        act.defects.filter(pk__in=locked_ids).select_related('defect_type').order_by('pk')
    )


def _lock_act_root_analyses(act):
    """Row-lock this act's root analyses and corrective actions, in that order."""
    root_ids = list(
        ActRootAnalysis.objects.select_for_update()
        .filter(act=act)
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    action_ids = list(
        ActCorrectiveAction.objects.select_for_update()
        .filter(root_analysis_id__in=root_ids)
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    return root_ids, action_ids


def send_to_ko(act, user):
    with _workflow_logging('send_to_ko', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_send_to_ko(act, user):
            raise ActWorkflowError('Передача акта в КО недоступна для вашей роли или текущего статуса.')
        _require_status(act, 'CREATED_OTK')
        from_status = act.status
        to_status = _get_required_status('KO_REVIEW')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        act.status = to_status
        act.save(update_fields=['status', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.SENT_TO_KO,
            'Акт передан в КО для рассмотрения.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def apply_ko_decision(act, user, defect_decisions):
    with _workflow_logging('apply_ko_decision', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_apply_ko_decision(act, user):
            raise ActWorkflowError('Решение КО недоступно для вашей роли или текущего статуса.')
        _require_status(act, 'KO_REVIEW')
        defect_decisions = list(defect_decisions)
        # Re-read the act's defects under lock and match submitted decisions by
        # primary key, so a decision aimed at another act's defect — or at a
        # defect deleted meanwhile — is rejected instead of silently applied.
        current_defects = {defect.pk: defect for defect in _lock_act_defects(act)}
        if current_defects:
            received_ids = [defect.pk if defect is not None else None for defect, _d, _c in defect_decisions]
            if None in received_ids:
                raise ActWorkflowError('Необходимо внести решение КО по каждому дефекту.')
            if len(received_ids) != len(set(received_ids)):
                raise ActWorkflowError('Необходимо внести решение КО по каждому дефекту.')
            if set(received_ids) != set(current_defects):
                raise ActWorkflowError('Необходимо внести решение КО по каждому дефекту.')
            # Replace any stale instance the caller passed with the locked one.
            defect_decisions = [
                (current_defects[defect.pk], decision, comment)
                for defect, decision, comment in defect_decisions
            ]
        elif len(defect_decisions) != 1 or defect_decisions[0][0] is not None:
            raise ActWorkflowError('Необходимо внести решение КО по каждому дефекту.')
        for _defect, decision, _comment in defect_decisions:
            if decision not in Act.KoDecision.new_values():
                raise ActWorkflowError('Недопустимое решение КО.')

        from_status = act.status
        to_status = _get_required_status('TO_ANALYSIS')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        first_defect, first_decision, first_comment = defect_decisions[0]
        act.ko_decision = first_decision
        act.ko_comment = first_comment
        act.ko_decision_by = user
        act.ko_decision_at = timezone.now()
        act.status = to_status
        act.save(
            update_fields=[
                'ko_decision',
                'ko_comment',
                'ko_decision_by',
                'ko_decision_at',
                'status',
                'updated_at',
            ]
        )
        for defect, decision, comment in defect_decisions:
            if defect is not None:
                defect.ko_decision = decision
                defect.ko_comment = comment
                defect.ko_decision_by = user
                defect.ko_decision_at = act.ko_decision_at
                defect.save(update_fields=['ko_decision', 'ko_comment', 'ko_decision_by', 'ko_decision_at', 'updated_at'])
                message = f'Решение КО по дефекту «{defect.defect_type}»: {defect.get_ko_decision_display()}.'
            else:
                message = f'Решение КО внесено: {act.get_ko_decision_display()}.'
            add_act_history_event(act, user, ActHistoryEvent.EventType.KO_DECISION_APPLIED, message)
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.SENT_TO_TO,
            'Акт передан в ТО для анализа.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def return_to_otk(act, user, return_comment):
    return_comment = (return_comment or '').strip()
    if not return_comment:
        raise ActWorkflowError('Укажите комментарий к возврату.')
    with _workflow_logging('return_to_otk', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_return_to_otk(act, user):
            raise ActWorkflowError('Возврат акта в ОТК недоступен для вашей роли или текущего статуса.')
        _require_status(act, 'KO_REVIEW')
        add_act_comment(act, user, return_comment, notify=False)
        from_status = act.status
        to_status = _get_required_status('CREATED_OTK')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        act.status = to_status
        act.save(update_fields=['status', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.RETURNED_TO_OTK,
            'Акт возвращён в ОТК на доработку.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def apply_to_analysis(act, user, root_cause, action_summary):
    with _workflow_logging('apply_to_analysis', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_apply_to_analysis(act, user):
            raise ActWorkflowError('Анализ ТО недоступен для вашей роли или текущего статуса.')
        _require_status(act, 'TO_ANALYSIS')
        from_status = act.status
        to_status = _get_required_status('OTK_REVIEW')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        act.to_root_cause = root_cause
        act.to_action_summary = action_summary
        act.to_analysis_by = user
        act.to_analysis_at = timezone.now()
        act.status = to_status
        act.save(
            update_fields=[
                'to_root_cause',
                'to_action_summary',
                'to_analysis_by',
                'to_analysis_at',
                'status',
                'updated_at',
            ]
        )
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.TO_ANALYSIS_APPLIED,
            'Анализ ТО внесён, мероприятия ожидают дальнейшей проработки.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def apply_structured_to_analysis(act, user, analysis_data):
    with _workflow_logging(
        'apply_structured_to_analysis', act, user
    ) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_apply_to_analysis(act, user):
            raise ActWorkflowError('Анализ ТО недоступен для вашей роли или текущего статуса.')
        _require_status(act, 'TO_ANALYSIS')
        if not analysis_data or any(not item['actions'] for item in analysis_data):
            raise ActWorkflowError('Добавьте корневую причину и корректирующее мероприятие.')
        from_status = act.status
        to_status = _get_required_status('OTK_REVIEW')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        # Lock any previously submitted structure before replacing it.
        _lock_act_root_analyses(act)
        ActRootAnalysis.objects.filter(act=act).delete()
        for root_index, root_data in enumerate(analysis_data):
            root_analysis = ActRootAnalysis.objects.create(
                act=act,
                root_cause=root_data['root_cause'],
                display_order=root_index,
            )
            for action_index, action_data in enumerate(root_data['actions']):
                corrective_action = ActCorrectiveAction.objects.create(
                    root_analysis=root_analysis,
                    comment=action_data['comment'],
                    department=action_data['department'],
                    due_date=action_data['due_date'],
                    display_order=action_index,
                )
                ActCorrectiveActionAssignee.objects.bulk_create(
                    [
                        ActCorrectiveActionAssignee(corrective_action=corrective_action, user=assignee)
                        for assignee in action_data['assignees']
                    ]
                )
                from notifications.services import notify_action_assigned

                notify_action_assigned(corrective_action, user, action_data['assignees'])

        first_root = analysis_data[0]
        first_action = first_root['actions'][0]
        act.to_root_cause = first_root['root_cause']
        act.to_action_summary = first_action['comment']
        act.to_analysis_by = user
        act.to_analysis_at = timezone.now()
        act.status = to_status
        act.save(
            update_fields=[
                'to_root_cause',
                'to_action_summary',
                'to_analysis_by',
                'to_analysis_at',
                'status',
                'updated_at',
            ]
        )
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.TO_ANALYSIS_APPLIED,
            'Анализ ТО внесён, мероприятия ожидают дальнейшей проработки.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def return_to_ko(act, user, return_comment):
    return_comment = (return_comment or '').strip()
    if not return_comment:
        raise ActWorkflowError('Укажите комментарий к возврату.')
    with _workflow_logging('return_to_ko', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_return_to_ko(act, user):
            raise ActWorkflowError('Возврат акта в КО недоступен для вашей роли или текущего статуса.')
        _require_status(act, 'TO_ANALYSIS')
        from_status = act.status
        to_status = _get_required_status('KO_REVIEW')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        add_act_comment(act, user, return_comment, notify=False)
        act.status = to_status
        act.save(update_fields=['status', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.RETURNED_TO_KO,
            'Акт возвращён в КО на доработку.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def return_to_to(act, user, return_comment):
    return_comment = (return_comment or '').strip()
    if not return_comment:
        raise ActWorkflowError('Укажите комментарий к возврату.')
    with _workflow_logging('return_to_to', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_return_to_to(act, user):
            raise ActWorkflowError('Возврат акта в ТО недоступен для вашей роли или текущего статуса.')
        _require_status(act, 'OTK_REVIEW')
        from_status = act.status
        to_status = _get_required_status('TO_ANALYSIS')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        add_act_comment(act, user, return_comment, notify=False)
        act.status = to_status
        act.save(update_fields=['status', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.RETURNED_TO_TO,
            'Акт возвращён в ТО на доработку.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def approve_act(act, user):
    with _workflow_logging('approve_act', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_approve_act(act, user):
            raise ActWorkflowError('Утверждение акта недоступно для вашей роли или текущего статуса.')
        _require_status(act, 'OTK_REVIEW')
        # Re-read the structure under lock: departments, assignees and due dates
        # are validated against current data, not against a stale request copy.
        _root_ids, action_ids = _lock_act_root_analyses(act)
        corrective_actions = list(
            ActCorrectiveAction.objects.select_related('root_analysis', 'department').prefetch_related(
                'assignees__user__userprofile'
            ).filter(pk__in=action_ids).order_by('pk')
        )
        _validate_corrective_actions_for_approval(corrective_actions)
        from tasks.models import Task, TaskAssignee
        from references.models import TaskStatus

        if Task.objects.filter(source_action__in=corrective_actions).exists():
            raise ActWorkflowError('Для корректирующих мероприятий этого акта уже созданы задачи.')
        try:
            new_task_status = TaskStatus.objects.get(code='IN_PROGRESS', is_active=True)
        except TaskStatus.DoesNotExist as exc:
            raise ActWorkflowError('Не найден активный статус задачи «В работе».') from exc
        approval_date = timezone.localdate()
        for action in corrective_actions:
            if action.due_date < approval_date:
                raise ActWorkflowError('Срок корректирующего мероприятия не может быть раньше даты утверждения.')
        for action in corrective_actions:
            task = Task.objects.create(
                source_action=action,
                act=act,
                root_analysis=action.root_analysis,
                task_text=action.comment,
                department=action.department,
                due_date=action.due_date,
                created_by=user,
                status=new_task_status,
            )
            assignee_ids = [assignment.user_id for assignment in action.assignees.all()]
            TaskAssignee.objects.bulk_create(
                [TaskAssignee(task=task, user_id=user_id) for user_id in assignee_ids]
            )
            # Emitted only once the task and all its assignees exist, so the
            # event can never describe a half-built task.
            emit_task_created(task, assignee_ids)
            # Ids and a count: the task text is the corrective action's own
            # wording and the assignees are people, so neither is logged.
            log_event(
                logger,
                'INFO',
                'task.created',
                task_id=task.pk,
                act_id=act.pk,
                actor_user_id=_pk_of(user),
                assignee_count=len(assignee_ids),
                next_status=new_task_status.code,
                outcome='ok',
            )
        from_status = act.status
        to_status = _get_required_status('ARCHIVED')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        act.approved_by = user
        act.approved_at = timezone.now()
        act.status = to_status
        act.save(update_fields=['approved_by', 'approved_at', 'status', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.APPROVED,
            'Акт утверждён и перемещён в архив.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def _validate_corrective_actions_for_approval(corrective_actions):
    if not corrective_actions:
        raise ActWorkflowError('Для утверждения требуется хотя бы одно корректирующее мероприятие.')
    for action in corrective_actions:
        if not action.comment or not action.comment.strip():
            raise ActWorkflowError('Корректирующее мероприятие не заполнено.')
        if not action.department_id:
            raise ActWorkflowError('Для корректирующего мероприятия не выбран отдел.')
        assignees = list(action.assignees.all())
        if not assignees:
            raise ActWorkflowError('Для корректирующего мероприятия не выбран исполнитель.')
        if not action.due_date:
            raise ActWorkflowError('Для корректирующего мероприятия не указан срок.')
        for assignment in assignees:
            try:
                profile = assignment.user.userprofile
            except UserProfile.DoesNotExist:
                profile = None
            if not assignment.user.is_active or profile is None or not profile.is_active:
                raise ActWorkflowError('Исполнитель должен быть активен.')


def add_act_history_event(
    act,
    user,
    event_type,
    message,
    from_status=None,
    to_status=None,
    emit_notification=True,
):
    history_event = ActHistoryEvent.objects.create(
        act=act,
        user=user if getattr(user, 'is_authenticated', False) else None,
        event_type=event_type,
        message=message,
        from_status=from_status,
        to_status=to_status,
    )
    # Every workflow transition records its history here, on the already locked
    # act, so this is the one place `act.status_changed` is emitted: a rejected
    # or stale request raises before reaching it, and creation (no from_status)
    # is not a change.
    if from_status is not None and to_status is not None and from_status.pk != to_status.pk:
        emit_act_status_changed(act, history_event)
    # Editing records its own history event from inside the caller's locked,
    # atomic block, so this is likewise the one place `act.updated` is emitted.
    elif event_type == ActHistoryEvent.EventType.ACT_EDITED:
        emit_act_updated(act)
    if emit_notification:
        from notifications.services import notify_history_event

        notify_history_event(history_event)
    return history_event


def add_act_comment(act, user, text, notify=True):
    with transaction.atomic():
        comment = ActComment.objects.create(
            act=act,
            author=user if getattr(user, 'is_authenticated', False) else None,
            text=text,
        )
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.COMMENT_ADDED,
            'Комментарий добавлен пользователем.',
            emit_notification=False,
        )
        # Exactly one event per created comment, including a mandatory return
        # comment: `notify=False` only suppresses the in-app notification,
        # because the recipient gets the more specific return event instead.
        emit_comment_created(comment)
        if notify:
            from notifications.services import notify_comment_added

            notify_comment_added(comment, user)
    return comment


def _delete_attachment_file(
    storage,
    file_name,
    *,
    attachment_id,
    act_id,
    user_id,
    operation,
    failure_outcome,
):
    if not file_name:
        return
    try:
        storage.delete(file_name)
    except Exception as exc:  # noqa: BLE001 - storage cleanup is best-effort
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.storage_failed',
            attachment_id=attachment_id,
            act_id=act_id,
            user_id=user_id,
            operation=operation,
            error_type=type(exc).__name__,
            outcome=failure_outcome,
        )


def add_act_attachment(act, user, uploaded_file, description=''):
    if not can_add_attachment(act, user):
        raise ActWorkflowError('Добавление вложения недоступно для вашей роли.')

    attachment = ActAttachment(
        act=act,
        uploaded_by=user if getattr(user, 'is_authenticated', False) else None,
        original_name=uploaded_file.name,
        description=description,
        file_size=getattr(uploaded_file, 'size', 0) or 0,
        content_type=getattr(uploaded_file, 'content_type', '') or '',
    )
    file_written = False
    try:
        # Storage is not transactional; write first, then clean it if DB work fails.
        attachment.file.save(uploaded_file.name, uploaded_file, save=False)
        file_written = True
        with transaction.atomic():
            locked_act = Act.objects.select_for_update().get(pk=act.pk)
            if not can_add_attachment(locked_act, user):
                raise ActWorkflowError('Добавление вложения недоступно для вашей роли.')
            attachment.act = locked_act
            attachment.save()
            add_act_history_event(
                locked_act,
                user,
                ActHistoryEvent.EventType.ATTACHMENT_ADDED,
                f'Вложение добавлено: {attachment.original_name}.',
            )
    except Exception:
        if file_written:
            _delete_attachment_file(
                attachment.file.storage,
                attachment.file.name,
                attachment_id=getattr(attachment, 'pk', None),
                act_id=_pk_of(act),
                user_id=_pk_of(user),
                operation='upload_rollback',
                failure_outcome='orphan_cleanup_failed',
            )
        raise

    log_event(
        attachment_logger,
        'INFO',
        'attachment.uploaded',
        attachment_id=attachment.pk,
        act_id=_pk_of(act),
        user_id=_pk_of(user),
        size_bytes=attachment.file_size,
        operation='upload',
        outcome='ok',
    )
    return attachment


def delete_act_attachment(attachment, user):
    if not can_view_act(attachment.act, user) or not can_delete_attachment(attachment, user):
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.access_denied',
            attachment_id=_pk_of(attachment),
            act_id=getattr(attachment, 'act_id', None),
            user_id=_pk_of(user),
            operation='delete',
            outcome='denied',
        )
        raise ActWorkflowError('Удаление вложения недоступно для вашей роли.')

    act_id = attachment.act_id
    attachment_id = attachment.pk
    with transaction.atomic():
        # Fixed lock order: act first, then its attachment.
        locked_act = Act.objects.select_for_update().filter(pk=act_id).first()
        if locked_act is None:
            return False
        if not can_view_act(locked_act, user):
            raise ActWorkflowError('Удаление вложения недоступно для вашей роли.')

        locked_attachment = (
            ActAttachment.objects.select_for_update()
            .filter(pk=attachment_id, act_id=act_id)
            .first()
        )
        if locked_attachment is None:
            return False
        if not can_delete_attachment(locked_attachment, user):
            raise ActWorkflowError('Удаление вложения недоступно для вашей роли.')

        original_name = locked_attachment.original_name
        file_name = locked_attachment.file.name
        storage = locked_attachment.file.storage
        locked_attachment.delete()
        add_act_history_event(
            locked_act,
            user,
            ActHistoryEvent.EventType.ATTACHMENT_DELETED,
            f'Вложение удалено: {original_name}.',
        )
        transaction.on_commit(
            partial(
                _delete_attachment_file,
                storage,
                file_name,
                attachment_id=attachment_id,
                act_id=act_id,
                user_id=_pk_of(user),
                operation='delete_file',
                failure_outcome='orphaned_file',
            )
        )

    log_event(
        attachment_logger,
        'INFO',
        'attachment.deleted',
        attachment_id=attachment_id,
        act_id=act_id,
        user_id=_pk_of(user),
        operation='delete',
        outcome='ok',
    )
    return True


def clear_all_acts():
    """Delete every act and its database records, then remove attached files."""
    attachments = list(ActAttachment.objects.exclude(file='').only('file'))
    with transaction.atomic():
        deleted_count = Act.objects.count()
        # Approved acts have shared tasks whose foreign keys intentionally protect
        # their source act. The administrator cleanup is an explicit full reset,
        # so remove those dependent tasks before the acts themselves.
        from tasks.models import Task

        Task.objects.filter(act__isnull=False).delete()
        Act.objects.all().delete()
        # The administrator cleanup is an explicit full reset, so numbering
        # restarts too. Deleting a single act never touches the sequence.
        ActNumberSequence.objects.all().delete()
    for attachment in attachments:
        try:
            attachment.file.delete(save=False)
        except OSError:
            pass
    return deleted_count


def format_file_size(size_bytes):
    if size_bytes < 1024:
        return f'{size_bytes} Б'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} КБ'
    return f'{size_bytes / (1024 * 1024):.1f} МБ'


def validate_act_can_be_closed(act):
    if _status_code(act) != 'ACTIONS_ASSIGNED':
        raise ActWorkflowError('Акт можно закрыть только после назначения мероприятий.')
    required_fields = (
        (act.ko_decision, 'Перед закрытием нужно внести решение КО.'),
        (act.ko_decision_by_id, 'Перед закрытием должен быть указан автор решения КО.'),
        (act.ko_decision_at, 'Перед закрытием должна быть указана дата решения КО.'),
        (act.to_root_cause, 'Перед закрытием нужно заполнить корневую причину ТО.'),
        (act.to_action_summary, 'Перед закрытием нужно заполнить мероприятия ТО.'),
        (act.to_analysis_by_id, 'Перед закрытием должен быть указан автор анализа ТО.'),
        (act.to_analysis_at, 'Перед закрытием должна быть указана дата анализа ТО.'),
    )
    for value, message in required_fields:
        if not value:
            raise ActWorkflowError(message)


def close_act(act, user, closing_comment=''):
    with _workflow_logging('close_act', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_close_act(act, user):
            raise ActWorkflowError('Закрытие акта недоступно для вашей роли или текущего статуса.')
        validate_act_can_be_closed(act)
        from_status = act.status
        to_status = _get_required_status('CLOSED')
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(from_status)
        log_state['next_status'] = _status_code_of(to_status)
        act.status = to_status
        act.closed_by = user
        act.closed_at = timezone.now()
        act.closing_comment = closing_comment
        act.save(
            update_fields=[
                'status',
                'closed_by',
                'closed_at',
                'closing_comment',
                'updated_at',
            ]
        )
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.ACT_CLOSED,
            'Акт закрыт.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def get_available_act_actions(act, user):
    return {
        'edit_act': can_edit_act(act, user),
        'send_to_ko': can_send_to_ko(act, user),
        'ko_decision': can_apply_ko_decision(act, user),
        'return_to_otk': can_return_to_otk(act, user),
        'return_to_ko': can_return_to_ko(act, user),
        'return_to_to': can_return_to_to(act, user),
        'approve_act': can_approve_act(act, user),
        'to_analysis': can_apply_to_analysis(act, user),
        'close_act': can_close_act(act, user),
        'print_act': can_view_act(act, user),
    }


def get_visible_acts_for_user(user):
    return get_visible_acts_queryset(user)


def get_role_context_text(user):
    if is_act_admin(user):
        return (
            'Администратор: показаны все акты на всех этапах. Доступны все действия, '
            'разрешённые текущим статусом акта.'
        )
    role = get_user_role(user)
    if role == 'otk':
        return 'Показаны только акты, созданные вами и находящиеся на этапе ОТК.'
    if role == 'ko':
        return 'Показаны только акты, находящиеся на рассмотрении КО.'
    if role == 'to':
        return 'Показаны только акты, находящиеся на анализе ТО.'
    if role == 'manager':
        return 'Показаны все акты.'
    return 'Для пользователя без роли список актов недоступен.'


def _require_status(act, expected_code):
    actual_code = getattr(getattr(act, 'status', None), 'code', '')
    if actual_code != expected_code:
        raise ActWorkflowError('Акт находится в неподходящем статусе для этого действия.')


def _get_required_status(code):
    try:
        return get_act_status(code)
    except ValidationError as exc:
        message = exc.messages[0] if getattr(exc, 'messages', None) else str(exc)
        raise ActWorkflowError(message) from exc
