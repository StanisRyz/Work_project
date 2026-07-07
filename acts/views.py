from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from references.models import ActStatus, DefectType, Operation

from .forms import ActAttachmentForm, ActCommentForm, ActCreateForm, KoDecisionForm, ToAnalysisForm
from .models import Act, ActAttachment, ActHistoryEvent, get_act_status
from .permissions import can_add_attachment, can_create_act, can_delete_attachment, can_download_attachment, can_view_act
from .services import (
    ActWorkflowError,
    add_act_comment,
    add_act_attachment,
    add_act_history_event,
    apply_ko_decision,
    apply_to_analysis,
    delete_act_attachment,
    format_file_size,
    get_available_act_actions,
    get_role_context_text,
    get_visible_acts_for_user,
    send_to_ko,
)


@login_required
def act_list(request):
    visible_acts = get_visible_acts_for_user(request.user)
    has_visible_acts = visible_acts.exists()
    today = timezone.localdate()

    status = request.GET.get('status')
    operation = request.GET.get('operation')
    defect_type = request.GET.get('defect_type')
    search = request.GET.get('search', '').strip()

    acts = visible_acts
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
    has_filters = bool(status or operation or defect_type or search)
    kpis = {
        'total': acts.count(),
        'overdue': acts.filter(due_date__lt=today).count(),
        'created_otk': acts.filter(status__code='CREATED_OTK').count(),
        'ko_review': acts.filter(status__code='KO_REVIEW').count(),
        'to_analysis': acts.filter(status__code='TO_ANALYSIS').count(),
    }

    context = {
        'active_page': 'acts',
        'page_title': 'Акты операционного контроля',
        'page_description': get_role_context_text(request.user),
        'acts': acts,
        'kpis': kpis,
        'today': today,
        'has_visible_acts': has_visible_acts,
        'has_filters': has_filters,
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
                add_act_history_event(
                    act,
                    request.user,
                    ActHistoryEvent.EventType.CREATED,
                    'Акт создан пользователем.',
                    to_status=act.status,
                )
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
    context = _get_act_detail_context(act, request.user)
    return render(request, 'acts/detail.html', context)


@login_required
def act_add_comment(request, pk):
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
    if request.method != 'POST':
        messages.error(request, 'Комментарий можно добавить только из формы на странице акта.')
        return redirect('acts:detail', pk=act.pk)

    form = ActCommentForm(request.POST)
    if form.is_valid():
        add_act_comment(act, request.user, form.cleaned_data['text'])
        messages.success(request, 'Комментарий добавлен.')
        return redirect('acts:detail', pk=act.pk)

    messages.error(request, 'Проверьте текст комментария.')
    context = _get_act_detail_context(act, request.user, comment_form=form)
    return render(request, 'acts/detail.html', context)


@login_required
def act_add_attachment(request, pk):
    act = _get_act_for_detail(pk)
    if not can_add_attachment(act, request.user):
        raise Http404('No Act matches the given query.')
    if request.method != 'POST':
        messages.error(request, 'Вложение можно добавить только из формы на странице акта.')
        return redirect('acts:detail', pk=act.pk)

    form = ActAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        add_act_attachment(
            act,
            request.user,
            form.cleaned_data['file'],
            form.cleaned_data.get('description', ''),
        )
        messages.success(request, 'Вложение добавлено.')
        return redirect('acts:detail', pk=act.pk)

    messages.error(request, 'Проверьте файл вложения.')
    context = _get_act_detail_context(act, request.user, attachment_form=form)
    return render(request, 'acts/detail.html', context)


@login_required
def act_download_attachment(request, pk, attachment_id):
    attachment = get_object_or_404(
        ActAttachment.objects.select_related('act', 'act__status', 'act__created_by', 'uploaded_by'),
        pk=attachment_id,
        act_id=pk,
    )
    if not can_download_attachment(attachment, request.user):
        raise Http404('No Act matches the given query.')
    if not attachment.file:
        raise Http404('Attachment file is missing.')

    return FileResponse(
        attachment.file.open('rb'),
        as_attachment=True,
        filename=attachment.original_name,
        content_type=attachment.content_type or 'application/octet-stream',
    )


@login_required
def act_delete_attachment(request, pk, attachment_id):
    attachment = get_object_or_404(
        ActAttachment.objects.select_related('act', 'act__status', 'act__created_by', 'uploaded_by'),
        pk=attachment_id,
        act_id=pk,
    )
    if request.method != 'POST':
        messages.error(request, 'Вложение можно удалить только подтверждённым действием.')
        return redirect('acts:detail', pk=attachment.act_id)
    if not can_view_act(attachment.act, request.user):
        raise Http404('No Act matches the given query.')
    if not can_delete_attachment(attachment, request.user):
        messages.error(request, 'Недостаточно прав для удаления вложения.')
        return redirect('acts:detail', pk=attachment.act_id)

    act_id = attachment.act_id
    try:
        delete_act_attachment(attachment, request.user)
    except ActWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Вложение удалено.')
    return redirect('acts:detail', pk=act_id)


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
    return _redirect_after_transition(act, request.user)


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
                return _redirect_after_transition(act, request.user)
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
                return _redirect_after_transition(act, request.user)
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


def _redirect_after_transition(act, user):
    if can_view_act(act, user):
        return redirect('acts:detail', pk=act.pk)
    return redirect('acts:list')


def _get_act_for_detail(pk):
    return get_object_or_404(
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


def _get_act_detail_context(act, user, comment_form=None, attachment_form=None):
    history_events = act.history_events.select_related('user', 'from_status', 'to_status')
    comments = act.comments.select_related('author')
    attachments = [
        {
            'object': attachment,
            'formatted_size': format_file_size(attachment.file_size),
            'can_delete': can_delete_attachment(attachment, user),
        }
        for attachment in act.attachments.select_related('uploaded_by')
    ]
    return {
        'active_page': 'acts',
        'act': act,
        'today': timezone.localdate(),
        'available_actions': get_available_act_actions(act, user),
        'history_events': history_events,
        'comments': comments,
        'comment_form': comment_form or ActCommentForm(),
        'attachments': attachments,
        'attachment_form': attachment_form or ActAttachmentForm(),
    }
