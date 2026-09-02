"""The only place a protocol is created, numbered or deleted.

Two rules shape this file:

* **No signals.** A number is a business decision, not a side effect of a
  `save()`; a fixture load, a data migration or a technical save must never
  consume one. Every mutation is an explicit call here.
* **Allocation is serialized on the protocol type.** `create_protocol()` opens
  a transaction, row-locks the `ProtocolType`, and only then looks for a free
  number. Two concurrent creations of the same type queue behind that lock, so
  they cannot pick the same number; two creations of *different* types never
  block each other. The `unique_protocol_number_per_type` constraint stays the
  final integrity guarantee and is not the allocator.
"""

import logging
from functools import partial

from django.db import transaction
from django.utils import timezone

from ecosystem.logging_utils import log_event
from ecosystem.workdays import add_working_days
from realtime.emitters import (
    emit_protocol_approval_changed,
    emit_protocol_created,
    emit_protocol_deleted,
    emit_protocol_status_changed,
    emit_protocol_updated,
)

from .models import (
    Protocol,
    ProtocolAction,
    ProtocolActionAssignee,
    ProtocolAgendaItem,
    ProtocolApproval,
    ProtocolAttachment,
    ProtocolComment,
    ProtocolHistoryEvent,
    ProtocolParticipant,
    ProtocolSpeech,
    ProtocolType,
)
from .permissions import (
    can_add_protocol_attachment,
    can_delete_draft_protocol,
    can_delete_protocol_attachment,
    can_edit_protocol,
    can_view_protocol,
)


# The same channel act attachments log to: one place to watch for refused
# downloads and orphaned files, whatever the file was attached to.
attachment_logger = logging.getLogger('ecosystem.attachments')


class ProtocolWorkflowError(Exception):
    """A refused operation: a lost race, a stale page, a missing right."""


def allocate_protocol_number(protocol_type):
    """Smallest free positive number for this type — not `max + 1`.

    Numbers are reusable: deleting a draft frees its number, and with `1, 2, 4,
    5` taken the answer is `3`. The caller must already hold the row lock on
    `protocol_type`; on its own this function is a read and guarantees nothing.
    """
    used = (
        Protocol.objects.filter(protocol_type=protocol_type)
        .order_by('number')
        .values_list('number', flat=True)
    )
    expected = 1
    for number in used.iterator():
        if number < expected:
            # Only reachable for a hand-written row outside the allocator.
            continue
        if number > expected:
            break
        expected += 1
    return expected


def build_participant_snapshot(user, department=None):
    """Freeze the document-facing identity of a user at this moment.

    An archived protocol must keep saying who took part and in which role, so
    these three strings are copied once and never follow the profile again.
    """
    profile = getattr(user, 'userprofile', None)
    display_name = user.get_full_name() or user.get_username()
    selected_department = department if department is not None else getattr(profile, 'department', None)
    return {
        'department': selected_department,
        'display_name': display_name[:180],
        'position': (getattr(profile, 'position', '') or '')[:120],
        'department_name': (getattr(selected_department, 'name', '') or '')[:120],
    }


def add_participant(protocol, user, department=None, requires_approval=False, display_order=None):
    """Add one participant with the snapshot taken now.

    Refuses a duplicate before the unique constraint does, so a repeated form
    submission gets a controlled error instead of an `IntegrityError`.
    """
    if protocol.participants.filter(user=user).exists():
        raise ProtocolWorkflowError('Этот пользователь уже участвует в протоколе.')
    if display_order is None:
        display_order = protocol.participants.count()
    return ProtocolParticipant.objects.create(
        protocol=protocol,
        user=user,
        requires_approval=requires_approval,
        display_order=display_order,
        **build_participant_snapshot(user, department),
    )


def add_speech(protocol, speaker, text, display_order=None):
    """Record one «Слушали» entry, spoken by a participant of *this* protocol."""
    if speaker.protocol_id != protocol.pk:
        raise ProtocolWorkflowError('Выступающий должен быть участником этого протокола.')
    if display_order is None:
        display_order = protocol.speeches.count()
    return ProtocolSpeech.objects.create(
        protocol=protocol,
        speaker=speaker,
        text=text,
        display_order=display_order,
    )


