"""The one write of «Утилизация МП»: `dispose_defects()`."""

import logging

from django.db import IntegrityError, transaction

from acts.models import ActDefect
from ecosystem.logging_utils import log_event

from .models import DisposalAct, DisposalItem
from .permissions import can_dispose
from .selectors import pending_defects


logger = logging.getLogger('ecosystem.workflow')

STALE_SELECTION = (
    'Часть выбранных МП уже утилизирована или больше не ожидает утилизации. '
    'Обновите список и выберите заново.'
)


class DisposalError(Exception):
    """A refused disposal; nothing was written."""


def _value(value):
    return str(value).strip() if value not in (None, '') else ''


def dispose_defects(defect_ids, *, actor):
    """Record the disposal of exactly the selected prohibited МП, or of none.

    The right (`can_dispose()`) is asked inside the transaction; the selected
    defects are locked (no act lock is taken, so this never meets an act
    transition in the opposite order) and re-read through `pending_defects()`
    after the lock — a selection holding one that was disposed of in another
    tab, or whose act went back to КО meanwhile, is refused whole. The
    one-to-one on `DisposalItem.defect` is the last word against a concurrent
    double disposal, turned into the same refusal. Returns the `DisposalAct`.
    """
    ids = sorted({int(pk) for pk in defect_ids if str(pk).isdigit()})
    if not ids:
        raise DisposalError('Отметьте МП, которые нужно утилизировать.')
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
    except IntegrityError as exc:
        raise DisposalError(STALE_SELECTION) from exc
    log_event(
        logger,
        'INFO',
        'disposal.created',
        disposal_id=disposal.pk,
        item_count=len(defects),
        actor_user_id=actor.pk,
        outcome='ok',
    )
    return disposal
