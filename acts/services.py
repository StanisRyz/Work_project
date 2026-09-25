import logging
import time
from contextlib import contextmanager
from functools import partial

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import UserProfile
from ecosystem.attachments import format_file_size  # noqa: F401
from ecosystem.logging_utils import log_event
from realtime.emitters import (
    emit_act_status_changed,
    emit_act_updated,
    emit_comment_created,
)

from . import quality_impact
from .models import Act, ActAttachment, ActComment, ActCorrectiveAction, ActCorrectiveActionAssignee, ActDefect, ActHistoryEvent, ActRootAnalysis, get_act_status
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
    creator_is_eligible_otk,
    decidable_defects,
    is_act_admin,
    is_decided_this_round,
    KO_ROLES,
    ko_roles_for_act,
    ko_workshops,
    pending_ko_roles,
    get_user_role,
    get_visible_acts_queryset,
    is_ko,
    is_manager,
    is_otk,
    is_to,
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



# --------------------------------------------------------------------------
# The act's route, mirrored in «Задачи»
#
# Every transition below moves one routing task along with the act: the stage
# that is finished is closed, and the stage the act now waits on is opened for
# everyone who may act on it. Both happen inside the transition's own
# `atomic()` block, under the act row lock already taken, so the queue and the
# act can never disagree — a rolled-back transition leaves no task behind, and
# two concurrent transitions of one act cannot both open a stage.
#
# No task is created when the act is created: `CREATED_OTK` is the creator's
# own work and they already have the act.
# --------------------------------------------------------------------------


