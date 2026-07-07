from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from references.models import ActStatus, DefectType, Operation

from .forms import ActCreateForm, KoDecisionForm, ToAnalysisForm
from .models import Act, get_act_status
from .permissions import can_create_act, can_view_act
from .services import (
    ActWorkflowError,
    apply_ko_decision,
    apply_to_analysis,
    get_available_act_actions,
    get_role_context_text,
    get_visible_acts_for_user,
    send_to_ko,
)


@login_required
def act_list(request):
    acts = get_visible_acts_for_user(request.user)

    status = request.GET.get('status')
    operation = request.GET.get('operation')
    defect_type = request.GET.get('defect_type')
    search = request.GET.get('search', '').strip()

    if status:
        acts = acts.filter(status_id=status)
    if operation:
        acts = acts.filter(operation_id=operation)
    if defect_type:
        acts = acts.filter(defect_type_id=defect_type)
    if search:
        acts = acts.filter(
            Q(number__icontains=search)
            | Q(party_number__icontains=search)
            | Q(nomenclature__icontains=search)
        )

    context = {
        'active_page': 'acts',
        'page_title': 'Акты операционного контроля',
        'page_description': get_role_context_text(request.user),
        'acts': acts,
        'statuses': ActStatus.objects.filter(is_active=True),
        'operations': Operation.objects.filter(is_active=True),
        'defect_types': DefectType.objects.filter(is_active=True),
        'selected': {
            'status': status or '',
            'operation': operation or '',
            'defect_type': defect_type or '',
            'search': search,
        },
        'can_create': can_create_act(request.user),
    }
    return render(request, 'acts/list.html', context)


@login_required
def act_create(request):
    if not can_create_act(request.user):
        messages.error(request, 'Недостаточно прав для создания акта.')
        return redirect('acts:list')

    if request.method == 'POST':
        form = ActCreateForm(request.POST)
        if form.is_valid():
            act = form.save(commit=False)
            act.created_by = request.user
            try:
                act.status = get_act_status('CREATED_OTK')
                act.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, 'Акт создан.')
                return redirect('acts:detail', pk=act.pk)
    else:
        form = ActCreateForm()

    return render(
        request,
        'acts/form.html',
        {
            'active_page': 'acts',
            'form': form,
        },
    )


@login_required
def act_detail(request, pk):
    act = get_object_or_404(
        Act.objects.select_related(
            'created_by',
            'operation',
            'defect_type',
            'priority',
            'status',
            'ko_decision_by',
            'to_analysis_by',
        ),
        pk=pk,
    )
    if not can_view_act(act, request.user):
        raise Http404('No Act matches the given query.')
    context = {
        'active_page': 'acts',
        'act': act,
        'available_actions': get_available_act_actions(act, request.user),
    }
    return render(request, 'acts/detail.html', context)


@login_required
def act_send_to_ko(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return redirect('acts:detail', pk=act.pk)

    try:
        send_to_ko(act, request.user)
    except ActWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Акт передан в КО.')
    return redirect('acts:detail', pk=act.pk)


@login_required
def act_ko_decision(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if not get_available_act_actions(act, request.user)['ko_decision']:
        messages.error(request, 'Решение КО для этого акта недоступно.')
        return redirect('acts:detail', pk=act.pk)

    if request.method == 'POST':
        form = KoDecisionForm(request.POST, instance=act)
        if form.is_valid():
            try:
                act = apply_ko_decision(
                    act,
                    request.user,
                    form.cleaned_data['ko_decision'],
                    form.cleaned_data['ko_comment'],
                )
            except ActWorkflowError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, 'Решение КО сохранено.')
                return redirect('acts:detail', pk=act.pk)
    else:
        form = KoDecisionForm(instance=act)

    return render(
        request,
        'acts/ko_decision.html',
        {
            'active_page': 'acts',
            'act': act,
            'form': form,
        },
    )


@login_required
def act_to_analysis(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if not get_available_act_actions(act, request.user)['to_analysis']:
        messages.error(request, 'Анализ ТО для этого акта недоступен.')
        return redirect('acts:detail', pk=act.pk)

    if request.method == 'POST':
        form = ToAnalysisForm(request.POST, instance=act)
        if form.is_valid():
            try:
                act = apply_to_analysis(
                    act,
                    request.user,
                    form.cleaned_data['to_root_cause'],
                    form.cleaned_data['to_action_summary'],
                )
            except ActWorkflowError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, 'Анализ ТО сохранен.')
                return redirect('acts:detail', pk=act.pk)
    else:
        form = ToAnalysisForm(instance=act)

    return render(
        request,
        'acts/to_analysis.html',
        {
            'active_page': 'acts',
            'act': act,
            'form': form,
        },
    )
