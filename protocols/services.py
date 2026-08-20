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

from django.db import transaction

from .models import (
    Protocol,
    ProtocolAction,
    ProtocolActionAssignee,
    ProtocolAgendaItem,
    ProtocolHistoryEvent,
    ProtocolParticipant,
    ProtocolSpeech,
    ProtocolType,
)
from .permissions import can_delete_draft_protocol, can_edit_protocol


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
    locked.delete()


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
        'actions': [
            (
                action.task_text,
                action.department_id,
                action.due_date,
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
    no `tasks.Task` is created, read or touched here."""
    for order, item in enumerate(actions):
        action = ProtocolAction.objects.create(
            protocol=protocol,
            task_text=item['text'],
            department=item['department'],
            due_date=item['due_date'],
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
    return locked
