from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms.utils import ErrorList
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from accounts.models import Department
from ecosystem.logging_utils import log_event
from realtime.auth import realtime_login_required
from realtime.emitters import emit_act_created

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
from .permissions import can_add_attachment, can_clear_all_acts, can_close_act, can_contribute_to_act, can_create_act, can_delete_attachment, can_download_attachment, can_edit_act, can_view_act
from .selectors import (
    build_act_list_state,
    build_route_steps,
    get_act_comments,
    get_history_events,
    get_related_tasks,
    group_history_events,
)
from .services import (
    ActWorkflowError,
    attachment_logger,
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
    get_visible_acts_for_user,
    lock_act_for_update,
    return_to_otk,
    return_to_ko,
    return_to_to,
    approve_act,
    send_to_ko,
)


@login_required
def act_list(request):
    state = build_act_list_state(request.user, request.GET)
    return render(request, 'acts/list.html', {
        'active_page': 'acts', 'header_title': 'Акты', **state,
    })


@realtime_login_required
@require_GET
def act_list_fragment(request):
    """Current registry KPIs and results for the live client.

    Same builder, same partials and the same query parameters as the full page.
    The scope and every filter are re-evaluated server-side for `request.user`;
    no user parameter is accepted and a GET never changes anything.
    """
    state = build_act_list_state(request.user, request.GET)
    return _fragment_response(
        {
            'kpis_html': render_to_string(
                'acts/includes/registry_kpis.html', state, request=request
            ),
            'results_html': render_to_string(
                'acts/includes/registry_results.html', state, request=request
            ),
            'act_ids': list(state['acts'].values_list('pk', flat=True)),
        }
    )


def _fragment_response(payload):
    """JSON fragment response: never cached, always scoped to the session."""
    response = JsonResponse({**payload, 'generated_at': timezone.now().isoformat()})
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response['Vary'] = 'Cookie'
    return response


def _get_live_act(request, pk):
    """Re-load the act and re-check visibility for every live fragment.

    A user who has lost access gets a plain 404 with no object data at all —
    not a hint that the act exists.
    """
    act = _get_act_for_detail(pk)
    if not can_view_act(act, request.user):
        raise Http404('No Act matches the given query.')
    return act


@realtime_login_required
@require_GET
def act_live_summary_fragment(request, pk):
    act = _get_live_act(request, pk)
    return _fragment_response(
        {
            'html': render_to_string(
                'acts/includes/live_summary.html',
                {'act': act, 'route_steps': build_route_steps(act)},
                request=request,
            ),
            'status_code': act.status.code,
        }
    )


@realtime_login_required
@require_GET
def act_history_fragment(request, pk):
    act = _get_live_act(request, pk)
    history_events = get_history_events(act)
    return _fragment_response(
        {
            'html': render_to_string(
                'acts/includes/history_content.html',
                {'act': act, 'history_groups': group_history_events(history_events)},
                request=request,
            )
        }
    )


@realtime_login_required
@require_GET
def act_comments_fragment(request, pk):
    act = _get_live_act(request, pk)
    return _fragment_response(
        {
            # The list only: the new-comment textarea is never part of this
            # partial, so a refresh cannot discard what the user is typing.
            'html': render_to_string(
                'acts/includes/comments_list.html',
                {'act': act, 'comments': get_act_comments(act)},
                request=request,
            )
        }
    )


@realtime_login_required
@require_GET
def act_work_fragment(request, pk):
    """The «Проработка» tab, rebuilt by the very same context builder.

    Forms, read-only data and the available workflow actions all come from
    `_get_act_detail_context`, so no permission or workflow logic is duplicated
    here and the fragment can never offer an action the page would not.
    """
    act = _get_live_act(request, pk)
    context = _get_act_detail_context(act, request.user, detail_tab='work')
    return _fragment_response(
        {
            'html': render_to_string(
                'acts/includes/work_content.html', context, request=request
            ),
            'status_code': act.status.code,
        }
    )


