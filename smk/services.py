"""The one way an СМК record — and the tasks it produces — is written.

Three entry points: `create_smk_source()` writes the record,
`update_smk_source()` corrects a live one, and `archive_smk_source()` files it
away — nothing else here changes an СМК record. Creation and correction each
write the record, its findings, its measures and the real `tasks.Task` rows
together or not at all, because a record whose measures reached nobody is
worse than no record. All three re-check the permission here, under the
transaction, and not only in the view.

A correction never edits a task, and never edits a measure either. The
findings and measures that were there are stamped `superseded_at`, the live
tasks they produced are cancelled with a reason, and a fresh set of findings,
measures and tasks is written in their place — so what was once asked of
somebody stays readable, and «неизменившееся мероприятие» still reaches its
исполнитель as a new task with a new notification. That is a deliberate
choice, not an optimisation gap: editing a task under the person holding it
would silently change work they may already have started.

`tasks.services.create_smk_action_task()` and `cancel_smk_source_tasks()` own
the tasks themselves; this module owns the decision to create or withdraw one,
and `notifications.services` is what tells the исполнитель about it. That split
is the same one `protocols/services.py` keeps, and it is why no second task or
notification system exists.
"""

import logging

from django.db import transaction
from django.utils import timezone

from ecosystem.logging_utils import log_event

from .models import (
    SmkActionAssignee,
    SmkCorrectiveAction,
    SmkHistoryEvent,
    SmkNonConformity,
    SmkSource,
)
from .permissions import (
    can_archive_smk_source,
    can_create_smk_task,
    can_edit_smk_source,
)


logger = logging.getLogger('ecosystem.workflow')


class SmkWorkflowError(Exception):
    pass


def _record(source, actor, event_type, message):
    """One history event.

    Always called inside the `atomic()` block of the change it describes, so a
    rolled-back write takes its event with it and the trail can never claim
    something that did not happen.
    """
    return SmkHistoryEvent.objects.create(
        source=source, actor=actor, event_type=event_type, message=message,
    )


def _write_content(source, non_conformities, actions, *, actor):
    """Write one *whole* set of findings, measures and real tasks onto a record.

    The single place an СМК record's content is stored, shared by creation and
    by correction so the two can never store it differently. `actions` carries
    `non_conformity` as a *position* in `non_conformities` — the findings have
    no primary keys until this function creates them — exactly as
    `SmkSourceForm` produces it.

    Must be called inside an `atomic()` block; it opens none of its own, so a
    failure anywhere takes the record, its measures and their tasks with it.
    Returns the tasks it created, in the order of the measures.
    """
    from notifications.services import notify_smk_task_assigned
    from tasks.services import TaskWorkflowError, create_smk_action_task

    # Created one by one rather than in bulk: a measure may point at a
    # finding, and `bulk_create` does not reliably return primary keys on
    # every backend this project runs on.
    findings = [
        SmkNonConformity.objects.create(
            source=source, text=text, display_order=index,
        )
        for index, text in enumerate(non_conformities)
    ]
    tasks = []
    for index, item in enumerate(actions):
        position = item.get('non_conformity')
        action = SmkCorrectiveAction.objects.create(
            source=source,
            task_text=item['text'],
            department=item['department'],
            due_date=item['due_date'],
            non_conformity=findings[position] if position is not None else None,
            requires_attachment=item['requires_attachment'],
            display_order=index,
        )
        SmkActionAssignee.objects.bulk_create(
            [
                SmkActionAssignee(action=action, user=user)
                for user in item['assignees']
            ]
        )
        try:
            task = create_smk_action_task(
                source,
                action,
                [user.pk for user in item['assignees']],
                created_by=actor,
            )
        except TaskWorkflowError as exc:
            # Rolls the whole record back: half an audit record, with some
            # measures assigned and others silently lost, is not a state
            # this module ever leaves behind.
            raise SmkWorkflowError(str(exc)) from exc
        # After the task and its assignees exist, never before, exactly as
        # `protocols/services.py` does it: an СМК record raises no event of
        # its own, so this is the only thing that reaches an исполнитель —
        # in the bell and, through the same delivery queue, by email. A
        # correction goes through here too, which is why an исполнитель whose
        # measure did not change is still told about the new task.
        notify_smk_task_assigned(task, actor, item['assignees'])
        _record(
            source,
            actor,
            SmkHistoryEvent.EventType.TASK_CREATED,
            f'По мероприятию №{index + 1} создана задача №{task.pk}.',
        )
        tasks.append(task)
    return tasks


