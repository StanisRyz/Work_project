from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.forms.utils import ErrorList
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from references.models import ActStatus, DefectType, Operation
from accounts.models import Department

from .forms import (
    ActAttachmentForm,
    ActCloseForm,
    ActCommentForm,
    ActCreateForm,
    ActDefectFormSet,
    ActDefectEditFormSet,
    ActDefectKoDecisionFormSet,
    KoDecisionForm,
    ReturnToOtkForm,
    ToAnalysisStructureForm,
)
from .models import Act, ActAttachment, ActHistoryEvent, get_act_status
from .permissions import can_add_attachment, can_clear_all_acts, can_close_act, can_create_act, can_delete_attachment, can_download_attachment, can_edit_act, can_view_act, get_archived_acts_queryset, has_full_act_access, is_act_admin
from .services import (
    ActWorkflowError,
    add_act_comment,
    add_act_attachment,
    add_act_history_event,
    apply_ko_decision,
    apply_structured_to_analysis,
    close_act,
    clear_all_acts,
    delete_act_attachment,
    format_file_size,
    get_available_act_actions,
    get_role_context_text,
    get_visible_acts_for_user,
    return_to_otk,
    return_to_ko,
    return_to_to,
    approve_act,
    send_to_ko,
)


@login_required
def act_list(request):
    scope = request.GET.get('scope', 'my')
    if scope not in {'my', 'all', 'archive'}:
        scope = 'my'
    active_acts = get_visible_acts_for_user(request.user)
    archived_acts = get_archived_acts_queryset(request.user)
    if scope == 'archive':
        visible_acts = archived_acts
    elif scope == 'all' and has_full_act_access(request.user):
        visible_acts = active_acts.model.objects.select_related(
            'created_by', 'operation', 'defect_type', 'priority', 'status'
        ).exclude(status__code='ARCHIVED')
    else:
        visible_acts = active_acts
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
    acts = acts.annotate(defects_total=Count('defects'))

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
            'scope': scope,
            'status': status or '',
            'operation': operation or '',
            'defect_type': defect_type or '',
            'search': search,
        },
        'can_create': can_create_act(request.user),
        'can_clear_all_acts': can_clear_all_acts(request.user),
        'is_act_admin': is_act_admin(request.user),
        'scope': scope,
    }
    return render(request, 'acts/list.html', context)


@login_required
def act_clear_all(request):
    if not can_clear_all_acts(request.user):
        raise Http404('No Act matches the given query.')
    if request.method != 'POST':
        messages.error(request, 'Очистка актов требует подтверждённого действия.')
        return redirect('acts:list')

    deleted_count = clear_all_acts()
    messages.success(request, f'Удалено актов: {deleted_count}.')
    return redirect('acts:list')


@login_required
def act_create(request):
    if not can_create_act(request.user):
        messages.error(request, 'Недостаточно прав для создания акта.')
        return redirect('acts:list')

    if request.method == 'POST':
        form = ActCreateForm(request.POST)
        defect_formset = ActDefectFormSet(request.POST)
        if form.is_valid() and defect_formset.is_valid():
            act = form.save(commit=False)
            act.created_by = request.user
            defect_forms = [
                defect_form
                for defect_form in defect_formset.forms
                if defect_form.cleaned_data and not defect_form.cleaned_data.get('DELETE', False)
            ]
            first_defect = defect_forms[0].cleaned_data
            act.operation = first_defect['operation']
            act.znp_number = first_defect['znp_number']
            act.party_number = first_defect['party_number']
            act.defect_type = first_defect['defect_type']
            act.description = first_defect['description']
            act.due_date = first_defect['detected_at']
            try:
                with transaction.atomic():
                    act.status = get_act_status('CREATED_OTK')
                    act.save()
                    defect_formset.instance = act
                    defect_formset.save()
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
            messages.error(request, 'Проверьте данные формы создания акта.')
    else:
        form = ActCreateForm()
        defect_formset = ActDefectFormSet()

    return render(
        request,
        'acts/form.html',
        {
            'active_page': 'acts',
            'header_title': 'Создание акта',
            'form': form,
            'defect_formset': defect_formset,
        },
    )