def _move_act_workflow_task(act, stage_name, user, *, reason):
    """Close the act's current routing task and open `stage_name`'s, if any.

    `stage_name` is a `Task.WorkflowStage` member name, or `None` at the end of
    the route. `tasks` is imported inside the function, as everywhere else in
    this module: `tasks.models` already imports `acts.models`.
    """
    from tasks.models import Task
    from tasks.services import (
        active_users_for_role,
        active_users_for_roles,
        move_act_workflow_task,
    )

    role_for_stage = {
        Task.WorkflowStage.KO_REVIEW: UserProfile.Role.KO,
        Task.WorkflowStage.TO_ANALYSIS: UserProfile.Role.TO,
        Task.WorkflowStage.OTK_REVIEW: UserProfile.Role.OTK,
        Task.WorkflowStage.OTK_REWORK: UserProfile.Role.OTK,
    }
    stage = getattr(Task.WorkflowStage, stage_name) if stage_name else None
    if stage is None:
        assignees = []
    elif stage == Task.WorkflowStage.OTK_REWORK:
        # A return lands on the author's own `CREATED_OTK` act, and only the
        # author may send it on again — so the rework queue entry is theirs,
        # not the whole department's. An author who lost the role or the
        # account falls back to ОТК at large, so the act is never stranded.
        # `creator_is_eligible_otk()` is the one statement of that rule, shared
        # with the permissions and the notification routing.
        if creator_is_eligible_otk(act):
            assignees = [act.created_by]
        else:
            assignees = active_users_for_role(UserProfile.Role.OTK)
    elif stage == Task.WorkflowStage.KO_REVIEW:
        # КО is per workshop: the entry goes to the КО of every цех the act
        # has defects in (and to the retired general КО, who decides all).
        assignees = active_users_for_roles(ko_roles_for_act(act))
    else:
        assignees = active_users_for_role(role_for_stage[stage])
    return move_act_workflow_task(act, stage, assignees, created_by=user, reason=reason)


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
        # A new КО round: decisions from an earlier one no longer count.
        act.ko_round += 1
        act.save(update_fields=['status', 'ko_round', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.SENT_TO_KO,
            'Акт передан в КО для рассмотрения.',
            from_status=from_status,
            to_status=to_status,
        )
        _move_act_workflow_task(
            act, 'KO_REVIEW', user, reason='sent_to_ko'
        )
    return act


def apply_ko_decision(act, user, defect_decisions):
    """Внести решения КО по дефектам своего цеха; когда решены все — передать в ТО.

    `defect_decisions` — последовательность кортежей
    `(дефект, решение, комментарий, анализ)`, где `анализ` — словарь значений
    «Анализа влияния отклонений на качество изделия» (`acts/quality_impact.py`);
    пустой словарь означает незаполненный анализ и допустим ровно там, где
    правило его не требует.

    КО разделён по цехам (`acts.permissions.WORKSHOP_KO_ROLES`): каждый вносит
    решения ровно по тем дефектам, которые ему принадлежат
    (`decidable_defects()`), — не больше и не меньше. Руководитель,
    администратор и прежняя общая роль КО решают все дефекты сразу, как и
    раньше. Решение помечается раундом КО акта (`Act.ko_round`), и акт уходит
    в ТО в той же транзакции, как только у каждого дефекта есть решение этого
    раунда; до того он остаётся на рассмотрении КО, а задача этапа — за теми,
    чьи дефекты ещё не решены.

    Анализ принадлежит дефекту, поэтому на устаревшем пути «решение на уровне
    акта» (`дефект is None`, акт без дефектов) он не хранится и не требуется.
    """
    with _workflow_logging('apply_ko_decision', act, user) as log_state, transaction.atomic():
        act = lock_act_for_update(act)
        if not can_apply_ko_decision(act, user):
            raise ActWorkflowError('Решение КО недоступно для вашей роли или текущего статуса.')
        _require_status(act, 'KO_REVIEW')
        if not act.ko_round:
            # An act that entered КО before rounds were counted.
            act.ko_round = 1
        defect_decisions = list(defect_decisions)
        # Re-read the act's defects under lock and match submitted decisions by
        # primary key, so a decision aimed at another act's defect — or at a
        # defect deleted meanwhile — is rejected instead of silently applied.
        current_defects = {defect.pk: defect for defect in _lock_act_defects(act)}
        if current_defects:
            received_ids = [
                defect.pk if defect is not None else None
                for defect, _d, _c, _i in defect_decisions
            ]
            # Exactly the defects this user decides: a workshop КО answers for
            # every defect of their цех, and for nothing else.
            owned_ids = {defect.pk for defect in decidable_defects(act, user)}
            if (
                not owned_ids
                or None in received_ids
                or len(received_ids) != len(set(received_ids))
                or set(received_ids) != owned_ids
                or not owned_ids <= set(current_defects)
            ):
                raise ActWorkflowError('Необходимо внести решение КО по каждому своему дефекту.')
            # Replace any stale instance the caller passed with the locked one.
            defect_decisions = [
                (current_defects[defect.pk], decision, comment, impact)
                for defect, decision, comment, impact in defect_decisions
            ]
        elif len(defect_decisions) != 1 or defect_decisions[0][0] is not None:
            raise ActWorkflowError('Необходимо внести решение КО по каждому дефекту.')
        for _defect, decision, _comment, _impact in defect_decisions:
            if decision not in Act.KoDecision.new_values():
                raise ActWorkflowError('Недопустимое решение КО.')
        # «Анализ влияния отклонений» — после проверки самих решений и до любой
        # записи, так что отказ не оставляет половины внесённых решений. Правило
        # спрашивается здесь, под блокировкой строки акта: форма задаёт тот же
        # вопрос, чтобы ошибка легла на поле, но авторитет — этот вызов, и
        # запрос в обход страницы получает тот же отказ.
        defect_decisions = _validated_quality_impacts(defect_decisions)

        decided_at = timezone.now()
        for defect, decision, comment, impact in defect_decisions:
            if defect is not None:
                defect.ko_decision = decision
                defect.ko_comment = comment
                defect.ko_decision_by = user
                defect.ko_decision_at = decided_at
                defect.ko_round = act.ko_round
                for name, value in impact.items():
                    setattr(defect, name, value)
                defect.save(update_fields=[
                    'ko_decision', 'ko_comment', 'ko_decision_by', 'ko_decision_at',
                    'ko_round', *quality_impact.FIELDS, 'updated_at',
                ])
                message = f'Решение КО по дефекту «{defect.defect_type}»: {defect.get_ko_decision_display()}.'
                checked = len(quality_impact.describe(impact))
                if checked:
                    message = f'{message} Анализ влияния отклонений: пунктов — {checked}.'
            else:
                message = f'Решение КО внесено: {Act.KoDecision(decision).label}.'
            add_act_history_event(act, user, ActHistoryEvent.EventType.KO_DECISION_APPLIED, message)

        # Every defect of the act, as the database now holds them, in its order.
        all_defects = sorted(current_defects.values(), key=lambda item: (item.created_at, item.pk))
        waiting = [defect for defect in all_defects if not is_decided_this_round(defect, act)]
        log_state['act_id'] = act.pk
        log_state['previous_status'] = _status_code_of(act.status)
        if waiting:
            # Another workshop's КО still owes a decision: the act stays here,
            # and the stage entry keeps only the people it is still waiting on.
            act.save(update_fields=['ko_round', 'updated_at'])
            log_state['next_status'] = _status_code_of(act.status)
            _narrow_ko_workflow_task(act, user)
            # The other КО looking at the act see the decision appear.
            emit_act_updated(act)
            return act

        from_status = act.status
        to_status = _get_required_status('TO_ANALYSIS')
        log_state['next_status'] = _status_code_of(to_status)
        if all_defects:
            first = all_defects[0]
            act.ko_decision, act.ko_comment = first.ko_decision, first.ko_comment
        else:
            _none, act.ko_decision, act.ko_comment, _impact = defect_decisions[0]
        act.ko_decision_by = user
        act.ko_decision_at = decided_at
        act.status = to_status
        act.save(
            update_fields=[
                'ko_decision',
                'ko_comment',
                'ko_decision_by',
                'ko_decision_at',
                'ko_round',
                'status',
                'updated_at',
            ]
        )
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.SENT_TO_TO,
            'Акт передан в ТО для анализа.',
            from_status=from_status,
            to_status=to_status,
        )
        _move_act_workflow_task(
            act, 'TO_ANALYSIS', user, reason='ko_decision_applied'
        )
        _ensure_rejection_task(
            act,
            [(defect, defect.ko_decision, defect.ko_comment, {}) for defect in all_defects]
            or defect_decisions,
            user,
        )
    return act