def create_smk_source(*, origin, audit_date, non_conformities, actions, created_by):
    """Store one СМК record and turn every measure into a real task.

    `non_conformities` is a list of strings; `actions` a list of
    `{'text', 'department', 'due_date', 'non_conformity', 'requires_attachment',
    'assignees'}` dicts, whose `non_conformity` is a *position* in
    `non_conformities` or `None` — the findings have no primary keys until this
    function creates them — as
    `SmkSourceForm` produces them. Both are already validated — this function
    checks the *right* to write, not the shape of what is written, and a
    malformed structure is a programming error rather than a user error.
    """
    if not can_create_smk_task(created_by):
        log_event(
            logger,
            'INFO',
            'smk.operation_rejected',
            operation='create_source',
            actor_user_id=getattr(created_by, 'pk', None),
            reason='not_permitted',
            outcome='rejected',
        )
        raise SmkWorkflowError('Создание задачи СМК недоступно.')
    if not actions:
        raise SmkWorkflowError('Добавьте хотя бы одно корректирующее мероприятие.')

    with transaction.atomic():
        source = SmkSource.objects.create(
            origin=origin, audit_date=audit_date, created_by=created_by,
        )
        _write_content(source, non_conformities, actions, actor=created_by)
        # Last, so the trail's oldest event is «создана» even when several
        # rows share the same timestamp: `ordering` breaks a tie by `-pk`.
        _record(
            source,
            created_by,
            SmkHistoryEvent.EventType.CREATED,
            f'{source.label} создана: несоответствий — {len(non_conformities)}, '
            f'корректирующих мероприятий — {len(actions)}.',
        )
    log_event(
        logger,
        'INFO',
        'smk.source_created',
        smk_source_id=source.pk,
        origin=source.origin,
        actor_user_id=created_by.pk,
        non_conformity_count=len(non_conformities),
        action_count=len(actions),
        outcome='ok',
    )
    return source