@transaction.atomic
def create_protocol(protocol_type, author):
    """Create a draft protocol, its author participant and its CREATED event.

    All four steps — lock, allocate, create, record — are one transaction, so a
    protocol never exists without the participant row that represents its
    author or without the history event that opens its audit trail.
    """
    locked_type = ProtocolType.objects.select_for_update().get(pk=protocol_type.pk)
    protocol = Protocol.objects.create(
        protocol_type=locked_type,
        number=allocate_protocol_number(locked_type),
        author=author,
        status=Protocol.Status.DRAFT,
        revision=0,
    )
    add_participant(protocol, author, requires_approval=False, display_order=0)
    ProtocolHistoryEvent.objects.create(
        protocol=protocol,
        actor=author,
        event_type=ProtocolHistoryEvent.EventType.CREATED,
        revision=protocol.revision,
        message=f'Протокол «{locked_type.name} №{protocol.number}» создан.',
    )
    # Inside the atomic block, after the author participant and the history
    # event exist: `publish_after_commit` keeps a rolled-back creation silent.
    emit_protocol_created(protocol)
    return protocol


@transaction.atomic
def delete_draft_protocol(protocol, user):
    """Delete an own draft, releasing its number for the next protocol.

    The protocol row is re-loaded under a lock and re-checked afterwards: the
    status may have moved on in another tab since the page was rendered.
    """
    locked = Protocol.objects.select_for_update().filter(pk=protocol.pk).first()
    if locked is None:
        raise ProtocolWorkflowError('Протокол уже удалён.')
    if not can_delete_draft_protocol(locked, user):
        raise ProtocolWorkflowError('Удалить черновик может только его автор.')
    # Speeches protect their participant, and the participants are about to be
    # cascaded away with the protocol, so they go first — the same fixed order
    # the rest of the project uses for dependent rows.
    locked.speeches.all().delete()
    # The identifier has to be read before the row goes: after `delete()`
    # Django clears the primary key on the instance.
    deleted_id = locked.pk
    locked.delete()
    emit_protocol_deleted(deleted_id)


def _document_snapshot(protocol):
    """A comparable picture of everything the editor can change.

    Compared before and after a save so one edit produces at most one `EDITED`
    event and an unchanged submission produces none — history records that the
    protocol was edited, not which field moved.
    """
    return {
        'participants': [
            (p.user_id, p.department_id, p.requires_approval, p.display_order)
            for p in protocol.participants.all()
        ],
        'agenda': [(item.text, item.display_order) for item in protocol.agenda_items.all()],
        'speeches': [
            (speech.speaker.user_id, speech.text, speech.display_order)
            for speech in protocol.speeches.select_related('speaker')
        ],
        # Every persisted column of a decision the editor can move, execution
        # flags included: they are the author's own answers, they survive a
        # return for revision, and they decide what the real task becomes — so
        # a submission that changes nothing but one of them is still an edit.
        'actions': [
            (
                action.task_text,
                action.department_id,
                action.due_date,
                action.split_for_assignees,
                action.requires_attachment,
                action.display_order,
                sorted(assignee.user_id for assignee in action.assignees.all()),
            )
            for action in protocol.actions.prefetch_related('assignees')
        ],
    }


def _apply_participants(protocol, participants):
    """Reconcile the participant rows, keeping the snapshots that already exist.

    A participant who stays keeps the `display_name`/`position` frozen when they
    were added — saving an unrelated block must not quietly refresh them from
    the profile. Only a genuinely new (or re-added) row gets a new snapshot, and
    only a changed department re-freezes `department_name` with it.
    """
    author_participant = protocol.participants.filter(user_id=protocol.author_id).first()
    if author_participant is None:
        # Impossible through `create_protocol()`; refuse rather than invent one.
        raise ProtocolWorkflowError('Протокол повреждён: автор не является участником.')
    if author_participant.display_order != 0 or author_participant.requires_approval:
        author_participant.display_order = 0
        author_participant.requires_approval = False
        author_participant.save(update_fields=['display_order', 'requires_approval'])

    existing = {
        participant.user_id: participant
        for participant in protocol.participants.all()
        if participant.user_id != protocol.author_id
    }
    keep = set()
    for order, item in enumerate(participants, start=1):
        user = item['user']
        participant = existing.get(user.pk)
        if participant is None:
            ProtocolParticipant.objects.create(
                protocol=protocol,
                user=user,
                requires_approval=item['requires_approval'],
                display_order=order,
                **build_participant_snapshot(user, item['department']),
            )
            continue
        keep.add(user.pk)
        updates = []
        if participant.department_id != getattr(item['department'], 'pk', None):
            participant.department = item['department']
            participant.department_name = (getattr(item['department'], 'name', '') or '')[:120]
            updates += ['department', 'department_name']
        if participant.requires_approval != item['requires_approval']:
            participant.requires_approval = item['requires_approval']
            updates.append('requires_approval')
        if participant.display_order != order:
            participant.display_order = order
            updates.append('display_order')
        if updates:
            participant.save(update_fields=updates)
    for user_id, participant in existing.items():
        if user_id not in keep:
            participant.delete()