def _narrow_ko_workflow_task(act, user):
    """Keep the open КО stage entry on the people whose defects still wait.

    The КО who has just decided their share is taken off it — their part is
    done — and nobody is ever left without work: if nobody would remain, the
    entry is left as it is.
    """
    from tasks.models import Task
    from tasks.services import active_users_for_roles, replace_task_assignees

    task = (
        Task.objects.filter(
            act=act,
            source_type=Task.SourceType.ACT_WORKFLOW,
            workflow_stage=Task.WorkflowStage.KO_REVIEW,
            status__code='IN_PROGRESS',
        )
        .order_by('-pk')
        .first()
    )
    if task is None:
        return
    remaining = active_users_for_roles(pending_ko_roles(act))
    if remaining:
        replace_task_assignees(task, remaining, actor=user)


def _validated_quality_impacts(defect_decisions):
    """Привести анализ каждого дефекта к записываемому виду или отказать.

    Возвращает те же кортежи с нормализованным анализом. Нормализация и
    проверка — обе из `acts/quality_impact.py`, чтобы правило существовало в
    одном месте; здесь только обход дефектов и превращение ошибки в
    `ActWorkflowError` с именем дефекта, потому что решений в одном переходе
    несколько и «где именно» — половина сообщения.

    Дефект `None` пропускается: анализ хранится на дефекте, и хранить его
    негде.
    """
    validated = []
    for defect, decision, comment, impact in defect_decisions:
        if defect is None:
            validated.append((defect, decision, comment, {}))
            continue
        values = quality_impact.normalize(decision, impact or {})
        errors = quality_impact.validate(decision, values)
        if errors:
            raise ActWorkflowError(
                f'Дефект «{defect.defect_type}»: {next(iter(errors.values()))}'
            )
        validated.append((defect, decision, comment, values))
    return validated