def update_smk_source(source, *, origin, audit_date, non_conformities, actions, actor):
    """Correct one live СМК record, and reissue every task it produced.

    The arguments have exactly the shape `create_smk_source()` takes, because
    the correction goes through the same form and the same validated
    structure. What differs is what happens to the work already out there, and
    it is deliberately not «обновить»:

    1. every live task of the record is cancelled by
       `tasks.services.cancel_smk_source_tasks()`, with the reason a person
       reads on the task page — the rows stay, and a task already completed is
       left alone, because it really happened;
    2. the findings and measures that were there are stamped `superseded_at`
       rather than rewritten, so the cancelled tasks keep pointing at the exact
       wording they were given;
    3. a fresh set of findings, measures and tasks is written by
       `_write_content()`, which notifies every исполнитель through the common
       notification service.

    That means an unchanged measure still produces a new task and an unchanged
    исполнитель still gets a new notification. This is the point, not a
    side effect: the record was corrected as a whole, and the people holding
    the work have to be told what it now says.

    All of it is one `atomic()` block over a `select_for_update()`d record, so
    a failure anywhere gives the cancelled tasks back and leaves the record
    exactly as it was. Archived records are refused — correcting a shelved
    document would reopen work that was filed away.
    """
    from tasks.services import TaskWorkflowError, cancel_smk_source_tasks

    if not actions:
        raise SmkWorkflowError('Добавьте хотя бы одно корректирующее мероприятие.')

    with transaction.atomic():
        locked = SmkSource.objects.select_for_update().get(pk=source.pk)
        if not can_edit_smk_source(locked, actor):
            log_event(
                logger,
                'INFO',
                'smk.operation_rejected',
                operation='update_source',
                smk_source_id=locked.pk,
                actor_user_id=getattr(actor, 'pk', None),
                reason='archived' if locked.is_archived else 'not_permitted',
                outcome='rejected',
            )
            raise SmkWorkflowError('Редактирование записи СМК недоступно.')

        try:
            cancelled = cancel_smk_source_tasks(
                locked,
                actor=actor,
                reason=(
                    f'Задача отменена: {locked.label} отредактирована. '
                    'Вместо неё создана новая задача по актуальному мероприятию.'
                ),
            )
        except TaskWorkflowError as exc:
            raise SmkWorkflowError(str(exc)) from exc

        superseded_at = timezone.now()
        locked.current_non_conformities.update(superseded_at=superseded_at)
        locked.current_actions.update(superseded_at=superseded_at)

        locked.origin = origin
        locked.audit_date = audit_date
        locked.save(update_fields=['origin', 'audit_date', 'updated_at'])

        created = _write_content(locked, non_conformities, actions, actor=actor)

        # One event for the correction, naming both halves, so the trail says
        # which задачи went and which replaced them. The `TASK_CREATED` events
        # `_write_content()` wrote sit just above it in the same timeline.
        cancelled_labels = ', '.join(f'№{task.pk}' for task in cancelled) or '—'
        created_labels = ', '.join(f'№{task.pk}' for task in created) or '—'
        _record(
            locked,
            actor,
            SmkHistoryEvent.EventType.EDITED,
            f'{locked.label} отредактирована: несоответствий — '
            f'{len(non_conformities)}, корректирующих мероприятий — '
            f'{len(actions)}. Отменённые задачи: {cancelled_labels}. '
            f'Новые задачи: {created_labels}.',
        )
    log_event(
        logger,
        'INFO',
        'smk.source_updated',
        smk_source_id=locked.pk,
        origin=locked.origin,
        actor_user_id=actor.pk,
        non_conformity_count=len(non_conformities),
        action_count=len(actions),
        cancelled_task_count=len(cancelled),
        created_task_count=len(created),
        outcome='ok',
    )
    return locked


def archive_smk_source(source, *, actor):
    """Move one record to «Архив» — the only state change it has.

    Deliberately narrow. It touches `status`, `archived_at` and `archived_by`
    and nothing else: the findings, the measures, the tasks they became and
    every link between them stay exactly as they were, and an archived record
    is still opened and read at the same URL. Archiving is not a closure and
    never completes anything.

    The right is re-checked here, not only in the view, and the row is locked
    while it is read so two people pressing the button at once cannot both
    write the transition.
    """
    with transaction.atomic():
        locked = SmkSource.objects.select_for_update().get(pk=source.pk)
        if not can_archive_smk_source(locked, actor):
            log_event(
                logger,
                'INFO',
                'smk.operation_rejected',
                operation='archive_source',
                smk_source_id=locked.pk,
                actor_user_id=getattr(actor, 'pk', None),
                reason='already_archived' if locked.is_archived else 'not_permitted',
                outcome='rejected',
            )
            raise SmkWorkflowError('Архивирование записи СМК недоступно.')
        locked.status = SmkSource.Status.ARCHIVED
        locked.archived_at = timezone.now()
        locked.archived_by = actor
        locked.save(update_fields=['status', 'archived_at', 'archived_by', 'updated_at'])
        _record(
            locked,
            actor,
            SmkHistoryEvent.EventType.ARCHIVED,
            f'{locked.label} перенесена в архив.',
        )
    log_event(
        logger,
        'INFO',
        'smk.source_archived',
        smk_source_id=locked.pk,
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return locked