def _apply_speeches(protocol, speeches):
    """Rewrite «Слушали», resolving each speaker to a participant of this protocol."""
    participants = {
        participant.user_id: participant for participant in protocol.participants.all()
    }
    for order, speech in enumerate(speeches):
        speaker = participants.get(speech['speaker_user'].pk)
        if speaker is None:
            # The form already refuses this; the service refuses it again so the
            # rule holds for any future caller.
            raise ProtocolWorkflowError('Выступающий должен быть участником этого протокола.')
        ProtocolSpeech.objects.create(
            protocol=protocol,
            speaker=speaker,
            text=speech['text'],
            display_order=order,
        )


def _apply_actions(protocol, actions):
    """Rewrite the protocol's task drafts. These are `ProtocolAction` rows only —
    no `tasks.Task` is created, read or touched here.

    The one rule applied on the way in is that `split_for_assignees` is stored
    normalized: splitting a decision between one person has no meaning, so a
    single assignee always stores `False` whatever the checkbox posted. This is
    the single write point for the flag, which is why the browser's matching
    behaviour can stay presentation.
    """
    for order, item in enumerate(actions):
        action = ProtocolAction.objects.create(
            protocol=protocol,
            task_text=item['text'],
            department=item['department'],
            due_date=item['due_date'],
            # `.get()`: the flag was added after this structure was, and a
            # caller that does not mention it means the shared task every
            # decision produced before — the same answer as the model default.
            split_for_assignees=(
                item.get('split_for_assignees', False) and len(item['assignees']) > 1
            ),
            # Stored as answered, with no normalization: unlike splitting, a
            # required attachment means the same for one исполнитель as for
            # five. `.get()` for the same reason as above — the flag was added
            # after this structure, and a caller that omits it means the
            # unrestricted task every decision produced before.
            requires_attachment=item.get('requires_attachment', False),
            display_order=order,
        )
        ProtocolActionAssignee.objects.bulk_create(
            [ProtocolActionAssignee(action=action, user=user) for user in item['assignees']]
        )


@transaction.atomic
def save_protocol_draft(protocol, user, data):
    """Persist the whole structured draft, or none of it.

    One transaction covers the lock, the re-check and every block, so a refusal
    anywhere — a lost race, a revoked right, a speaker who is no longer a
    participant — leaves the stored protocol exactly as it was.
    """
    locked = Protocol.objects.select_for_update().filter(pk=protocol.pk).first()
    if locked is None:
        raise ProtocolWorkflowError('Протокол уже удалён.')
    # Re-checked *after* the lock: the page may have been open while the
    # protocol moved on or the right was withdrawn.
    if not can_edit_protocol(locked, user):
        raise ProtocolWorkflowError('Изменить протокол в его текущем состоянии нельзя.')

    before = _document_snapshot(locked)
    # Speeches protect the participants they point at, so they go first and are
    # rebuilt after the participant rows settle — the project's fixed order for
    # dependent rows.
    locked.speeches.all().delete()
    _apply_participants(locked, data['participants'])
    _apply_speeches(locked, data['speeches'])
    locked.agenda_items.all().delete()
    ProtocolAgendaItem.objects.bulk_create(
        [
            ProtocolAgendaItem(protocol=locked, text=text, display_order=order)
            for order, text in enumerate(data['agenda'])
        ]
    )
    locked.actions.all().delete()
    _apply_actions(locked, data['actions'])

    if _document_snapshot(locked) == before:
        return locked
    # `auto_now` is skipped when the field is not listed, so it is listed.
    locked.save(update_fields=['updated_at'])
    ProtocolHistoryEvent.objects.create(
        protocol=locked,
        actor=user,
        event_type=ProtocolHistoryEvent.EventType.EDITED,
        revision=locked.revision,
        message=f'Протокол «{locked.protocol_type.name} №{locked.number}» отредактирован.',
    )
    # Only on the path that actually stored something: an unchanged submission
    # returned above and produced neither a history event nor an event here.
    emit_protocol_updated(locked)
    return locked


# --------------------------------------------------------------------------
# Approval workflow
#
# One lock order, everywhere: `Protocol.select_for_update()` first, then the
# approvals, then the tasks. Every transition re-reads the authoritative row
# *after* taking that lock, so a stale tab, a double click or two approvers
# pressing at the same instant serialize instead of racing. Nothing here is
# reachable from the browser yet — the endpoints are the next stage.
# --------------------------------------------------------------------------