@login_required
def act_detail(request, pk):
    act = _get_act_for_detail(pk)
    if not can_view_act(act, request.user):
        raise Http404('No Act matches the given query.')
    context = _get_act_detail_context(act, request.user, detail_tab=_get_detail_tab(request.GET.get('tab')))
    return render(request, 'acts/detail.html', context)


@login_required
def act_edit(request, pk):
    act = _get_act_for_detail(pk)
    if not can_view_act(act, request.user) or not can_edit_act(act, request.user):
        raise Http404('No Act matches the given query.')

    if request.method == 'POST':
        form = ActCreateForm(request.POST, instance=act)
        defect_formset = ActDefectEditFormSet(request.POST, instance=act)
        if form.is_valid() and defect_formset.is_valid():
            with transaction.atomic():
                act = form.save(commit=False)
                defect_formset.save()
                first_defect = act.defects.select_related(
                    'defect_type', 'operation'
                ).order_by('created_at', 'pk').first()
                act.operation = first_defect.operation
                act.znp_number = first_defect.znp_number
                act.party_number = first_defect.party_number
                act.defect_type = first_defect.defect_type
                act.description = first_defect.description
                act.due_date = first_defect.detected_at
                act.save()
                add_act_history_event(
                    act,
                    request.user,
                    ActHistoryEvent.EventType.ACT_EDITED,
                    'Акт отредактирован до передачи в КО.',
                )
            messages.success(request, 'Акт сохранён.')
            return _redirect_to_detail_tab(act, 'work')
        messages.error(request, 'Проверьте данные формы редактирования акта.')
    else:
        form = ActCreateForm(instance=act)
        defect_formset = ActDefectEditFormSet(instance=act)

    return render(
        request,
        'acts/form.html',
        {
            'active_page': 'acts',
            'header_title': f'Редактирование акта {act.number}',
            'form': form,
            'defect_formset': defect_formset,
            'act': act,
            'is_edit': True,
        },
    )


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
        return _redirect_to_detail_tab(act, 'attachments')

    form = ActCommentForm(request.POST)
    if form.is_valid():
        add_act_comment(act, request.user, form.cleaned_data['text'])
        messages.success(request, 'Комментарий добавлен.')
        return _redirect_to_detail_tab(act, 'attachments')

    messages.error(request, 'Проверьте текст комментария.')
    context = _get_act_detail_context(act, request.user, comment_form=form, detail_tab='attachments')
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
        return _redirect_to_detail_tab(act, 'attachments')

    messages.error(request, 'Проверьте файл вложения.')
    context = _get_act_detail_context(act, request.user, attachment_form=form, detail_tab='attachments')
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
def act_close(request, pk):
    act = _get_act_for_detail(pk)
    if not can_view_act(act, request.user):
        raise Http404('No Act matches the given query.')
    if not can_close_act(act, request.user):
        messages.error(request, 'Закрытие этого акта недоступно.')
        return redirect('acts:detail', pk=act.pk)

    if request.method == 'POST':
        form = ActCloseForm(request.POST, instance=act)
        if form.is_valid():
            try:
                close_act(act, request.user, form.cleaned_data.get('closing_comment', ''))
            except ActWorkflowError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, 'Акт закрыт.')
                return _redirect_after_transition(act, request.user)
    else:
        form = ActCloseForm(instance=act)

    return render(
        request,
        'acts/close.html',
        {
            'active_page': 'acts',
            'act': act,
            'form': form,
        },
    )


@login_required
def act_print(request, pk):
    act = _get_act_for_detail(pk)
    if not can_view_act(act, request.user):
        raise Http404('No Act matches the given query.')

    return render(
        request,
        'acts/print.html',
        {
            'act': act,
            'attachments': act.attachments.select_related('uploaded_by'),
            'history_events': act.history_events.select_related('user', 'from_status', 'to_status')[:20],
        },
    )


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
        if can_view_act(act, request.user):
            messages.success(request, 'Акт передан в КО.')
        else:
            messages.success(request, 'Акт передан в КО и больше не отображается в вашей очереди ОТК.')
    return _redirect_after_transition(act, request.user)


