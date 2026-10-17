"""«Утилизация МП»: two tabs, the confirmation step and «Акт о списании брака»."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.directory import get_employee_directory

from .models import DisposalAct
from .permissions import can_dispose
from .selectors import build_disposal_state, pending_defects
from .services import DisposalError, clean_member_ids, dispose_defects

FORM_CODE = 'Форма ДП-СМК 07.04/1'


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
    """«Утилизировать»: the act to be signed, with «Комиссия в составе» to choose.

    The right is asked before the method; a GET goes back to the list. A POST
    without `confirmed=1` writes nothing: it draws «Акт о списании брака» of
    exactly the ticked МП still waiting, with «Подразделение | Сотрудник» rows
    for the commission — the master first, «+ Добавить члена комиссии» for one
    more (a real submit button without JavaScript). Only the confirmed POST
    reaches `dispose_defects()`, which asks everything again under its locks;
    a refusal comes back as the same page with the selection kept, and a
    success opens the act ready to print.
    """
    if not can_dispose(request.user):
        raise PermissionDenied
    if request.method != 'POST':
        return redirect(_list_url('pending'))
    ids = request.POST.getlist('defect')
    posted_members = request.POST.getlist('member')
    error = ''
    if request.POST.get('confirmed') == '1':
        try:
            disposal = dispose_defects(ids, actor=request.user, member_ids=posted_members)
        except DisposalError as exc:
            error = str(exc)
        else:
            messages.success(
                request, f'{disposal.label}: утилизировано МП — {disposal.items.count()}. Распечатайте акт.',
            )
            return redirect('disposal:print', pk=disposal.pk)
    selected = [pk for pk in ids if pk.isdigit()]
    defects = list(pending_defects().filter(pk__in=selected))
    if not defects:
        messages.error(request, error or 'Отметьте МП, которые нужно утилизировать.')
        return redirect(_list_url('pending'))
    members = clean_member_ids(posted_members)
    if 'member' not in request.POST:
        members = [request.user.pk]
    return render(request, 'disposal/print.html', {
        'preview': True,
        'form_code': FORM_CODE,
        'rows': [_defect_row(defect) for defect in defects],
        'defect_ids': [defect.pk for defect in defects],
        'disposed_at': timezone.now(),
        'error': error,
        **_commission_picker(members, extra_row='add_member_row' in request.POST),
    }, status=400 if error else 200)


@login_required
def disposal_print(request, pk):
    """«Акт о списании брака» as it was signed; read by everyone."""
    disposal = get_object_or_404(DisposalAct, pk=pk)
    return render(request, 'disposal/print.html', {
        'preview': False,
        'form_code': FORM_CODE,
        'disposal': disposal,
        'commission': list(disposal.commission.all()),
        'rows': [_item_row(item) for item in disposal.items.all()],
        'disposed_at': disposal.disposed_at,
    })


def _commission_picker(member_ids, *, extra_row=False):
    """The rows of «Комиссия в составе» and the directory they choose from.

    One row per chosen person, with their department beside them, so a page
    drawn again reads as it was filled; one empty row more when «+ Добавить
    члена комиссии» asked for it without JavaScript, or when nobody is chosen.
    """
    directory = get_employee_directory()
    employees = list(directory['employees'])
    departments_of = {
        user.pk: str(getattr(getattr(user, 'userprofile', None), 'department_id', '') or '')
        for user in employees
    }
    rows = [{'user': str(pk), 'department': departments_of.get(pk, '')} for pk in member_ids]
    if extra_row or not rows:
        rows.append({'user': '', 'department': ''})
    return {
        'departments': directory['departments'],
        'employees': employees,
        'member_rows': rows,
        'empty_member_row': {'user': '', 'department': ''},
    }


def _with_note(row):
    """«Примечание»: what tells one product of the same name from another."""
    parts = [
        f"заказ {row['order_number']}" if row['order_number'] else '',
        f"ЗНП {row['znp_number']}" if row['znp_number'] else '',
        f"партия {row['party_number']}" if row['party_number'] else '',
        row['mp_type'],
        row['defect_type'],
    ]
    row['note'] = '; '.join(part for part in parts if part)
    return row


def _defect_row(defect):
    act = defect.act
    return _with_note({
        'act_number': act.number, 'act_date': timezone.localtime(act.created_at).date(),
        'nomenclature': act.nomenclature,
        'order_number': act.order_number, 'znp_number': defect.znp_number,
        'party_number': defect.party_number,
        'mp_type': defect.get_mp_type_display() if defect.mp_type else '',
        'defect_type': str(defect.defect_type or ''), 'quantity': defect.nonconforming_quantity,
    })


def _item_row(item):
    return _with_note({
        'act_number': item.act_number, 'act_date': item.act_date,
        'nomenclature': item.nomenclature,
        'order_number': item.order_number, 'znp_number': item.znp_number,
        'party_number': item.party_number, 'mp_type': item.mp_type,
        'defect_type': item.defect_type, 'quantity': item.quantity,
    })