# Only Saturday and Sunday are skipped; there is deliberately no holiday
# calendar to fall out of date.
APPROVAL_TASK_WORKING_DAYS = 2


def _pk_of(value):
    return getattr(value, 'pk', value)


def _lock_protocol(protocol):
    locked = Protocol.objects.select_for_update().filter(pk=_pk_of(protocol)).first()
    if locked is None:
        raise ProtocolWorkflowError('Протокол уже удалён.')
    return locked


def _protocol_label(protocol):
    return f'{protocol.protocol_type.name} №{protocol.number}'


def _record(protocol, actor, event_type, message):
    return ProtocolHistoryEvent.objects.create(
        protocol=protocol,
        actor=actor,
        event_type=event_type,
        revision=protocol.revision,
        message=message,
    )


def _resolve_approver_department(user, participant):
    """The department this approval — and its task — belongs to.

    The department chosen for the participant in the editor wins: that is the
    role the person takes in *this* protocol. Only when there is none does the
    profile answer, and when neither does, the submission is refused rather
    than a task created without a department.
    """
    if participant is not None and participant.department_id is not None:
        return participant.department
    profile = getattr(user, 'userprofile', None)
    return getattr(profile, 'department', None)


def collect_required_approvers(protocol):
    """Who must approve the protocol as it is stored right now.

    `participants marked requires_approval` UNION `every ProtocolAction
    assignee`, MINUS the author — the one place this formula exists. A person
    required for both reasons gets one entry carrying both flags, and an author
    assigned to a protocol task gets no entry at all: an author does not
    approve their own document.

    Returns entries in a stable order (participants as displayed, then action
    assignees), each `{'user', 'participant', 'required_as_participant',
    'required_as_action_assignee'}`.
    """
    entries = {}

    def _entry(user, participant=None):
        existing = entries.get(user.pk)
        if existing is None:
            existing = {
                'user': user,
                'participant': participant,
                'required_as_participant': False,
                'required_as_action_assignee': False,
            }
            entries[user.pk] = existing
        elif existing['participant'] is None and participant is not None:
            existing['participant'] = participant
        return existing

    participants = {
        participant.user_id: participant
        for participant in protocol.participants.select_related(
            'department', 'user__userprofile__department'
        )
    }
    for participant in sorted(participants.values(), key=lambda p: (p.display_order, p.pk)):
        if participant.requires_approval and participant.user_id != protocol.author_id:
            _entry(participant.user, participant)['required_as_participant'] = True

    actions = protocol.actions.prefetch_related(
        'assignees__user__userprofile__department'
    ).order_by('display_order', 'pk')
    for action in actions:
        for assignment in sorted(action.assignees.all(), key=lambda a: a.pk):
            if assignment.user_id == protocol.author_id:
                # Excluded here too, not only among participants: being named
                # in a decision does not make an author approve themselves.
                continue
            entry = _entry(assignment.user, participants.get(assignment.user_id))
            entry['required_as_action_assignee'] = True

    return list(entries.values())


def _is_usable_employee(user):
    profile = getattr(user, 'userprofile', None)
    return bool(user.is_active and profile is not None and profile.is_active)


def _user_label(user):
    return user.get_full_name() or user.get_username()


def validate_protocol_for_approval(protocol):
    """Re-read the stored protocol and refuse anything that cannot be approved.

    Existence is not completeness: a draft can be saved, then emptied, then
    submitted from a stale tab. Everything the approval and the archive depend
    on is checked here against the persisted rows, and the caller runs this
    *inside* the transaction that holds the protocol lock, so a refusal never
    leaves a partial write behind.

    Returns the required-approver entries, each with its resolved department.
    """
    if not protocol.participants.filter(user_id=protocol.author_id).exists():
        raise ProtocolWorkflowError('Протокол повреждён: автор не является участником.')
    if not any(item.text.strip() for item in protocol.agenda_items.all()):
        raise ProtocolWorkflowError('Добавьте хотя бы один вопрос повестки.')

    speeches = list(protocol.speeches.select_related('speaker'))
    if not any(speech.text.strip() for speech in speeches):
        raise ProtocolWorkflowError('Добавьте хотя бы одно выступление в разделе «Слушали».')
    for speech in speeches:
        if speech.speaker.protocol_id != protocol.pk:
            raise ProtocolWorkflowError('Выступающий должен быть участником этого протокола.')

    actions = protocol.actions.select_related('department').prefetch_related('assignees__user')
    for action in actions:
        if not action.task_text.strip() or action.department_id is None or action.due_date is None:
            raise ProtocolWorkflowError(
                'У каждой задачи протокола должны быть текст, подразделение и срок.'
            )
        if not any(
            _is_usable_employee(assignment.user) for assignment in action.assignees.all()
        ):
            raise ProtocolWorkflowError(
                'У каждой задачи протокола должен быть хотя бы один активный исполнитель.'
            )

    approvers = collect_required_approvers(protocol)
    for entry in approvers:
        user = entry['user']
        if not _is_usable_employee(user):
            raise ProtocolWorkflowError(
                f'Согласующий «{_user_label(user)}» больше не является активным сотрудником.'
            )
        department = _resolve_approver_department(user, entry['participant'])
        if department is None:
            raise ProtocolWorkflowError(
                f'У согласующего «{_user_label(user)}» не определено подразделение.'
            )
        entry['department'] = department
    return approvers