@login_required
def act_ko_decision(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')

    if not get_available_act_actions(act, request.user)['ko_decision']:
        messages.error(request, 'Решение КО для этого акта недоступно.')
        return _redirect_to_detail_tab(act, 'work')

    defects = act.defects.select_related('defect_type')
    if defects.exists():
        formset = ActDefectKoDecisionFormSet(request.POST, queryset=defects)
        is_valid = formset.is_valid()
        defect_decisions = [
            (form.instance, form.cleaned_data['ko_decision'], form.cleaned_data['ko_comment'])
            for form in formset.forms
        ] if is_valid else []
        form = None
    else:
        form = KoDecisionForm(request.POST, instance=act)
        is_valid = form.is_valid()
        defect_decisions = [(None, form.cleaned_data['ko_decision'], form.cleaned_data['ko_comment'])] if is_valid else []
        formset = None
    if is_valid:
        try:
            act = apply_ko_decision(act, request.user, defect_decisions)
        except ActWorkflowError as exc:
            if formset is not None:
                formset._non_form_errors = ErrorList([str(exc)])
            else:
                form.add_error(None, str(exc))
        else:
            messages.success(request, 'Решения КО сохранены. Акт передан в ТО.')
            return _redirect_after_transition(act, request.user)

    context = _get_act_detail_context(
        act, request.user, ko_decision_form=form, ko_decision_formset=formset, detail_tab='work'
    )
    return render(request, 'acts/detail.html', context)


@login_required
def act_return_to_otk(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')
    form = ReturnToOtkForm(request.POST)
    if form.is_valid():
        try:
            return_to_otk(act, request.user, form.cleaned_data['comment'])
        except ActWorkflowError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, 'Акт возвращён в ОТК на доработку.')
            return _redirect_after_transition(act, request.user)

    context = _get_act_detail_context(
        act,
        request.user,
        detail_tab='work',
        return_to_otk_form=form,
        return_dialog_open=True,
    )
    return render(request, 'acts/detail.html', context)


@login_required
def act_to_analysis(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')

    form = ToAnalysisStructureForm(request.POST)
    if request.POST.get('action') != 'send_to_otk':
        form.non_field_errors.append('Выберите действие для анализа ТО.')
    elif form.is_valid():
        try:
            apply_structured_to_analysis(act, request.user, form.analysis_data)
        except ActWorkflowError as exc:
            form.non_field_errors.append(str(exc))
        else:
            messages.success(request, 'Анализ ТО сохранен.')
            return _redirect_after_transition(act, request.user)

    context = _get_act_detail_context(act, request.user, detail_tab='work', to_analysis_form=form)
    return render(request, 'acts/detail.html', context)


@login_required
def act_return_to_ko(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')

    form = ReturnToOtkForm(request.POST)
    if form.is_valid():
        try:
            return_to_ko(act, request.user, form.cleaned_data['comment'])
        except ActWorkflowError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, 'Акт возвращён в КО на доработку.')
            return _redirect_after_transition(act, request.user)

    context = _get_act_detail_context(
        act,
        request.user,
        detail_tab='work',
        return_to_ko_form=form,
        return_to_ko_dialog_open=True,
    )
    return render(request, 'acts/detail.html', context)


@login_required
def act_return_to_to(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')
    form = ReturnToOtkForm(request.POST)
    if form.is_valid():
        try:
            return_to_to(act, request.user, form.cleaned_data['comment'])
        except ActWorkflowError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, 'Акт возвращён в ТО на доработку.')
            return _redirect_after_transition(act, request.user)
    context = _get_act_detail_context(
        act, request.user, detail_tab='work', return_to_to_form=form, return_to_to_dialog_open=True
    )
    return render(request, 'acts/detail.html', context)


@login_required
def act_approve(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')
    try:
        approve_act(act, request.user)
    except ActWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Акт утверждён и перемещён в архив.')
    return _redirect_after_transition(act, request.user)


def _redirect_after_transition(act, user):
    if can_view_act(act, user):
        return redirect('acts:detail', pk=act.pk)
    return redirect('acts:list')


def _get_detail_tab(tab):
    return tab if tab in {'work', 'history', 'attachments'} else 'work'


def _redirect_to_detail_tab(act, tab):
    return redirect(f"{reverse('acts:detail', args=[act.pk])}?tab={_get_detail_tab(tab)}")


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
            'closed_by',
        ).prefetch_related('defects__defect_type', 'defects__operation'),
        pk=pk,
    )


def _get_act_detail_context(
    act,
    user,
    comment_form=None,
    attachment_form=None,
    ko_decision_form=None,
    ko_decision_formset=None,
    detail_tab='work',
    return_to_otk_form=None,
    return_dialog_open=False,
    to_analysis_form=None,
    return_to_ko_form=None,
    return_to_ko_dialog_open=False,
    return_to_to_form=None,
    return_to_to_dialog_open=False,
):
    history_events = act.history_events.select_related('user', 'from_status', 'to_status')
    comments = act.comments.select_related('author')
    defect_rows = list(act.defects.select_related('defect_type', 'operation'))
    has_defect_records = bool(defect_rows)
    if not defect_rows:
        defect_rows = [
            {
                'defect_type': act.defect_type,
                'operation': act.operation,
                'znp_number': act.znp_number,
                'party_number': act.party_number,
                'checked_quantity': None,
                'nonconforming_quantity': None,
                'description': act.description,
                'detected_at': act.due_date,
            }
        ]
    if ko_decision_formset is None and has_defect_records:
        ko_decision_formset = ActDefectKoDecisionFormSet(
            queryset=act.defects.select_related('defect_type')
        )
    if ko_decision_formset is not None:
        for field in ko_decision_formset.management_form.fields.values():
            field.widget.attrs['form'] = 'ko-decision-form'
        ko_forms = list(ko_decision_formset)
        for form in ko_forms:
            for field in form.fields.values():
                field.widget.attrs['form'] = 'ko-decision-form'
    else:
        ko_forms = []
    if ko_decision_form is None:
        ko_decision_form = KoDecisionForm(instance=act)
    for field in ko_decision_form.fields.values():
        field.widget.attrs['form'] = 'ko-decision-form'
    defect_decision_rows = [
        {
            'defect': defect,
            'ko_form': ko_forms[index] if index < len(ko_forms) else None,
        }
        for index, defect in enumerate(defect_rows)
    ]
    attachments = [
        {
            'object': attachment,
            'formatted_size': format_file_size(attachment.file_size),
            'can_delete': can_delete_attachment(attachment, user),
        }
        for attachment in act.attachments.select_related('uploaded_by')
    ]
    root_analyses = list(
        act.root_analyses.prefetch_related(
            'corrective_actions__department',
            'corrective_actions__assignees__user__userprofile',
            'corrective_actions__task__status',
            'corrective_actions__task__assignees__user__userprofile',
            'corrective_actions__task__completed_by',
        )
    )
    return {
        'active_page': 'acts',
        'header_title': act.number,
        'act': act,
        'today': timezone.localdate(),
        'detail_tab': _get_detail_tab(detail_tab),
        'available_actions': get_available_act_actions(act, user),
        'defect_rows': defect_rows,
        'defect_decision_rows': defect_decision_rows,
        'has_defect_records': has_defect_records,
        'history_events': history_events,
        'comments': comments,
        'comment_form': comment_form or ActCommentForm(),
        'return_to_otk_form': return_to_otk_form or ReturnToOtkForm(),
        'return_dialog_open': return_dialog_open,
        'return_to_ko_form': return_to_ko_form or ReturnToOtkForm(),
        'return_to_ko_dialog_open': return_to_ko_dialog_open,
        'return_to_to_form': return_to_to_form or ReturnToOtkForm(),
        'return_to_to_dialog_open': return_to_to_dialog_open,
        'to_analysis_form': to_analysis_form or ToAnalysisStructureForm(root_analyses=root_analyses),
        'root_analyses': root_analyses,
        'analysis_departments': Department.objects.filter(is_active=True),
        'analysis_users': User.objects.filter(
            is_active=True, userprofile__is_active=True
        ).select_related('userprofile').order_by('username'),
        'ko_decision_form': ko_decision_form,
        'ko_decision_formset': ko_decision_formset,
        'attachments': attachments,
        'attachment_form': attachment_form or ActAttachmentForm(),
    }
