from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.templatetags.people import person_name
from ecosystem.xlsx import xlsx_response
from ecosystem.logging_utils import log_event
from realtime.auth import realtime_login_required
from smk.permissions import can_create_smk_task, requires_task_type_choice

from .drafts import remember_execution_draft, take_execution_draft
from .forms import TaskAttachmentForm
from .models import Task, TaskAttachment
from .permissions import (
    can_complete_task,
    can_delete_task_attachment,
    can_download_task_attachment,
    can_reopen_task,
    can_upload_task_attachment,
    get_readable_tasks_queryset,
    get_visible_tasks_queryset,
)
from .presentation import (
    board_card_url,
    describe_task_source,
    describe_task_state,
    describe_task_type,
    get_task_approval,
    task_attachment_cards,
)
from .selectors import build_task_list_state
from .services import (
    TaskWorkflowError,
    add_task_attachment,
    attachment_logger,
    complete_task,
    delete_task_attachment,
    reopen_task,
)


@login_required
def task_list(request):
    state = build_task_list_state(request.user, request.GET)
    if request.GET.get('export') == 'xlsx':
        return _export_task_registry(state)
    return render(request, 'tasks/list.html', {
        'active_page': 'tasks', 'header_title': 'Задачи',
        # Whether «Создать задачу» is offered at all. The same answer the
        # chooser below re-asks, so the button and the endpoint cannot
        # disagree — the button is not the permission.
        'can_create_task': can_create_smk_task(request.user),
        **state,
    })


def _export_task_registry(state):
    """The registry exactly as the page shows it: the same rows, the same labels."""
    rows = [
        [
            row['task'].pk,
            row['task'].task_text,
            ', '.join(person_name(a.user) for a in row['task'].assignees.all()),
            row['type_label'],
            row['source']['label'],
            row['state']['label'],
            row['task'].due_date,
        ]
        for row in state['rows']
    ]
    return xlsx_response(
        'tasks', 'Задачи',
        ['№', 'Что сделать', 'Исполнители', 'Тип задачи', 'Источник', 'Статус', 'Срок'],
        rows,
    )


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
    # «Ознакомиться» and «Согласовать документ» are answered on the document.
    if task.is_routing_task and task.is_document_task and task.document_version_id:
        return redirect('documents:document_detail', task.document_version.document_id)
    # A board card is worked on its board. Not a routing entry — it takes
    # files exactly like any task — only shown elsewhere. `?tab=files` (where
    # an attachment request comes back to) opens the panel on its files.
    if task.is_board_task and task.board_card_id:
        tab = request.GET.get('tab', '')
        return redirect(board_card_url(task, tab if tab.isalpha() else ''))
    context = _task_detail_context(
        task, request.user, request.GET.urlencode(),
        # The parked upload draft first, then whatever the task already holds.
        # For an ordinary open task the second is empty; for one an
        # administrator has just reopened it is the result written before the
        # task was closed, put back into the field so the исполнитель corrects
        # it instead of retyping it. Reading it is all that happens here —
        # `complete_task()` is still the only writer of that column.
        execution_comment=take_execution_draft(request, task) or task.execution_comment,
    )
    context['header_title'] = f'Задача №{task.pk}'
    return render(request, 'tasks/detail.html', context)


def _task_detail_context(
    task, user, list_query='', execution_comment='', execution_error='',
    attachment_form=None,
):
    return {
        'active_page': 'tasks', 'header_title': f'Задача №{task.pk}', 'task': task, 'today': timezone.localdate(),
        'can_complete': can_complete_task(task, user), 'list_query': list_query,
        # «Вернуть в работу», offered only to an administrator and only for a
        # completed ordinary task. Never both this and `can_complete` — the two
        # permissions are exact opposites on the task's status.
        'can_reopen': can_reopen_task(task, user),
        'execution_comment': execution_comment, 'execution_error': execution_error,
        # Source-aware presentation, from the same helpers the registry uses.
        'task_type_label': describe_task_type(task),
        'task_source': describe_task_source(task),
        'task_state': describe_task_state(task),
        'task_approval': get_task_approval(task),
        # Attachments are their own card and their own form: uploading one is
        # never part of completing the task.
        'attachments': task_attachment_cards(task, user),
        'can_upload_attachment': can_upload_task_attachment(task, user),
        'attachment_form': attachment_form or TaskAttachmentForm(),
    }


# A `BOARD` task is completed and reopened on its board, never here: the
# board's routes (`boards:card_complete`, `boards:card_reopen`) are what write
# the card's journal, and a task closed behind the board's back would leave a
# gap in it.
BOARD_TASK_REFUSAL = 'Карточку доски завершают и возвращают на доске.'


def _to_board_card(request, task):
    """`tasks:complete`/`tasks:reopen` of a `BOARD` task: nothing done, the card opened."""
    messages.info(request, BOARD_TASK_REFUSAL)
    return redirect(board_card_url(task))


@login_required
def complete_task_view(request, pk):
    if request.method != 'POST':
        return redirect('tasks:detail', pk=pk)
    task = get_object_or_404(get_visible_tasks_queryset(request.user), pk=pk)
    if task.is_board_task and task.board_card_id:
        return _to_board_card(request, task)
    execution_comment = request.POST.get('execution_comment', '')
    list_query = request.POST.get('list_query', '')
    try:
        complete_task(task, request.user, execution_comment)
    except TaskWorkflowError as exc:
        return render(
            request, 'tasks/detail.html',
            _task_detail_context(task, request.user, list_query, execution_comment, str(exc)), status=400,
        )
    return _redirect_to_next_task(request, task, list_query)


