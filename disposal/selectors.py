"""What «Утилизация МП» reads: the МП waiting for disposal and those disposed of."""

from django.db.models import F

from acts.models import Act, ActDefect

from .models import DisposalItem


# An act still in front of КО, or put back before КО — its decision is not
# (or no longer) the one in force — and a cancelled act ask for nothing.
NOT_DECIDED_STATUSES = ('CREATED_OTK', 'KO_REVIEW', 'CANCELLED')


def pending_defects():
    """Every «Цех МП» defect prohibited from use by the КО decision in force.

    `workshop == MP_SHOP` and `ko_decision == PROHIBIT_USE`, decided in the
    act's current КО round (`ko_round` equal to the act's — an act returned
    from ТО to КО starts a new round and waits until КО decides again), on an
    act that has left КО and is not cancelled, and not disposed of yet. The
    same two facts `ACT_REJECTION` reads, so ПДО's notice and this list name
    the same products. Oldest decision first.
    """
    return (
        ActDefect.objects.filter(
            workshop=ActDefect.Workshop.MP_SHOP,
            ko_decision=Act.KoDecision.PROHIBIT_USE,
            ko_round=F('act__ko_round'),
            disposal_item__isnull=True,
        )
        .exclude(act__status__code__in=NOT_DECIDED_STATUSES)
        .select_related('act', 'defect_type', 'ko_decision_by')
        .order_by('ko_decision_at', 'act_id', 'created_at', 'pk')
    )


def disposed_items():
    """Every disposed МП, the newest disposal first, with its act and master."""
    return DisposalItem.objects.select_related(
        'disposal__disposed_by', 'defect__act',
    ).order_by('-disposal__disposed_at', '-disposal_id', 'position', 'pk')


def build_disposal_state(tab):
    tab = tab if tab in ('pending', 'done') else 'pending'
    pending = pending_defects()
    done = disposed_items()
    return {
        'tab': tab,
        'tab_counts': {'pending': pending.count(), 'done': done.count()},
        'defects': list(pending) if tab == 'pending' else [],
        'items': list(done) if tab == 'done' else [],
    }
