from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from ecosystem.attachments import format_file_size
from ecosystem.logging_utils import log_event
from realtime.auth import realtime_login_required
from smk.permissions import can_create_smk_task, requires_task_type_choice

from .forms import TaskAttachmentForm
from .models import Task, TaskAttachment
from .permissions import (
    can_complete_task,
    can_download_task_attachment,
    can_upload_task_attachment,
    get_readable_tasks_queryset,
    get_visible_tasks_queryset,
)
from .presentation import (
    describe_task_source,
    describe_task_state,
    describe_task_type,
    get_task_approval,
)
from .selectors import build_task_list_state
from .services import TaskWorkflowError, add_task_attachment, attachment_logger, complete_task


@login_required
def task_list(request):
    state = build_task_list_state(request.user, request.GET)
    return render(request, 'tasks/list.html', {
        'active_page': 'tasks', 'header_title': 'Задачи',
        # Whether «Создать задачу» is offered at all. The same answer the
        # chooser below re-asks, so the button and the endpoint cannot
        # disagree — the button is not the permission.
        'can_create_task': can_create_smk_task(request.user),
        **state,
    })


@login_required
def task_create(request):
    """Which kind of task to create — the step before any creation form.

    Most tasks are still produced by a workflow and have no form at all. The
    ones that do are listed here, and the list is built from the user's own
    rights: today it holds exactly one entry, «Задача СМК».

    An СМК employee has that single kind and nothing else, so they are taken
    straight to its form — a one-option menu is not a choice. Руководитель and
    администратор choose, because more kinds will be added under them.
    """
    if not can_create_smk_task(request.user):
        raise Http404('No task type is available.')
    options = [{
        'code': 'smk',
        'label': 'Задача СМК',
        'description': 'Корректирующие мероприятия по результатам аудита.',
        'url': reverse('smk:create'),
    }]
    if not requires_task_type_choice(request.user) and len(options) == 1:
        return redirect(options[0]['url'])
    return render(request, 'tasks/create.html', {
        'active_page': 'tasks', 'header_title': 'Создание задачи',
        'task_type_options': options,
    })


@realtime_login_required
@require_GET
def task_list_fragment(request):
    """Current registry results for the live client.

    Same builder, same partial and the same query parameters as the full page,
    so what the browser swaps in is exactly what a reload would render. The
    recipient is always `request.user`: no user parameter is accepted, and a
    GET never changes anything.
    """
    state = build_task_list_state(request.user, request.GET)
    results_html = render_to_string(
        'tasks/includes/list_results.html', state, request=request
    )
    response = JsonResponse(
        {
            'results_html': results_html,
            'tab': state['tab'],
            'task_ids': [row['task'].pk for row in state['rows']],
            'generated_at': timezone.now().isoformat(),
        }
    )
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response['Vary'] = 'Cookie'
    return response


@login_required
def task_detail(request, pk):
    """The ordinary task page — except for a routing queue entry.

    A `PROTOCOL_APPROVAL` and an `ACT_WORKFLOW` task are work-queue rows, not
    documents: the decision is «согласовать протокол» or «обработать акт», it
    is taken on that document's own page, and there is nothing here to
    complete. Reaching this URL by hand therefore lands on the source, exactly
    as clicking the row in the registry does.
    """
    task = get_object_or_404(get_readable_tasks_queryset(request.user), pk=pk)
    if task.source_type == Task.SourceType.PROTOCOL_APPROVAL and task.protocol_id:
        return redirect('protocols:detail', pk=task.protocol_id)
    if task.source_type == Task.SourceType.ACT_WORKFLOW and task.act_id:
        return redirect('acts:detail', pk=task.act_id)
    context = _task_detail_context(task, request.user, request.GET.urlencode())
    context['header_title'] = f'Задача {task.pk}'
    return render(request, 'tasks/detail.html', context)


def _task_attachment_cards(task):
    """Stored attachments, each with the size label the card shows."""
    return [
        {'object': attachment, 'formatted_size': format_file_size(attachment.file_size)}
        for attachment in task.attachments.select_related('uploaded_by')
    ]


