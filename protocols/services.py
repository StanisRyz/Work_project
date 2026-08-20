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
    ProtocolHistoryEvent,
    ProtocolParticipant,
    ProtocolSpeech,
    ProtocolType,
)
from .permissions import can_delete_draft_protocol


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