@realtime_login_required
@require_GET
def act_activities_fragment(request, pk):
    act = _get_live_act(request, pk)
    return _fragment_response(
        {
            'html': render_to_string(
                'acts/includes/activities_content.html',
                {'act': act, 'related_tasks': get_related_tasks(act, request.user)},
                request=request,
            )
        }
    )


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
            # Summary copy of the first defect. Fields the ПиР workshop does
            # not collect stay empty instead of being filled with placeholders.
            first_defect = defect_forms[0].cleaned_data
            act.operation = first_defect.get('operation')
            act.znp_number = first_defect.get('znp_number', '')
            act.party_number = first_defect.get('party_number') or ''
            act.defect_type = first_defect['defect_type']
            act.description = first_defect.get('description') or ''
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
                    # Inside the atomic block: a rollback or a failed
                    # validation therefore publishes nothing.
                    emit_act_created(act)
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
            try:
                with transaction.atomic():
                    # Re-read and lock the act before writing: it may have been
                    # transferred out of CREATED_OTK since this form was opened,
                    # and a full save would otherwise revert that transition.
                    locked_act = lock_act_for_update(act)
                    if not can_view_act(locked_act, request.user) or not can_edit_act(
                        locked_act, request.user
                    ):
                        raise ActWorkflowError(
                            'Акт уже передан дальше по маршруту, редактирование недоступно.'
                        )
                    act = form.save(commit=False)
                    act.status = locked_act.status
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
                    # `form.save(commit=False)` already resolved the business
                    # number, including keeping a legacy one untouched.
                    act.save()
                    add_act_history_event(
                        act,
                        request.user,
                        ActHistoryEvent.EventType.ACT_EDITED,
                        'Акт отредактирован до передачи в КО.',
                    )
            except ActWorkflowError as exc:
                messages.error(request, str(exc))
                return _redirect_to_detail_tab(act, 'work')
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
    if not can_contribute_to_act(act, request.user):
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
        # A refused download of protected production data is worth a line: it
        # is either a stale link or somebody probing. Ids only — never the
        # file's name, its path or its content type.
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.access_denied',
            attachment_id=attachment.pk,
            act_id=attachment.act_id,
            user_id=request.user.pk,
            operation='download',
            outcome='denied',
        )
        raise Http404('No Act matches the given query.')
    if not attachment.file:
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.storage_failed',
            attachment_id=attachment.pk,
            act_id=attachment.act_id,
            user_id=request.user.pk,
            operation='download',
            outcome='missing_file',
        )
        raise Http404('Attachment file is missing.')

    try:
        handle = attachment.file.open('rb')
    except OSError as exc:
        log_event(
            attachment_logger,
            'ERROR',
            'attachment.storage_failed',
            attachment_id=attachment.pk,
            act_id=attachment.act_id,
            user_id=request.user.pk,
            operation='download',
            error_type=type(exc).__name__,
            outcome='failed',
            exc_info=True,
        )
        raise Http404('Attachment file is missing.') from exc

    log_event(
        attachment_logger,
        'INFO',
        'attachment.downloaded',
        attachment_id=attachment.pk,
        act_id=attachment.act_id,
        user_id=request.user.pk,
        size_bytes=attachment.file_size,
        operation='download',
        outcome='ok',
    )
    return FileResponse(
        handle,
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
        deleted = delete_act_attachment(attachment, request.user)
    except ActWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        if deleted:
            messages.success(request, 'Вложение удалено.')
        else:
            messages.info(request, 'Вложение уже удалено.')
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
                act = close_act(act, request.user, form.cleaned_data.get('closing_comment', ''))
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

    defects = list(
        act.defects.select_related('defect_type', 'operation').order_by('created_at', 'pk')
    )

    return render(
        request,
        'acts/print.html',
        {
            'act': act,
            'defects': defects,
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
        act = send_to_ko(act, request.user)
    except ActWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        if can_contribute_to_act(act, request.user):
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


def _handle_return_transition(request, pk, apply_return, success_message):
    """Shared POST flow of the three return transitions.

    The confirmation modal is generic and holds no server state, so a rejected
    return reports through `messages` and sends the user back to the work tab
    instead of re-rendering a per-action dialog. Role, status and the mandatory
    comment stay enforced by the form and the service under lock.
    """
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')

    form = ReturnToOtkForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Укажите комментарий к возврату.')
        return _redirect_to_detail_tab(act, 'work')

    try:
        act = apply_return(act, request.user, form.cleaned_data['comment'])
    except ActWorkflowError as exc:
        messages.error(request, str(exc))
        return _redirect_to_detail_tab(act, 'work')

    messages.success(request, success_message)
    return _redirect_after_transition(act, request.user)


@login_required
def act_return_to_otk(request, pk):
    return _handle_return_transition(
        request, pk, return_to_otk, 'Акт возвращён в ОТК на доработку.'
    )


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
            act = apply_structured_to_analysis(act, request.user, form.analysis_data)
        except ActWorkflowError as exc:
            form.non_field_errors.append(str(exc))
        else:
            messages.success(request, 'Анализ ТО сохранен.')
            return _redirect_after_transition(act, request.user)

    context = _get_act_detail_context(act, request.user, detail_tab='work', to_analysis_form=form)
    return render(request, 'acts/detail.html', context)


@login_required
def act_return_to_ko(request, pk):
    return _handle_return_transition(
        request, pk, return_to_ko, 'Акт возвращён в КО на доработку.'
    )


@login_required
def act_return_to_to(request, pk):
    return _handle_return_transition(
        request, pk, return_to_to, 'Акт возвращён в ТО на доработку.'
    )


@login_required
def act_approve(request, pk):
    act = get_object_or_404(get_visible_acts_for_user(request.user), pk=pk)
    if request.method != 'POST':
        return _redirect_to_detail_tab(act, 'work')
    try:
        act = approve_act(act, request.user)
    except ActWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Акт утверждён и перемещён в архив.')
    return _redirect_after_transition(act, request.user)


def _redirect_after_transition(act, user):
    if can_contribute_to_act(act, user):
        return redirect('acts:detail', pk=act.pk)
    return redirect('acts:list')


def _get_detail_tab(tab):
    return tab if tab in {'work', 'history', 'attachments', 'activities'} else 'work'


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
    to_analysis_form=None,
):
    # Shared with the live fragments, so a refreshed block and a reload can
    # never disagree.
    history_events = get_history_events(act)
    history_groups = group_history_events(history_events)
    comments = get_act_comments(act)
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
    related_tasks = get_related_tasks(act, user)
    return {
        'active_page': 'acts',
        'header_title': '',
        'act': act,
        'today': timezone.localdate(),
        'detail_tab': _get_detail_tab(detail_tab),
        'available_actions': get_available_act_actions(act, user),
        'defect_rows': defect_rows,
        'defect_decision_rows': defect_decision_rows,
        'has_defect_records': has_defect_records,
        'history_events': history_events,
        'history_groups': history_groups,
        'comments': comments,
        'can_contribute': can_contribute_to_act(act, user),
        'comment_form': comment_form or ActCommentForm(),
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
        'related_tasks': related_tasks,
        'route_steps': build_route_steps(act),
    }
