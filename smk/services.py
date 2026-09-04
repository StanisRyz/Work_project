"""The one way an СМК record — and the tasks it produces — is written.

Three entry points: `create_smk_source()` writes the record,
`update_smk_source()` corrects a live one, and `archive_smk_source()` files it
away — nothing else here changes an СМК record. Creation and correction each
write the record, its findings, its measures and the real `tasks.Task` rows
together or not at all, because a record whose measures reached nobody is
worse than no record. All three re-check the permission here, under the
transaction, and not only in the view.

A correction never edits a task, and it is *selective*: only the measures whose
task-relevant state really changed are replaced. Every мероприятие carries its
own primary key through the form, so `update_smk_source()` can compare each one
with itself:

* unchanged — the row and the live task(s) it already produced are kept
  untouched, and nobody is notified again;
* changed — the row is stamped `superseded_at`, its live tasks are cancelled
  with a reason, and a fresh row with fresh tasks and fresh notifications is
  written beside them;
* added — a new row and new tasks, exactly as creation writes them;
* removed — the row is superseded and its live tasks cancelled, with nothing
  put in their place.

«Task-relevant» is `_action_fingerprint()` and nothing else: wording, срок, the
set of исполнители, the split mode and the required-attachment flag. Editing
the audit's date, reordering rows or repointing a measure at another
несоответствие leaves the work alone, because none of it changes what somebody
was asked to do. A completed task is never rewritten — it stays on the
superseded row it was issued from, and the new state gets new tasks.

Findings carry no work, so they are corrected in place and only a removed one
is superseded; that also keeps a kept measure's «связано с несоответствием»
pointing at a row the record still reads.

`tasks.services.create_smk_action_task()` and `cancel_smk_action_tasks()` own
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


def _action_fingerprint(*, text, due_date, assignee_ids, split_for_assignees,
                        requires_attachment):
    """Everything about a мероприятие that decides what its task(s) say.

    The one definition of «изменилось», used for both sides of the comparison
    so a stored row and a submitted one can never be fingerprinted differently.
    Surrounding whitespace is stripped — retyping the same sentence with a
    trailing space is not a new instruction — and nothing else is interpreted:
    two wordings that mean the same thing are two different measures here, on
    purpose.

    Deliberately *not* included: the finding the measure answers, its position
    on the page and its department. None of them changes what the исполнитель
    was asked to do, and reissuing a task over one would be exactly the
    unnecessary churn this comparison exists to prevent.
    """
    return (
        (text or '').strip(),
        due_date,
        tuple(sorted(assignee_ids)),
        bool(split_for_assignees),
        bool(requires_attachment),
    )


def _stored_fingerprint(action):
    """The fingerprint of a мероприятие as the record currently holds it."""
    return _action_fingerprint(
        text=action.task_text,
        due_date=action.due_date,
        assignee_ids=[item.user_id for item in action.assignees.all()],
        split_for_assignees=action.split_for_assignees,
        requires_attachment=action.requires_attachment,
    )


def _submitted_fingerprint(item):
    """The fingerprint of a мероприятие as the form validated it."""
    return _action_fingerprint(
        text=item['text'],
        due_date=item['due_date'],
        assignee_ids=[user.pk for user in item['assignees']],
        split_for_assignees=item['split_for_assignees'],
        requires_attachment=item['requires_attachment'],
    )


def _create_action(source, item, index, findings, *, actor):
    """Store one мероприятие and issue the real task(s) it becomes.

    The single place a measure and its work are written, shared by creation and
    by correction so the two can never write them differently. `item` is one
    entry of the validated `actions` structure and `findings` the record's
    findings in submitted order — `item['non_conformity']` is a *position* in
    that list, because a newly added finding has no primary key until it is
    stored.

    One task or many, decided exactly as `protocols/services.py` decides it:
    the split flag arrives already normalized, so the length check is only a
    second lock on the rule that splitting a single исполнитель means nothing.

    Must be called inside an `atomic()` block. Returns `(action, tasks)`.
    """
    from notifications.services import notify_smk_task_assigned
    from tasks.services import TaskWorkflowError, create_smk_action_task

    position = item.get('non_conformity')
    action = SmkCorrectiveAction.objects.create(
        source=source,
        task_text=item['text'],
        department=item['department'],
        due_date=item['due_date'],
        non_conformity=findings[position] if position is not None else None,
        requires_attachment=item['requires_attachment'],
        split_for_assignees=item['split_for_assignees'],
        display_order=index,
    )
    assignees = item['assignees']
    SmkActionAssignee.objects.bulk_create(
        [SmkActionAssignee(action=action, user=user) for user in assignees]
    )
    if action.split_for_assignees and len(assignees) > 1:
        batches = [([user], user.pk) for user in assignees]
    else:
        batches = [(assignees, None)]
    tasks = []
    for users, individual_id in batches:
        try:
            task = create_smk_action_task(
                source,
                action,
                [user.pk for user in users],
                created_by=actor,
                individual_assignee_id=individual_id,
            )
        except TaskWorkflowError as exc:
            # Rolls the whole record back: half an audit record, with some
            # measures assigned and others silently lost, is not a state this
            # module ever leaves behind.
            raise SmkWorkflowError(str(exc)) from exc
        # After the task and its assignees exist, never before, exactly as
        # `protocols/services.py` does it: an СМК record raises no event of its
        # own, so this is the only thing that reaches an исполнитель — in the
        # bell and, through the same delivery queue, by email. One notification
        # per task, so a split исполнитель is told about their own task exactly
        # once and nobody is told twice.
        notify_smk_task_assigned(task, actor, users)
        _record(
            source,
            actor,
            SmkHistoryEvent.EventType.TASK_CREATED,
            f'По мероприятию №{index + 1} создана задача №{task.pk}.',
        )
        tasks.append(task)
    return action, tasks


def _write_content(source, non_conformities, actions, *, actor):
    """Write one *whole* set of findings, measures and real tasks onto a record.

    Creation only — a correction goes through `update_smk_source()`, which
    rewrites just the measures that changed. `actions` carries `non_conformity`
    as a *position* in `non_conformities`, exactly as `SmkSourceForm` produces
    it.

    Must be called inside an `atomic()` block; it opens none of its own, so a
    failure anywhere takes the record, its measures and their tasks with it.
    Returns the tasks it created, in the order of the measures.
    """
    # Created one by one rather than in bulk: a measure may point at a finding,
    # and `bulk_create` does not reliably return primary keys on every backend
    # this project runs on.
    findings = [
        SmkNonConformity.objects.create(
            source=source, text=item['text'], display_order=index,
        )
        for index, item in enumerate(non_conformities)
    ]
    tasks = []
    for index, item in enumerate(actions):
        _, created = _create_action(source, item, index, findings, actor=actor)
        tasks.extend(created)
    return tasks


def _sync_findings(source, submitted, *, superseded_at):
    """Bring the record's findings in line with the correction.

    A finding carries no work, so there is nothing to withdraw and nothing to
    reissue: a row the correction kept is corrected in place, a new one is
    stored, and only a row the correction dropped is stamped `superseded_at`.
    Editing in place is also what keeps a kept мероприятие's «связано с
    несоответствием» pointing at a row the record still reads.

    Returns the findings in submitted order, which is what a measure's
    `non_conformity` position indexes.
    """
    existing = {finding.pk: finding for finding in source.current_non_conformities}
    kept = set()
    findings = []
    for index, item in enumerate(submitted):
        finding = existing.get(item['id']) if item['id'] is not None else None
        # A repeated id is a forged or duplicated row, never identity: the
        # second occurrence is stored as the new finding it really is.
        if finding is None or finding.pk in kept:
            finding = SmkNonConformity.objects.create(
                source=source, text=item['text'], display_order=index,
            )
        else:
            kept.add(finding.pk)
            if finding.text != item['text'] or finding.display_order != index:
                finding.text = item['text']
                finding.display_order = index
                finding.save(update_fields=['text', 'display_order'])
        findings.append(finding)
    dropped = [pk for pk in existing if pk not in kept]
    if dropped:
        SmkNonConformity.objects.filter(pk__in=dropped).update(
            superseded_at=superseded_at,
        )
    return findings


def create_smk_source(*, origin, audit_date, non_conformities, actions, created_by):
    """Store one СМК record and turn every measure into a real task.

    `non_conformities` is a list of `{'id', 'text'}` dicts and `actions` a list
    of `{'id', 'text', 'department', 'due_date', 'non_conformity',
    'requires_attachment', 'split_for_assignees', 'assignees'}` dicts, whose
    `non_conformity` is a *position* in `non_conformities` or `None` — the
    findings have no primary keys until this function creates them — as
    `SmkSourceForm` produces them. The `id` of each is `None` here by
    construction: a record being created holds no rows yet, and identity only
    means something to `update_smk_source()`.

    Both are already validated — this function checks the *right* to write, not
    the shape of what is written, and a malformed structure is a programming
    error rather than a user error.
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
    """Correct one live СМК record, reissuing only the work that really changed.

    The arguments have exactly the shape `create_smk_source()` takes, because
    the correction goes through the same form and the same validated structure.
    What differs is that each мероприятие now carries the `id` of the row it
    came from, and that identity is what makes the correction *selective*:

    1. the findings are brought in line by `_sync_findings()` — corrected in
       place, added, or superseded when dropped. None of it touches a task;
    2. every submitted measure is compared with the row it claims to be, by
       `_action_fingerprint()`. Same fingerprint, and the row and its live
       task(s) are left exactly as they are: nothing is cancelled, nothing is
       created and nobody is notified again;
    3. a measure that changed, and one that was removed, is stamped
       `superseded_at` and its live tasks are cancelled by
       `tasks.services.cancel_smk_action_tasks()` with the reason a person
       reads on the task page. A task in a final status is left alone, because
       it really happened;
    4. a changed measure and an added one are then written by
       `_create_action()` as a fresh row with fresh task(s) and fresh
       notifications — one shared task, or one per исполнитель when the measure
       is split.

    So editing the audit date, a finding, the order of the rows or an unrelated
    measure leaves the untouched work exactly where it was, and only the people
    whose instruction actually changed hear about it again.

    All of it is one `atomic()` block over a `select_for_update()`d record, so a
    failure anywhere gives the cancelled tasks back and leaves the record
    exactly as it was. Archived records are refused — correcting a shelved
    document would reopen work that was filed away.
    """
    from tasks.services import TaskWorkflowError, cancel_smk_action_tasks

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

        superseded_at = timezone.now()
        findings = _sync_findings(
            locked, non_conformities, superseded_at=superseded_at,
        )

        # Read once, with the assignees the fingerprint needs, so the whole
        # comparison is one query rather than one per measure.
        existing = {
            action.pk: action
            for action in locked.current_actions.prefetch_related('assignees')
        }
        # `keep` rows are only repositioned; `create` rows are written fresh.
        # A measure claiming an id that is already spoken for is treated as a
        # new one — identity is never taken on trust.
        plan = []
        kept_ids = set()
        for index, item in enumerate(actions):
            current = existing.get(item['id']) if item['id'] is not None else None
            if (
                current is not None
                and current.pk not in kept_ids
                and _stored_fingerprint(current) == _submitted_fingerprint(item)
            ):
                kept_ids.add(current.pk)
                plan.append(('keep', index, item, current))
            else:
                plan.append(('create', index, item, None))
        replaced = [
            action for pk, action in existing.items() if pk not in kept_ids
        ]

        try:
            cancelled = cancel_smk_action_tasks(
                replaced,
                actor=actor,
                reason=(
                    f'Задача отменена: мероприятие изменено при редактировании '
                    f'{locked.label}.'
                ),
            )
        except TaskWorkflowError as exc:
            raise SmkWorkflowError(str(exc)) from exc
        if replaced:
            SmkCorrectiveAction.objects.filter(
                pk__in=[action.pk for action in replaced]
            ).update(superseded_at=superseded_at)

        locked.origin = origin
        locked.audit_date = audit_date
        locked.save(update_fields=['origin', 'audit_date', 'updated_at'])

        created = []
        for kind, index, item, current in plan:
            if kind == 'create':
                _, tasks = _create_action(
                    locked, item, index, findings, actor=actor,
                )
                created.extend(tasks)
                continue
            # A kept measure: only what does not reach its задача may have
            # moved — where it sits on the page, which finding it answers, and
            # the department its исполнители belong to.
            position = item.get('non_conformity')
            current.display_order = index
            current.non_conformity = findings[position] if position is not None else None
            current.department = item['department']
            current.save(
                update_fields=['display_order', 'non_conformity', 'department'],
            )

        # One event for the correction, naming all three halves, so the trail
        # says what was left alone as well as what was withdrawn and what
        # replaced it. The `TASK_CREATED` events `_create_action()` wrote sit
        # just above it in the same timeline.
        cancelled_labels = ', '.join(f'№{task.pk}' for task in cancelled) or '—'
        created_labels = ', '.join(f'№{task.pk}' for task in created) or '—'
        _record(
            locked,
            actor,
            SmkHistoryEvent.EventType.EDITED,
            f'{locked.label} отредактирована: несоответствий — '
            f'{len(non_conformities)}, корректирующих мероприятий — '
            f'{len(actions)}, из них без изменений — {len(kept_ids)}. '
            f'Отменённые задачи: {cancelled_labels}. '
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
        unchanged_action_count=len(kept_ids),
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