@transaction.atomic
def send_protocol_for_approval(protocol, actor):
    """Move an own `DRAFT`/`REVISION` protocol to `APPROVAL` as a new revision.

    Author-only. An administrator may *edit* the allowed states, but sending a
    document for approval states that its author is finished with it, and
    nobody makes that statement on someone else's behalf.

    A protocol whose current content requires nobody's approval — only the
    author, no approval-marked participants, no non-author assignees — is not
    left waiting in `APPROVAL` for a signature that can never arrive: the
    revision and the send event are recorded, and it is finalized immediately
    inside this same transaction.
    """
    # Imported inside the workflow, not at module level: `tasks.models`
    # already imports `protocols.models`, and a top-level import here would
    # close the circle.
    from notifications.services import notify_protocol_approval_required
    from tasks.services import create_protocol_approval_task

    locked = _lock_protocol(protocol)
    if locked.author_id != _pk_of(actor):
        raise ProtocolWorkflowError('Отправить протокол на согласование может только его автор.')
    if locked.status not in (Protocol.Status.DRAFT, Protocol.Status.REVISION):
        raise ProtocolWorkflowError('Протокол уже отправлен на согласование или заархивирован.')
    resent = locked.status == Protocol.Status.REVISION
    # The status readers were actually looking at. One event is emitted at the
    # end of the transition against this value, so a submission that requires
    # nobody and finalizes below is announced as the `DRAFT → ARCHIVED` the
    # user observes, never as an `APPROVAL` state that never existed for them.
    previous_status = locked.status

    approvers = validate_protocol_for_approval(locked)

    # A new revision, always. The approvals of the previous one are left
    # untouched as history and never count towards this one.
    locked.revision += 1
    locked.status = Protocol.Status.APPROVAL
    locked.save(update_fields=['status', 'revision', 'updated_at'])
    label = _protocol_label(locked)
    _record(
        locked,
        actor,
        ProtocolHistoryEvent.EventType.RESENT_FOR_APPROVAL
        if resent
        else ProtocolHistoryEvent.EventType.SENT_FOR_APPROVAL,
        f'Протокол «{label}» отправлен на согласование (редакция {locked.revision}).',
    )

    due_date = add_working_days(timezone.localdate(), APPROVAL_TASK_WORKING_DAYS)
    for entry in approvers:
        user = entry['user']
        snapshot = build_participant_snapshot(user, entry['department'])
        approval = ProtocolApproval.objects.create(
            protocol=locked,
            revision=locked.revision,
            user=user,
            status=ProtocolApproval.Status.PENDING,
            required_as_participant=entry['required_as_participant'],
            required_as_action_assignee=entry['required_as_action_assignee'],
            display_name=snapshot['display_name'],
            position=snapshot['position'],
            department_name=snapshot['department_name'],
        )
        approval.task = create_protocol_approval_task(
            locked,
            user,
            department=entry['department'],
            due_date=due_date,
            created_by=locked.author,
            task_text=f'Согласовать протокол {label}',
        )
        approval.save(update_fields=['task'])
        # Only now, with the approval row committed to this transaction, does
        # the person have anything to be told about. The queue task creates no
        # notification of its own: one required action, one notification.
        notify_protocol_approval_required(locked, approval, actor)

    if not approvers:
        _finalize_protocol(locked, actor)
    emit_protocol_status_changed(locked, previous_status)
    return locked


