"""«Утилизация МП»: two tabs, the confirmation print form and the signed one."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import DisposalAct
from .permissions import can_dispose
from .selectors import build_disposal_state, pending_defects
from .services import DisposalError, dispose_defects


def _list_url(tab):
    return f"{reverse('disposal:list')}?tab={tab}"


@login_required
def disposal_list(request):
    """«Ожидают утилизации» and «Утилизировано». Open to every employee;
    the tick boxes and «Утилизировать» only for whoever may dispose."""
    return render(request, 'disposal/list.html', {
        'active_page': 'disposal', 'header_title': 'Утилизация МП',
        'can_dispose': can_dispose(request.user),
        **build_disposal_state(request.GET.get('tab', '')),
    })


@login_required
def disposal_confirm(request):
    """«Утилизировать» — two POSTs, the second carrying `confirmed=1`.

    The right is asked before the method. The first POST writes nothing: it
    draws the print form of exactly the ticked МП that still wait, with
    «Подтвердить утилизацию» carrying their ids and the flag; only that second
    POST reaches `dispose_defects()`, which asks everything again under its
    locks. A GET goes back to the list.
    """
    if not can_dispose(request.user):
        raise PermissionDenied
    if request.method != 'POST':
        return redirect(_list_url('pending'))
    ids = request.POST.getlist('defect')
    if request.POST.get('confirmed') == '1':
        try:
            disposal = dispose_defects(ids, actor=request.user)
        except DisposalError as exc:
            messages.error(request, str(exc))
            return redirect(_list_url('pending'))
        messages.success(
            request, f'{disposal.label}: утилизировано МП — {disposal.items.count()}.',
        )
        return redirect(_list_url('done'))
    selected = [pk for pk in ids if pk.isdigit()]
    defects = list(pending_defects().filter(pk__in=selected))
    if not defects:
        messages.error(request, 'Отметьте МП, которые нужно утилизировать.')
        return redirect(_list_url('pending'))
    return render(request, 'disposal/print.html', {
        'preview': True,
        'rows': [_defect_row(defect) for defect in defects],
        'defect_ids': [defect.pk for defect in defects],
        'disposed_by': request.user,
        'disposed_at': timezone.now(),
    })


@login_required
def disposal_print(request, pk):
    """The signed form of one disposal, as it was printed; read by everyone."""
    disposal = get_object_or_404(DisposalAct.objects.select_related('disposed_by'), pk=pk)
    return render(request, 'disposal/print.html', {
        'preview': False,
        'disposal': disposal,
        'rows': [_item_row(item) for item in disposal.items.all()],
        'disposed_by': disposal.disposed_by,
        'disposed_at': disposal.disposed_at,
    })


def _defect_row(defect):
    act = defect.act
    return {
        'act_number': act.number, 'nomenclature': act.nomenclature,
        'order_number': act.order_number, 'znp_number': defect.znp_number,
        'party_number': defect.party_number,
        'mp_type': defect.get_mp_type_display() if defect.mp_type else '',
        'defect_type': str(defect.defect_type or ''), 'quantity': defect.nonconforming_quantity,
    }


def _item_row(item):
    return {
        'act_number': item.act_number, 'nomenclature': item.nomenclature,
        'order_number': item.order_number, 'znp_number': item.znp_number,
        'party_number': item.party_number, 'mp_type': item.mp_type,
        'defect_type': item.defect_type, 'quantity': item.quantity,
    }
