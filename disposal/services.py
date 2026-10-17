"""The one write of «Утилизация МП»: `dispose_defects()`."""

import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from acts.models import ActDefect
from ecosystem.logging_utils import log_event

from .models import DisposalAct, DisposalCommissionMember, DisposalItem
from .permissions import can_dispose
from .selectors import pending_defects


logger = logging.getLogger('ecosystem.workflow')

STALE_SELECTION = (
    'Часть выбранных МП уже утилизирована или больше не ожидает утилизации. '
    'Обновите список и выберите заново.'
)


COMMISSION_REQUIRED = 'Выберите хотя бы одного члена комиссии.'


class DisposalError(Exception):
    """A refused disposal; nothing was written."""


def clean_member_ids(values):
    """The posted «Комиссия» rows as ids, blanks dropped and repeats collapsed, in order."""
    ids = []
    for value in values or ():
        value = str(value).strip()
        if value.isdigit() and int(value) not in ids:
            ids.append(int(value))
    return ids


def commission_people(member_ids):
    """The active employees among `member_ids`, in the order given."""
    found = {
        user.pk: user
        for user in get_user_model().objects.filter(
            pk__in=member_ids, is_active=True, userprofile__is_active=True,
        ).select_related('userprofile')
    }
    return [found[pk] for pk in member_ids if pk in found]


def commission_snapshot(user):
    """«Должность, ФИО» as the paper prints them: the profile's position, else its role."""
    profile = getattr(user, 'userprofile', None)
    position = ''
    if profile is not None:
        position = (profile.position or '').strip() or profile.get_role_display()
    return {'position': position, 'name': user.get_full_name() or user.get_username()}


def _value(value):
    return str(value).strip() if value not in (None, '') else ''


def dispose_defects(defect_ids, *, actor, member_ids):
    """Record the disposal of exactly the selected prohibited МП, or of none.

    The right (`can_dispose()`) is asked inside the transaction; the selected
    defects are locked (no act lock is taken, so this never meets an act
    transition in the opposite order) and re-read through `pending_defects()`
    after the lock — a selection holding one that was disposed of in another
    tab, or whose act went back to КО meanwhile, is refused whole. The
    one-to-one on `DisposalItem.defect` is the last word against a concurrent
    double disposal, turned into the same refusal. «Комиссия в составе» is
    `member_ids` — at least one active employee, anybody else dropped, each
    stored with the position and the name of the day. Returns the
    `DisposalAct`.
    """
    ids = sorted({int(pk) for pk in defect_ids if str(pk).isdigit()})
    if not ids:
        raise DisposalError('Отметьте МП, которые нужно утилизировать.')
    members = commission_people(clean_member_ids(member_ids))
    if not members:
        raise DisposalError(COMMISSION_REQUIRED)
    try:
        with transaction.atomic():
            if not can_dispose(actor):
                raise DisposalError('Утилизировать МП может мастер цеха МП или администратор.')
            list(ActDefect.objects.select_for_update().filter(pk__in=ids).order_by('pk').values_list('pk', flat=True))
            defects = list(pending_defects().filter(pk__in=ids))
            if len(defects) != len(ids):
                raise DisposalError(STALE_SELECTION)
            disposal = DisposalAct.objects.create(disposed_by=actor)
            DisposalItem.objects.bulk_create([
                DisposalItem(
                    disposal=disposal,
                    defect=defect,
                    position=position,
                    act_number=_value(defect.act.number),
                    act_date=timezone.localtime(defect.act.created_at).date(),
                    nomenclature=_value(defect.act.nomenclature),
                    order_number=_value(defect.act.order_number),
                    znp_number=_value(defect.znp_number),
                    party_number=_value(defect.party_number),
                    mp_type=defect.get_mp_type_display() if defect.mp_type else '',
                    defect_type=_value(defect.defect_type),
                    quantity=defect.nonconforming_quantity,
                )
                for position, defect in enumerate(defects, start=1)
            ])
            DisposalCommissionMember.objects.bulk_create([
                DisposalCommissionMember(
                    disposal=disposal, user=user, order=order, **commission_snapshot(user),
                )
                for order, user in enumerate(members, start=1)
            ])
    except IntegrityError as exc:
        raise DisposalError(STALE_SELECTION) from exc
    log_event(
        logger,
        'INFO',
        'disposal.created',
        disposal_id=disposal.pk,
        item_count=len(defects),
        commission_count=len(members),
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return disposal