@transaction.atomic
def approve_protocol(protocol, actor):
    """Record one approver's decision to approve the current revision.

    The protocol row is locked first, so two approvers pressing at the same
    moment queue behind each other: whichever commits second re-reads the
    pending set and is the one that sees its approval become the last,
    finalizing inside its own transaction. Only one of them can observe that.
    """
    from tasks.services import complete_protocol_approval_task

    locked = _lock_protocol(protocol)
    if locked.status != Protocol.Status.APPROVAL:
        raise ProtocolWorkflowError('Протокол не находится на согласовании.')
    approval = ProtocolApproval.objects.filter(
        protocol=locked,
        revision=locked.revision,
        user_id=_pk_of(actor),
        status=ProtocolApproval.Status.PENDING,
    ).first()
    if approval is None:
        # Not an approver, already decided, or a page left open on an older
        # revision — one refusal covers all three.
        raise ProtocolWorkflowError('Согласование этого протокола вам недоступно.')

    decided_at = timezone.now()
    approval.status = ProtocolApproval.Status.APPROVED
    approval.decided_at = decided_at
    approval.save(update_fields=['status', 'decided_at'])
    complete_protocol_approval_task(approval.task, actor, decided_at)
    _record(
        locked,
        actor,
        ProtocolHistoryEvent.EventType.APPROVED_BY_USER,
        f'{approval.display_name}: протокол согласован (редакция {locked.revision}).',
    )

    still_pending = ProtocolApproval.objects.filter(
        protocol=locked,
        revision=locked.revision,
        status=ProtocolApproval.Status.PENDING,
    ).exists()
    # One decision is one approval event, whether or not it happens to be the
    # last one; the archive it may trigger is a second, separate fact.
    emit_protocol_approval_changed(locked, approval)
    if not still_pending:
        _finalize_protocol(locked, actor)
        emit_protocol_status_changed(locked, Protocol.Status.APPROVAL)
    return locked


@transaction.atomic
def return_protocol_for_revision(protocol, actor, comment):
    """Send the protocol back to its author, closing the rest of the round.

    An approval already given stays `APPROVED`: it is a historical fact about
    that revision. The still-pending ones become `CANCELLED` and their tasks
    are closed *without* an approver, because those people no longer have
    anything to sign. Nothing is deleted, on either side.
    """
    from notifications.services import notify_protocol_returned
    from tasks.services import cancel_protocol_approval_task, complete_protocol_approval_task

    comment = (comment or '').strip()
    if not comment:
        raise ProtocolWorkflowError('Укажите причину возврата на доработку.')

    locked = _lock_protocol(protocol)
    if locked.status != Protocol.Status.APPROVAL:
        raise ProtocolWorkflowError('Протокол не находится на согласовании.')
    approvals = list(
        ProtocolApproval.objects.filter(
            protocol=locked,
            revision=locked.revision,
            status=ProtocolApproval.Status.PENDING,
        ).select_related('task__status')
    )
    actor_id = _pk_of(actor)
    approval = next((item for item in approvals if item.user_id == actor_id), None)
    if approval is None:
        raise ProtocolWorkflowError('Возврат этого протокола на доработку вам недоступен.')

    decided_at = timezone.now()
    approval.status = ProtocolApproval.Status.RETURNED
    approval.decided_at = decided_at
    approval.return_comment = comment
    approval.save(update_fields=['status', 'decided_at', 'return_comment'])
    complete_protocol_approval_task(approval.task, actor, decided_at)

    for other in approvals:
        if other.pk == approval.pk:
            continue
        other.status = ProtocolApproval.Status.CANCELLED
        other.decided_at = decided_at
        other.save(update_fields=['status', 'decided_at'])
        cancel_protocol_approval_task(other.task, decided_at)

    locked.status = Protocol.Status.REVISION
    locked.save(update_fields=['status', 'updated_at'])
    _record(
        locked,
        actor,
        ProtocolHistoryEvent.EventType.RETURNED_FOR_REVISION,
        f'Протокол возвращён на доработку (редакция {locked.revision}). Причина: {comment}',
    )
    # The same reason, a third time and for a third purpose: the approval row
    # keeps it as the decision, the history event as the workflow record, and
    # the feed carries it as the message the author actually has to answer.
    # Inside this transaction, so a refusal above leaves no comment behind, and
    # `record_history=False` because `RETURNED_FOR_REVISION` above already *is*
    # this event — a `COMMENT_ADDED` beside it would describe it twice.
    add_protocol_comment(locked, actor, comment, record_history=False)
    # Inside this transaction, so a refusal anywhere above leaves no
    # notification claiming a return that never happened. The reason itself is
    # not copied into it — it stays authoritative on the approval row.
    notify_protocol_returned(locked, actor)
    # The returning decision and the transition it caused. The cancelled
    # approvals of the same round are not announced one by one: the client
    # refetches the whole approval block from the ordinary endpoint anyway.
    emit_protocol_approval_changed(locked, approval)
    emit_protocol_status_changed(locked, Protocol.Status.APPROVAL)
    return locked