def _ensure_rejection_task(act, defect_decisions, user):
    """One ПДО task for the «Цех МП» products this КО decision prohibited.

    Read from the locked defect rows *after* they were saved, so the task
    describes the decisions the database actually holds rather than the ones
    the request proposed. A defect qualifies on two facts together — the
    workshop is «Цех МП» and the decision is «запретить использование» — so a
    ПиР defect never produces one, and neither does any permitting decision.
    Defect order is the act's own, and quantities are never summed: one
    sentence per defect.

    Inside the transition's `atomic()` block and under the act row lock, so a
    rolled-back КО decision takes the task with it. Missing ПДО recipients are
    not an error and never block the act; an unexpected database failure is
    left to propagate.
    """
    from tasks.services import ensure_act_rejection_task

    rejected = [
        defect
        for defect, _decision, _comment, _impact in defect_decisions
        if defect is not None
        and defect.workshop == ActDefect.Workshop.MP_SHOP
        and defect.ko_decision == Act.KoDecision.PROHIBIT_USE
    ]
    return ensure_act_rejection_task(act, rejected, created_by=user)


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
        _move_act_workflow_task(
            act, 'OTK_REWORK', user, reason='returned_to_otk'
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
        _move_act_workflow_task(
            act, 'OTK_REVIEW', user, reason='to_analysis_applied'
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
                    # Stored normalized: splitting a corrective action between
                    # one person has no meaning, so a single assignee always
                    # stores False whatever the checkbox posted. This is the
                    # single write point for the flag, which is why the
                    # browser's matching behaviour can stay presentation.
                    # `.get()`: the flag was added after this structure was, so
                    # a caller that does not mention it means the shared task
                    # every corrective action produced before.
                    split_for_assignees=(
                        action_data.get('split_for_assignees', False)
                        and len(action_data['assignees']) > 1
                    ),
                    # Stored as answered, with no normalization: unlike
                    # splitting, a required attachment means the same for one
                    # исполнитель as for five. `.get()` for the same reason as
                    # above — a caller that omits it means the unrestricted
                    # task every corrective action produced before.
                    requires_attachment=action_data.get('requires_attachment', False),
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
        _move_act_workflow_task(
            act, 'OTK_REVIEW', user, reason='to_analysis_applied'
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
        # A new КО round: every defect is decided again.
        act.ko_round += 1
        act.save(update_fields=['status', 'ko_round', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.RETURNED_TO_KO,
            'Акт возвращён в КО на доработку.',
            from_status=from_status,
            to_status=to_status,
        )
        _move_act_workflow_task(
            act, 'KO_REVIEW', user, reason='returned_to_ko'
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
        _move_act_workflow_task(
            act, 'TO_ANALYSIS', user, reason='returned_to_to'
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
        from tasks.models import Task
        from tasks.services import TaskWorkflowError, create_act_action_task

        # The two unique constraints on `Task` already make a second task for
        # the same corrective action — or for the same person within it —
        # impossible; this turns the resulting IntegrityError into a controlled
        # refusal, and it is inside the approval transaction, so a partial set
        # of tasks can never outlive a failed approval.
        if Task.objects.filter(source_action__in=corrective_actions).exists():
            raise ActWorkflowError('Для корректирующих мероприятий этого акта уже созданы задачи.')
        approval_date = timezone.localdate()
        for action in corrective_actions:
            if action.due_date < approval_date:
                raise ActWorkflowError('Срок корректирующего мероприятия не может быть раньше даты утверждения.')
        for action in corrective_actions:
            assignments = list(action.assignees.all())
            # One corrective action, one or many tasks. `split_for_assignees`
            # is stored already normalized, so the length check is only a
            # second lock on the rule that splitting a single assignee means
            # nothing. Everything else about the task — the act, the root
            # analysis, the wording, the department, the deadline — is read
            # from the corrective action by the tasks service.
            if action.split_for_assignees and len(assignments) > 1:
                batches = [[assignment.user_id] for assignment in assignments]
                individuals = [assignment.user_id for assignment in assignments]
            else:
                batches = [[assignment.user_id for assignment in assignments]]
                individuals = [None]
            for assignee_ids, individual_id in zip(batches, individuals):
                try:
                    create_act_action_task(
                        action,
                        assignee_ids,
                        created_by=user,
                        individual_assignee_id=individual_id,
                    )
                except TaskWorkflowError as exc:
                    # A refusal from the tasks service is an act workflow
                    # refusal here: the surrounding transaction rolls the whole
                    # approval back, siblings included.
                    raise ActWorkflowError(str(exc)) from exc
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
        # End of the route: the stage is closed and no new one opens.
        _move_act_workflow_task(act, None, user, reason='approved')
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


# The role a step of the route is taken in. Transitions name it by their
# event; a comment, a file or an edit belongs to the stage the act is in.
_EVENT_ROLES = {
    ActHistoryEvent.EventType.CREATED: UserProfile.Role.OTK,
    ActHistoryEvent.EventType.SENT_TO_KO: UserProfile.Role.OTK,
    ActHistoryEvent.EventType.ACT_EDITED: UserProfile.Role.OTK,
    ActHistoryEvent.EventType.RETURNED_TO_TO: UserProfile.Role.OTK,
    ActHistoryEvent.EventType.APPROVED: UserProfile.Role.OTK,
    ActHistoryEvent.EventType.KO_DECISION_APPLIED: UserProfile.Role.KO,
    ActHistoryEvent.EventType.SENT_TO_TO: UserProfile.Role.KO,
    ActHistoryEvent.EventType.RETURNED_TO_OTK: UserProfile.Role.KO,
    ActHistoryEvent.EventType.TO_ANALYSIS_APPLIED: UserProfile.Role.TO,
    ActHistoryEvent.EventType.RETURNED_TO_KO: UserProfile.Role.TO,
    ActHistoryEvent.EventType.ACT_CLOSED: UserProfile.Role.TO,
}
_STATUS_ROLES = {
    'CREATED_OTK': UserProfile.Role.OTK,
    'OTK_REVIEW': UserProfile.Role.OTK,
    'KO_REVIEW': UserProfile.Role.KO,
    'TO_ANALYSIS': UserProfile.Role.TO,
    'ACTIONS_ASSIGNED': UserProfile.Role.TO,
}


def describe_acting_substitution(act, user, event_type, from_status=None):
    """«замещает Иванова И. И.», if `user` took this step in a lent role.

    Empty when the user holds the needed role themselves, and for a
    руководитель or администратор — they act on every stage in their own
    right. The text is stored on the event, so it survives the substitution.
    """
    from accounts.roles import substitution_for
    from accounts.templatetags.people import person_name

    if not getattr(user, 'is_authenticated', False):
        return ''
    own_role = get_user_role(user)
    if is_act_admin(user) or own_role == UserProfile.Role.MANAGER:
        return ''
    role = _EVENT_ROLES.get(event_type)
    if role is None:
        status = from_status or getattr(act, 'status', None)
        role = _STATUS_ROLES.get(getattr(status, 'code', ''))
    if role is None:
        return ''
    # КО is any of the КО roles — the general one or a workshop one.
    needed = KO_ROLES if role == UserProfile.Role.KO else (role,)
    if own_role in needed:
        return ''
    # The stage's own role first; a lent «Руководитель» opens every stage too.
    substitution = next(
        (found for found in (substitution_for(user, item) for item in needed) if found),
        None,
    ) or substitution_for(user, UserProfile.Role.MANAGER)
    if substitution is None:
        return ''
    if substitution.substitutes_for_id:
        return f'замещает {person_name(substitution.substitutes_for)}'[:200]
    return f'по замещению: {substitution.role_label}'[:200]


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
        substitution_note=describe_acting_substitution(act, user, event_type, from_status),
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

        Task.objects.filter(
            source_type__in=[
                Task.SourceType.ACT,
                Task.SourceType.ACT_WORKFLOW,
                Task.SourceType.ACT_REJECTION,
            ]
        ).delete()
        Act.objects.all().delete()
    for attachment in attachments:
        try:
            attachment.file.delete(save=False)
        except OSError:
            pass
    return deleted_count


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
    if is_manager(user):
        return 'Показаны все акты.'
    # Several at once when a substitution lends a role: the queue is their union.
    parts = []
    if is_otk(user):
        parts.append(
            'созданные вами акты на этапе ОТК и все акты, ожидающие итоговой проверки ОТК'
        )
    if is_ko(user):
        parts.append(
            'только акты, находящиеся на рассмотрении КО'
            if ko_workshops(user) is None
            else 'только акты на рассмотрении КО с дефектами вашего цеха'
        )
    if is_to(user):
        parts.append('только акты, находящиеся на анализе ТО')
    if len(parts) == 1:
        return f'Показаны {parts[0]}.'
    if parts:
        return 'Показаны ' + '; '.join(part.removeprefix('только ') for part in parts) + '.'
    if get_user_role(user) == UserProfile.Role.MAS:
        return 'Акты доступны для чтения во вкладках «Все акты» и «Архив».'
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