def _task_detail_context(
    task, user, list_query='', execution_comment='', execution_error='',
    attachment_form=None,
):
    return {
        'active_page': 'tasks', 'header_title': f'Задача {task.pk}', 'task': task, 'today': timezone.localdate(),
        'can_complete': can_complete_task(task, user), 'list_query': list_query,
        'execution_comment': execution_comment, 'execution_error': execution_error,
        # Source-aware presentation, from the same helpers the registry uses.
        'task_type_label': describe_task_type(task),
        'task_source': describe_task_source(task),
        'task_state': describe_task_state(task),
        'task_approval': get_task_approval(task),
        # Attachments are their own card and their own form: uploading one is
        # never part of completing the task.
        'attachments': _task_attachment_cards(task),
        'can_upload_attachment': can_upload_task_attachment(task, user),
        'attachment_form': attachment_form or TaskAttachmentForm(),
    }


@login_required
def complete_task_view(request, pk):
    if request.method != 'POST':
        return redirect('tasks:detail', pk=pk)
    task = get_object_or_404(get_visible_tasks_queryset(request.user), pk=pk)
    execution_comment = request.POST.get('execution_comment', '')
    list_query = request.POST.get('list_query', '')
    try:
        complete_task(task, request.user, execution_comment)
    except TaskWorkflowError as exc:
        return render(
            request, 'tasks/detail.html',
            _task_detail_context(task, request.user, list_query, execution_comment, str(exc)), status=400,
        )
    return redirect(f"{reverse('tasks:list')}?tab=archive&number={task.pk}")


@login_required
def task_add_attachment(request, pk):
    """Upload one file to an ordinary task.

    Its own endpoint, deliberately separate from completion, and it stays that
    way now that a file can be a precondition: an ordinary task is still
    finished with the execution comment and nothing attached, but one created
    with `Task.requires_attachment` needs at least one attachment before
    `complete_task()` will close it. Uploading and completing remain two
    requests either way — nothing here checks or enforces the requirement, and
    the completion guard is the only authority on it.

    The task is loaded through the visible-tasks queryset and the permission is
    re-checked in the service under the row lock.
    """
    task = get_object_or_404(get_visible_tasks_queryset(request.user), pk=pk)
    if request.method != 'POST':
        return redirect('tasks:detail', pk=pk)
    list_query = request.POST.get('list_query', '')
    form = TaskAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            add_task_attachment(task, request.user, form.cleaned_data['file'])
        except TaskWorkflowError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Вложение добавлено.')
        return redirect(f"{reverse('tasks:detail', args=[task.pk])}"
                        f"{'?' + list_query if list_query else ''}")
    messages.error(request, 'Проверьте файл вложения.')
    context = _task_detail_context(task, request.user, list_query, attachment_form=form)
    return render(request, 'tasks/detail.html', context, status=400)


@login_required
def task_download_attachment(request, pk, attachment_id):
    """Protected media: the file is served only after the task is re-checked.

    The row is scoped to the task in the URL, so a valid attachment id from
    another task is a 404 rather than a download, and a denial and a missing
    file look the same.
    """
    attachment = get_object_or_404(
        TaskAttachment.objects.select_related('task', 'task__status', 'uploaded_by'),
        pk=attachment_id,
        task_id=pk,
    )
    if not can_download_task_attachment(attachment, request.user):
        # Ids only — never the file's name, its path or its content type.
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.access_denied',
            attachment_id=attachment.pk,
            task_id=attachment.task_id,
            user_id=getattr(request.user, 'pk', None),
            operation='download',
            outcome='denied',
        )
        raise Http404('No Task matches the given query.')
    if not attachment.file:
        raise Http404('Attachment file is missing.')
    try:
        handle = attachment.file.open('rb')
    except OSError as exc:
        log_event(
            attachment_logger,
            'ERROR',
            'attachment.storage_failed',
            attachment_id=attachment.pk,
            task_id=attachment.task_id,
            user_id=request.user.pk,
            operation='download',
            error_type=type(exc).__name__,
            outcome='failed',
        )
        raise Http404('Attachment file is missing.') from exc
    log_event(
        attachment_logger,
        'INFO',
        'attachment.downloaded',
        attachment_id=attachment.pk,
        task_id=attachment.task_id,
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