def _finalize_protocol(protocol, actor):
    """Archive the protocol and turn every decision into a real task.

    **The caller must already hold the `Protocol` row lock inside the workflow
    transaction.** This is not an entry point and takes no lock of its own: it
    is the tail of `send_protocol_for_approval()` and `approve_protocol()`, and
    its whole guarantee is that the archive, the tasks and their assignees
    appear together or not at all. Any failure here rolls the surrounding
    transition back — the protocol does not stay archived, the final approval
    is not half-committed, and no partial set of tasks survives.
    """
    from notifications.services import (
        notify_protocol_approved,
        notify_protocol_task_assigned,
    )
    from tasks.models import Task
    from tasks.services import create_protocol_action_task

    if ProtocolApproval.objects.filter(
        protocol=protocol,
        revision=protocol.revision,
        status=ProtocolApproval.Status.PENDING,
    ).exists():
        raise ProtocolWorkflowError('По протоколу остались несогласованные позиции.')

    protocol.status = Protocol.Status.ARCHIVED
    protocol.save(update_fields=['status', 'updated_at'])
    _record(
        protocol,
        actor,
        ProtocolHistoryEvent.EventType.ARCHIVED,
        f'Протокол «{_protocol_label(protocol)}» согласован и помещён в архив.',
    )
    # The author is told the document is finished. `actor` is the last approver
    # — or, for a protocol nobody had to approve, the author themselves, which
    # the notification service deliberately does not treat as self-notification.
    notify_protocol_approved(protocol, actor)

    actions = list(
        protocol.actions.select_related('department')
        .prefetch_related('assignees__user')
        .order_by('display_order', 'pk')
    )
    if not actions:
        # No decisions, so no tasks and no `TASKS_CREATED`: the event states
        # that tasks were created, and an empty one would be audit noise.
        return protocol
    # The two unique constraints on `Task` already make a second task for the
    # same decision — or for the same person within it — impossible; this turns
    # the resulting IntegrityError into a controlled refusal.
    if Task.objects.filter(protocol_action__in=actions).exists():
        raise ProtocolWorkflowError('Задачи по этому протоколу уже созданы.')
    created = 0
    for action in actions:
        assignments = list(action.assignees.all())
        # One decision, one or many tasks. `split_for_assignees` is stored
        # already normalized, so the length check is only a second lock on the
        # rule that splitting a single assignee means nothing.
        if action.split_for_assignees and len(assignments) > 1:
            batches = [([assignment.user], assignment.user_id) for assignment in assignments]
        else:
            batches = [([assignment.user for assignment in assignments], None)]
        for users, individual_id in batches:
            task = create_protocol_action_task(
                protocol,
                action,
                [user.pk for user in users],
                created_by=protocol.author,
                individual_assignee_id=individual_id,
            )
            # After the task and its assignees exist, never before: a
            # notification must not describe a task a later failure would roll
            # back. One notification per task, so a split assignee is told
            # about their own task exactly once and nobody is told twice.
            notify_protocol_task_assigned(task, actor, users)
            created += 1
    _record(
        protocol,
        actor,
        ProtocolHistoryEvent.EventType.TASKS_CREATED,
        f'По протоколу создано задач: {created}.',
    )
    return protocol


# --------------------------------------------------------------------------
# Collaboration: comments and attachments
#
# The protocol's discussion, not its document. Nothing here touches the
# status, the revision or the approval round, and nothing here is a workflow
# transition — but every write still goes through this module, records its own
# history event and announces itself on the existing protocol channel, so a
# reader's open page refreshes exactly as it does for a save.
#
# No notification is created for either: the protocol notification set is the
# approval one, and a comment is not an approval duty.
# --------------------------------------------------------------------------


def add_protocol_comment(protocol, user, text, record_history=True):
    """One comment in the feed, with its history event.

    `record_history=False` is for the mandatory return comment, whose workflow
    event is the return itself — see `return_protocol_for_revision()`. Every
    other caller records `COMMENT_ADDED`, which says that a comment appeared
    and by whom, never what it said.
    """
    text = (text or '').strip()
    if not text:
        raise ProtocolWorkflowError('Введите текст комментария.')
    with transaction.atomic():
        comment = ProtocolComment.objects.create(
            protocol=protocol,
            author=user if getattr(user, 'is_authenticated', False) else None,
            text=text,
        )
        if record_history:
            _record(
                protocol,
                user,
                ProtocolHistoryEvent.EventType.COMMENT_ADDED,
                'Добавлен комментарий к протоколу.',
            )
        # The existing protocol event, carrying an id and nothing else: the
        # feed itself is refetched from the ordinary permission-checked
        # fragment, so no comment text ever travels through Redis.
        emit_protocol_updated(protocol)
    return comment