def _back_to_board_card(request, task, error):
    """A refused upload on a `BOARD` task: the message goes to the card's files.

    The task page is never drawn for a board task, so where it would come back
    with the error beside the form, the board does instead. No «Выполнение»
    draft travels: the board's result is typed into «Завершить» in the panel's
    heading, and no attachment form carries it.
    """
    messages.error(request, error)
    return redirect(board_card_url(task, 'files'))


def _redirect_to_next_task(request, done_task, list_query):
    """After «Завершить задачу»: the next open task of «Мои задачи».

    The queue is `build_task_list_state()` itself, tab «Мои задачи», so «next»
    means what the registry shows first — overdue, then nearest deadline — and
    never a task this user could not open. With nothing left the registry
    opens instead, and a message says the work is done either way.
    """
    params = QueryDict(mutable=True)
    params['tab'] = 'my'
    queue = build_task_list_state(request.user, params)['tasks']
    upcoming = queue.exclude(pk=done_task.pk).first()
    if upcoming is None:
        messages.success(
            request, f'Задача №{done_task.pk} выполнена. Других задач в работе у вас нет.'
        )
        return redirect(f"{reverse('tasks:list')}?tab=my")
    messages.success(
        request,
        f'Задача №{done_task.pk} выполнена. Открыта следующая из «Мои задачи» — №{upcoming.pk}.',
    )
    return redirect(f"{reverse('tasks:detail', args=[upcoming.pk])}?tab=my")


@login_required
@require_POST
def task_reopen(request, pk):
    """«Вернуть в работу» — POST only, administrators only.

    The view confirms nothing itself: the modal in the browser is the fast path
    to this POST, and `reopen_task()` re-checks the right under the task's row
    lock. A refusal comes back as a message on the task, which stays readable —
    reopening hides nothing, exactly as archiving an СМК record hides nothing.

    The task is loaded through the *readable* queryset rather than the visible
    one: an administrator is not an исполнитель of the tasks they correct.
    """
    task = get_object_or_404(get_readable_tasks_queryset(request.user), pk=pk)
    if task.is_board_task and task.board_card_id:
        return _to_board_card(request, task)
    list_query = request.POST.get('list_query', '')
    try:
        reopen_task(task, request.user)
    except TaskWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Задача возвращена в работу.')
    return redirect(f"{reverse('tasks:detail', args=[task.pk])}"
                    f"{'?' + list_query if list_query else ''}")


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
    # What the user had already typed into «Выполнение» when they picked the
    # file. The upload form carries it as a hidden field precisely so this
    # round trip does not discard it; it is put straight back into the
    # textarea and is never saved as the task's execution comment here —
    # `complete_task()` remains the only writer of that field.
    execution_comment = request.POST.get('execution_comment', '')
    form = TaskAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            add_task_attachment(task, request.user, form.cleaned_data['file'])
        except TaskWorkflowError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Вложение добавлено.')
        remember_execution_draft(request, task, execution_comment)
        return redirect(f"{reverse('tasks:detail', args=[task.pk])}"
                        f"{'?' + list_query if list_query else ''}")
    if task.is_board_task:
        errors = [str(error) for error in form.errors.get('file', [])]
        return _back_to_board_card(request, task, ' '.join(['Проверьте файл вложения.', *errors]))
    messages.error(request, 'Проверьте файл вложения.')
    context = _task_detail_context(
        task, request.user, list_query, execution_comment, attachment_form=form,
    )
    return render(request, 'tasks/detail.html', context, status=400)


@login_required
def task_delete_attachment(request, pk, attachment_id):
    """Remove one wrongly uploaded file from a task.

    POST only, and scoped exactly like the download: the row is re-loaded
    through the task in the URL, so an id belonging to another task is a 404
    rather than a deletion. The permission is asked here and again in the
    service under the task's row lock — the cross in the card is a shortcut to
    a permitted action, never the thing that permits it — and a refusal is a
    404 for the same reason a refused download is one.
    """
    if request.method != 'POST':
        return redirect('tasks:detail', pk=pk)
    attachment = get_object_or_404(
        TaskAttachment.objects.select_related('task', 'task__status'),
        pk=attachment_id,
        task_id=pk,
    )
    list_query = request.POST.get('list_query', '')
    # Removing a file is the same round trip as adding one, and it used to cost
    # the user the same thing: the «Выполнение» text they had already typed.
    # The delete form carries the draft for exactly that reason.
    execution_comment = request.POST.get('execution_comment', '')
    detail_url = (f"{reverse('tasks:detail', args=[pk])}"
                  f"{'?' + list_query if list_query else ''}")
    if not can_delete_task_attachment(attachment, request.user):
        # Ids only — never the file's name, its path or its content type.
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.access_denied',
            attachment_id=attachment.pk,
            task_id=attachment.task_id,
            user_id=getattr(request.user, 'pk', None),
            operation='delete',
            outcome='denied',
        )
        raise Http404('No Task matches the given query.')
    try:
        removed = delete_task_attachment(attachment, request.user)
    except TaskWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        if removed:
            messages.success(request, 'Вложение удалено.')
    remember_execution_draft(request, attachment.task, execution_comment)
    return redirect(detail_url)


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