def _delete_attachment_file(storage, file_name, *, attachment_id, protocol_id, user_id, operation, failure_outcome):
    """Best-effort storage cleanup. A failure is logged, never raised.

    Storage is not transactional, so the file and the row can only be kept in
    step by ordering: written before the row is committed, deleted after.
    """
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
            protocol_id=protocol_id,
            user_id=user_id,
            operation=operation,
            error_type=type(exc).__name__,
            outcome=failure_outcome,
        )


def add_protocol_attachment(protocol, user, uploaded_file, description=''):
    """Store one file against the protocol, or leave nothing behind.

    The file is written to storage first and the row second, because storage
    cannot join the transaction: if the database work fails, the orphan is
    removed on the way out. The permission is re-checked under the protocol row
    lock — an archive that happened between the click and the save must not
    still accept the upload.
    """
    if not can_add_protocol_attachment(protocol, user):
        raise ProtocolWorkflowError('Добавление вложения недоступно для этого протокола.')

    attachment = ProtocolAttachment(
        protocol=protocol,
        uploaded_by=user if getattr(user, 'is_authenticated', False) else None,
        original_name=uploaded_file.name,
        description=description,
        file_size=getattr(uploaded_file, 'size', 0) or 0,
        content_type=getattr(uploaded_file, 'content_type', '') or '',
    )
    file_written = False
    try:
        attachment.file.save(uploaded_file.name, uploaded_file, save=False)
        file_written = True
        with transaction.atomic():
            locked = _lock_protocol(protocol)
            if not can_add_protocol_attachment(locked, user):
                raise ProtocolWorkflowError('Добавление вложения недоступно для этого протокола.')
            attachment.protocol = locked
            attachment.save()
            _record(
                locked,
                user,
                ProtocolHistoryEvent.EventType.ATTACHMENT_ADDED,
                f'Добавлено вложение: {attachment.original_name}.',
            )
            emit_protocol_updated(locked)
    except Exception:
        if file_written:
            _delete_attachment_file(
                attachment.file.storage,
                attachment.file.name,
                attachment_id=getattr(attachment, 'pk', None),
                protocol_id=_pk_of(protocol),
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
        protocol_id=_pk_of(protocol),
        user_id=_pk_of(user),
        size_bytes=attachment.file_size,
        operation='upload',
        outcome='ok',
    )
    return attachment


def delete_protocol_attachment(attachment, user):
    """Remove one attachment, re-checking the right under the protocol lock.

    Returns `False` when there was nothing left to delete — a double-submitted
    form is not an error. The file itself is unlinked only `on_commit`, so a
    rolled-back deletion cannot destroy a file whose row survived.
    """
    if not can_view_protocol(attachment.protocol, user) or not can_delete_protocol_attachment(
        attachment, user
    ):
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.access_denied',
            attachment_id=_pk_of(attachment),
            protocol_id=getattr(attachment, 'protocol_id', None),
            user_id=_pk_of(user),
            operation='delete',
            outcome='denied',
        )
        raise ProtocolWorkflowError('Удаление вложения недоступно для этого протокола.')

    protocol_id = attachment.protocol_id
    attachment_id = attachment.pk
    with transaction.atomic():
        # Fixed lock order — protocol first, then its attachment — the same
        # order every other protocol operation takes.
        locked = Protocol.objects.select_for_update().filter(pk=protocol_id).first()
        if locked is None:
            return False
        locked_attachment = (
            ProtocolAttachment.objects.select_for_update()
            .filter(pk=attachment_id, protocol_id=protocol_id)
            .first()
        )
        if locked_attachment is None:
            return False
        if not can_delete_protocol_attachment(locked_attachment, user):
            raise ProtocolWorkflowError('Удаление вложения недоступно для этого протокола.')

        original_name = locked_attachment.original_name
        file_name = locked_attachment.file.name
        storage = locked_attachment.file.storage
        locked_attachment.delete()
        _record(
            locked,
            user,
            ProtocolHistoryEvent.EventType.ATTACHMENT_DELETED,
            f'Вложение удалено: {original_name}.',
        )
        emit_protocol_updated(locked)
        transaction.on_commit(
            partial(
                _delete_attachment_file,
                storage,
                file_name,
                attachment_id=attachment_id,
                protocol_id=protocol_id,
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
        protocol_id=protocol_id,
        user_id=_pk_of(user),
        operation='delete',
        outcome='ok',
    )
    return True
