"""Protocols pages: the registry, type selection and the single-page editor.

Thin by design. A view parses the request, asks `protocols/permissions.py` who
is allowed to do what, hands the parsed structure to `protocols/services.py`
and renders — no protocol content is written here.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from ecosystem.logging_utils import log_event
from realtime.auth import realtime_login_required
from realtime.fragments import LIVE_REVISION_PLACEHOLDER, content_revision

from ecosystem.xlsx import xlsx_response

from .forms import ProtocolAttachmentForm, ProtocolCommentForm, ProtocolDraftForm
from .models import Protocol, ProtocolAttachment, ProtocolType
from .permissions import (
    can_add_protocol_attachment,
    can_contribute_to_protocol,
    can_decide_protocol_approval,
    can_delete_draft_protocol,
    can_delete_protocol_attachment,
    can_download_protocol_attachment,
    can_edit_protocol,
    can_send_protocol_for_approval,
    can_view_protocol,
)
from .pdf import ProtocolPdfUnavailable, protocol_pdf_filename, render_protocol_pdf
from .selectors import (
    build_protocol_document,
    build_protocol_list_state,
    describe_protocol_workflow,
    get_active_protocol_types,
    get_approval_progress,
    get_approval_revision_groups,
    get_current_approval_rows,
    get_editor_directory,
    get_protocol_attachments,
    get_protocol_comments,
    get_protocol_history_groups,
    get_readable_protocols_queryset,
    get_related_protocol_tasks,
    get_user_approval,
)
from .services import (
    create_protocol_based_on,
    ProtocolWorkflowError,
    add_protocol_attachment,
    add_protocol_comment,
    approve_protocol,
    attachment_logger,
    create_protocol,
    delete_draft_protocol,
    delete_protocol_attachment,
    return_protocol_for_revision,
    save_protocol_draft,
    send_protocol_for_approval,
)


@login_required
def protocol_list(request):
    state = build_protocol_list_state(request.GET)
    if request.GET.get('export') == 'xlsx':
        return _export_protocol_registry(state)
    return render(request, 'protocols/list.html', {
        'active_page': 'protocols', 'header_title': 'Протоколы', **state,
    })


def _export_protocol_registry(state):
    """The registry exactly as the page shows it, from the same rows."""
    rows = []
    for row in state['rows']:
        protocol = row['protocol']
        approval = row['approval']
        rows.append([
            protocol.number,
            protocol.protocol_type.name,
            row['subject'],
            protocol.author.get_full_name() or protocol.author.get_username(),
            timezone.localtime(protocol.created_at).date(),
            protocol.get_status_display(),
            f"{approval['approved']} из {approval['total']}" if approval else '',
            ', '.join(approval['pending_names']) if approval else '',
        ])
    return xlsx_response(
        'protocols', 'Протоколы',
        ['№', 'Тип протокола', 'Тема', 'Автор', 'Дата создания', 'Статус', 'Согласовано', 'Ожидаем'],
        rows,
    )


# --------------------------------------------------------------------------
# Live fragments
#
# One rule: a fragment renders through the very same state builder and the very
# same partial as the full page, so a refreshed block can never disagree with a
# reload. Each one re-loads the protocol, re-checks `request.user`, accepts no
# user parameter, changes nothing on GET and is never cached.
# --------------------------------------------------------------------------


# The detail page's tabs. An unknown `?tab=` falls back to «Протокол» rather
# than rendering nothing, and the whitelist is the only place they are named.
DETAIL_TABS = ('protocol', 'history', 'collaboration', 'activities')


def _fragment_response(payload):
    """JSON fragment response: never cached, always scoped to the session."""
    response = JsonResponse({**payload, 'generated_at': timezone.now().isoformat()})
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response['Vary'] = 'Cookie'
    return response


def _get_live_protocol(request, pk):
    """Re-load the protocol and re-check visibility for every live fragment.

    A user who may not read it — or a draft that has since been deleted — gets
    a plain 404 with no object data at all, not a hint that it ever existed.
    """
    protocol = get_readable_protocols_queryset().filter(pk=pk).first()
    if protocol is None or not can_view_protocol(protocol, request.user):
        raise Http404('No Protocol matches the given query.')
    return protocol


@realtime_login_required
@require_GET
def protocol_list_fragment(request):
    """The registry rows for the current tab, for the live client.

    Same builder, same partial and the same query parameters as the full page.
    """
    state = build_protocol_list_state(request.GET)
    return _fragment_response(
        {
            'results_html': render_to_string(
                'protocols/includes/registry_results.html', state, request=request
            ),
            'tab': state['tab'],
        }
    )


@realtime_login_required
@require_GET
def protocol_heading_fragment(request, pk):
    protocol = _get_live_protocol(request, pk)
    context = _detail_context(request, protocol, include_document=False)
    return _fragment_response(
        {
            'html': render_to_string(
                'protocols/includes/detail_heading.html', context, request=request
            ),
            'status': protocol.status,
            'revision': protocol.revision,
        }
    )


@realtime_login_required
@require_GET
def protocol_approval_fragment(request, pk):
    protocol = _get_live_protocol(request, pk)
    context = _detail_context(request, protocol, include_document=False)
    return _fragment_response(
        {
            'html': render_to_string(
                'protocols/includes/detail_approval.html', context, request=request
            ),
            'status': protocol.status,
        }
    )


@realtime_login_required
@require_GET
def protocol_content_fragment(request, pk):
    """The document block — the editor or the read-only rendering.

    Which of the two comes out is decided by `_detail_context`, exactly as on
    the full page, so the fragment can never hand someone an editor the page
    would not have given them.
    """
    protocol = _get_live_protocol(request, pk)
    context = _detail_context(request, protocol)
    html = render_to_string(CONTENT_TEMPLATE, context, request=request)
    return _fragment_response(
        {
            'html': html,
            # Compared by the client with the fingerprint the page was
            # rendered with: an unchanged document is neither replaced nor
            # reported as a conflict to an author with unsaved input.
            'revision': content_revision(html),
            'status': protocol.status,
            'can_edit': context['can_edit'],
        }
    )


@realtime_login_required
@require_GET
def protocol_history_fragment(request, pk):
    protocol = _get_live_protocol(request, pk)
    context = _detail_context(request, protocol, include_document=False)
    return _fragment_response(
        {
            'html': render_to_string(
                'protocols/includes/detail_history.html', context, request=request
            )
        }
    )


@realtime_login_required
@require_GET
def protocol_comments_fragment(request, pk):
    """The comment list only — never the textarea.

    A live refresh must not be able to discard what somebody is typing, so the
    new-comment form is deliberately outside this partial, exactly as it is on
    the act page.
    """
    protocol = _get_live_protocol(request, pk)
    return _fragment_response(
        {
            'html': render_to_string(
                'protocols/includes/comments_list.html',
                {'comments': get_protocol_comments(protocol)},
                request=request,
            )
        }
    )


@realtime_login_required
@require_GET
def protocol_attachments_fragment(request, pk):
    """The attachment cards only, each with this reader's own delete right."""
    protocol = _get_live_protocol(request, pk)
    return _fragment_response(
        {
            'html': render_to_string(
                'protocols/includes/attachments_list.html',
                {
                    'protocol': protocol,
                    'attachments': get_protocol_attachments(protocol, request.user),
                },
                request=request,
            )
        }
    )


@realtime_login_required
@require_GET
def protocol_activities_fragment(request, pk):
    protocol = _get_live_protocol(request, pk)
    return _fragment_response(
        {
            'html': render_to_string(
                'protocols/includes/activities_content.html',
                {
                    'protocol': protocol,
                    'related_tasks': get_related_protocol_tasks(protocol, request.user),
                },
                request=request,
            )
        }
    )


@login_required
def protocol_create(request):
    """Choose a type; the service does the numbering, the author row and history."""
    protocol_types = get_active_protocol_types()
    if request.method == 'POST':
        protocol_type = protocol_types.filter(pk=_as_int(request.POST.get('protocol_type'))).first()
        if protocol_type is None:
            return render(request, 'protocols/create.html', {
                'active_page': 'protocols', 'header_title': 'Создание протокола',
                'protocol_types': protocol_types, 'error': 'Выберите тип протокола.',
            }, status=400)
        protocol = create_protocol(protocol_type, request.user)
        return redirect('protocols:detail', pk=protocol.pk)
    return render(request, 'protocols/create.html', {
        'active_page': 'protocols', 'header_title': 'Создание протокола',
        'protocol_types': protocol_types, 'error': '',
    })


@login_required
def protocol_detail(request, pk):
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    return _render_detail(request, protocol, _detail_context(request, protocol))


# --------------------------------------------------------------------------
# The official document
#
# Both endpoints render `build_protocol_document()`: the page prints it and the
# PDF downloads it, so the two cannot say different things. Reading is open to
# every authenticated user, exactly as `can_view_protocol` already allows —
# neither endpoint changes anything.
# --------------------------------------------------------------------------


@login_required
def protocol_print(request, pk):
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    if not can_view_protocol(protocol, request.user):
        raise Http404('No Protocol matches the given query.')
    return render(request, 'protocols/print.html', {
        'document': build_protocol_document(protocol),
    })


@login_required
def protocol_pdf(request, pk):
    """The same document as a downloadable PDF.

    An installation without the renderer or without a Cyrillic font answers
    503 with a readable message instead of streaming a broken file; the same
    condition is reported by `manage.py check` in production.
    """
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    if not can_view_protocol(protocol, request.user):
        raise Http404('No Protocol matches the given query.')
    document = build_protocol_document(protocol)
    try:
        content = render_protocol_pdf(document)
    except ProtocolPdfUnavailable as exc:
        return HttpResponse(f'PDF недоступен: {exc}', status=503, content_type='text/plain; charset=utf-8')
    response = HttpResponse(content, content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="{protocol_pdf_filename(document)}"'
    )
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    return response


@login_required
def protocol_save_draft(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    # The permission is enforced here and again inside the service under the
    # row lock; hiding the button is presentation, never the check.
    if not can_edit_protocol(protocol, request.user):
        return _render_detail(
            request, protocol,
            _detail_context(request, protocol, save_error='Изменить этот протокол нельзя.'),
            status=403,
        )
    form = ProtocolDraftForm(protocol, request.POST)
    if not form.is_valid():
        return _render_detail(
            request, protocol, _detail_context(request, protocol, form=form),
            status=400,
        )
    try:
        save_protocol_draft(protocol, request.user, form.cleaned)
    except ProtocolWorkflowError as exc:
        # Nothing was stored, so the editor is rendered from what was posted:
        # the author must not lose the text the service refused.
        protocol.refresh_from_db()
        return _render_detail(
            request, protocol,
            _detail_context(request, protocol, form=form, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Черновик протокола сохранён.')
    return redirect('protocols:detail', pk=protocol.pk)


@login_required
def protocol_create_based_on(request, pk):
    """«Создать на основе»: POST only, a new draft of the same type.

    Anybody who may create a protocol may do it — reading the source is open
    to every authenticated user, and the new draft is theirs. What is copied
    is `create_protocol_based_on()`'s decision, not this view's.
    """
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    source = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    try:
        protocol = create_protocol_based_on(source, request.user)
    except ProtocolWorkflowError as exc:
        return _render_detail(
            request, source,
            _detail_context(request, source, save_error=str(exc)), status=400,
        )
    messages.success(
        request,
        f'Создан черновик на основе «{source.protocol_type.name} №{source.number}»: '
        'участники и повестка перенесены, проверьте их перед сохранением.',
    )
    return redirect('protocols:detail', pk=protocol.pk)


@login_required
def protocol_delete(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    try:
        delete_draft_protocol(protocol, request.user)
    except ProtocolWorkflowError as exc:
        return _render_detail(
            request, protocol,
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Черновик протокола удалён.')
    return redirect('protocols:list')


# --------------------------------------------------------------------------
# Workflow endpoints
#
# POST only, one per transition, and each one a thin wrapper: the view parses
# the request, asks `protocols/permissions.py` the presentation question and
# hands the decision to `protocols/services.py`, which re-checks status, actor
# and content under the row lock. No state machine is restated here, and a GET
# never reaches a service — it redirects to the protocol page.
# --------------------------------------------------------------------------


@login_required
def protocol_send_for_approval(request, pk):
    """Save what the editor currently shows, then submit that content.

    The two steps are deliberately separate calls in that order: the author
    presses one button, but what goes for approval must be exactly what they
    are looking at, so the visible form is validated and stored first and the
    workflow service then re-reads the persisted protocol. An invalid form
    submits nothing; a draft that saves but is refused by
    `validate_protocol_for_approval()` leaves the protocol editable with the
    workflow error shown, which is the whole point of not merging the two.
    """
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    if not can_send_protocol_for_approval(protocol, request.user):
        return _render_detail(
            request, protocol,
            _detail_context(
                request, protocol,
                save_error='Отправить этот протокол на согласование нельзя.',
            ),
            status=403,
        )
    form = ProtocolDraftForm(protocol, request.POST)
    if not form.is_valid():
        return _render_detail(
            request, protocol, _detail_context(request, protocol, form=form),
            status=400,
        )
    try:
        save_protocol_draft(protocol, request.user, form.cleaned)
        send_protocol_for_approval(protocol, request.user)
    except ProtocolWorkflowError as exc:
        # The draft may already be stored — that is intended. The protocol
        # stays in its editable status, so the page re-renders as the editor
        # with the refusal above it — from what was posted, so a refusal of
        # the save itself does not throw the typed text away either.
        protocol.refresh_from_db()
        return _render_detail(
            request, protocol,
            _detail_context(request, protocol, form=form, save_error=str(exc)), status=400,
        )
    protocol.refresh_from_db()
    if protocol.status == Protocol.Status.ARCHIVED:
        # Nobody had to sign, so the same transaction archived it: say so
        # instead of sending the author to an empty «0 из 0» panel.
        messages.success(request, 'Протокол не требовал согласования и помещён в архив.')
    else:
        messages.success(
            request,
            f'Протокол отправлен на согласование (редакция {protocol.revision}).',
        )
    return redirect('protocols:detail', pk=protocol.pk)


@login_required
def protocol_approve(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    try:
        approve_protocol(protocol, request.user)
    except ProtocolWorkflowError as exc:
        protocol.refresh_from_db()
        return _render_detail(
            request, protocol,
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Протокол согласован.')
    return redirect('protocols:detail', pk=protocol.pk)


@login_required
def protocol_return_for_revision(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    try:
        return_protocol_for_revision(protocol, request.user, request.POST.get('comment', ''))
    except ProtocolWorkflowError as exc:
        protocol.refresh_from_db()
        return _render_detail(
            request, protocol,
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Протокол возвращён на доработку.')
    return redirect('protocols:detail', pk=protocol.pk)


# --------------------------------------------------------------------------
# Collaboration endpoints
#
# POST for the two mutations, GET for the download. Each one re-loads the
# protocol, asks `protocols/permissions.py`, and hands the work to
# `protocols/services.py`, which checks the same rule again under the row lock.
# A denied or missing file is an ordinary 404 that says nothing about what
# exists — never a filesystem path and never a stored file name.
# --------------------------------------------------------------------------


@login_required
def protocol_add_comment(request, pk):
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    if request.method != 'POST':
        return _redirect_to_tab(protocol, 'collaboration')
    if not can_contribute_to_protocol(protocol, request.user):
        messages.error(request, 'Комментировать этот протокол нельзя.')
        return _redirect_to_tab(protocol, 'collaboration')

    form = ProtocolCommentForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Проверьте текст комментария.')
        return render(
            request,
            'protocols/detail.html',
            _detail_context(request, protocol, comment_form=form, tab='collaboration'),
            status=400,
        )
    try:
        add_protocol_comment(protocol, request.user, form.cleaned_data['text'])
    except ProtocolWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Комментарий добавлен.')
    return _redirect_to_tab(protocol, 'collaboration')


@login_required
def protocol_add_attachment(request, pk):
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    if request.method != 'POST':
        return _redirect_to_tab(protocol, 'collaboration')
    if not can_add_protocol_attachment(protocol, request.user):
        messages.error(request, 'Добавить вложение к этому протоколу нельзя.')
        return _redirect_to_tab(protocol, 'collaboration')

    form = ProtocolAttachmentForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, 'Проверьте файл вложения.')
        return render(
            request,
            'protocols/detail.html',
            _detail_context(request, protocol, attachment_form=form, tab='collaboration'),
            status=400,
        )
    try:
        add_protocol_attachment(
            protocol,
            request.user,
            form.cleaned_data['file'],
            form.cleaned_data.get('description', ''),
        )
    except ProtocolWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Вложение добавлено.')
    return _redirect_to_tab(protocol, 'collaboration')


@login_required
def protocol_download_attachment(request, pk, attachment_id):
    """Stream one attachment after re-checking who may read its protocol.

    The file is never reachable by URL: the row is looked up scoped to the
    protocol in the path, the read rule is asked again, and only then is
    storage opened. A refusal and a missing file are the same 404 to the
    client; the difference goes to the log, as identifiers only.
    """
    attachment = get_object_or_404(
        ProtocolAttachment.objects.select_related('protocol', 'uploaded_by'),
        pk=attachment_id,
        protocol_id=pk,
    )
    if not can_download_protocol_attachment(attachment, request.user):
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.access_denied',
            attachment_id=attachment.pk,
            protocol_id=attachment.protocol_id,
            user_id=request.user.pk,
            operation='download',
            outcome='denied',
        )
        raise Http404('No Protocol matches the given query.')
    if not attachment.file:
        log_event(
            attachment_logger,
            'WARNING',
            'attachment.storage_failed',
            attachment_id=attachment.pk,
            protocol_id=attachment.protocol_id,
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
            protocol_id=attachment.protocol_id,
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
        protocol_id=attachment.protocol_id,
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
def protocol_delete_attachment(request, pk, attachment_id):
    attachment = get_object_or_404(
        ProtocolAttachment.objects.select_related('protocol', 'uploaded_by'),
        pk=attachment_id,
        protocol_id=pk,
    )
    if request.method != 'POST':
        return _redirect_to_tab(attachment.protocol, 'collaboration')
    if not can_view_protocol(attachment.protocol, request.user):
        raise Http404('No Protocol matches the given query.')
    if not can_delete_protocol_attachment(attachment, request.user):
        messages.error(request, 'Недостаточно прав для удаления вложения.')
        return _redirect_to_tab(attachment.protocol, 'collaboration')

    try:
        deleted = delete_protocol_attachment(attachment, request.user)
    except ProtocolWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        if deleted:
            messages.success(request, 'Вложение удалено.')
    return _redirect_to_tab(attachment.protocol, 'collaboration')


CONTENT_TEMPLATE = 'protocols/includes/detail_content.html'


def _render_detail(request, protocol, context, status=200):
    """The protocol page, with the fingerprint of its document block.

    The live client compares this fingerprint with the one the content
    fragment returns, so a reconnect or a recovery sync that finds the document
    unchanged neither replaces the editor nor tells the author it was changed.

    An ordinary page renders the block once and reuses that markup. A page
    re-rendered from a posted editor form shows what the author typed, so its
    fingerprint comes from a clean render — what a fragment would return — and
    the page is flagged as holding unsaved input from the start.
    """
    if context.get('detail_tab') != 'protocol':
        return render(request, 'protocols/detail.html', context, status=status)
    if not context.get('form_is_bound'):
        content_html = render_to_string(CONTENT_TEMPLATE, context, request=request)
        context['content_html'] = content_html
        context['content_revision'] = content_revision(content_html)
        return render(request, 'protocols/detail.html', context, status=status)
    # The page first, with a placeholder for the fingerprint, and the clean
    # render second — so the posted form is the one the response is about.
    context['content_revision'] = LIVE_REVISION_PLACEHOLDER
    response = render(request, 'protocols/detail.html', context, status=status)
    clean = _detail_context(request, protocol)
    revision = content_revision(render_to_string(CONTENT_TEMPLATE, clean, request=request))
    response.content = response.content.replace(
        LIVE_REVISION_PLACEHOLDER.encode(), revision.encode()
    )
    return response


def _redirect_to_tab(protocol, tab):
    """Back to the protocol page with the tab that issued the action open."""
    return redirect(f"{reverse('protocols:detail', args=[protocol.pk])}?tab={tab}")


def _detail_context(request, protocol, form=None, save_error='', include_document=True,
                    comment_form=None, attachment_form=None, tab=None):
    """Everything the protocol page and its live fragments render.

    `include_document=False` is what the heading, approval and history
    fragments pass: they need the status, the permissions and the approval read
    side, but not the editor directory or the read-only document blocks, and
    building those would be work no rendered partial would use. It changes
    nothing about *what* the shared blocks show.
    """
    can_edit = can_edit_protocol(protocol, request.user)
    requested = tab or request.GET.get('tab') or request.POST.get('tab') or 'protocol'
    detail_tab = requested if requested in DETAIL_TABS else 'protocol'
    can_contribute = can_contribute_to_protocol(protocol, request.user)
    context = {
        'active_page': 'protocols',
        'header_title': f'{protocol.protocol_type.name} №{protocol.number}',
        'protocol': protocol,
        'detail_tab': detail_tab,
        'can_edit': can_edit,
        'can_contribute': can_contribute,
        'can_delete': can_delete_draft_protocol(protocol, request.user),
        'save_error': save_error,
        'history_groups': get_protocol_history_groups(protocol),
        'workflow_steps': describe_protocol_workflow(protocol),
        'author_participant': protocol.participants.filter(user_id=protocol.author_id).first(),
        # Approval read side. `can_send`/`can_decide` only decide what is
        # rendered; every one of the three endpoints re-checks its own rule.
        'can_send_for_approval': can_send_protocol_for_approval(protocol, request.user),
        'can_decide_approval': can_decide_protocol_approval(protocol, request.user),
        'approval_progress': get_approval_progress(protocol),
        'current_approvals': get_current_approval_rows(protocol),
        'approval_revisions': get_approval_revision_groups(protocol),
        'user_approval': get_user_approval(protocol, request.user),
        'is_under_approval': protocol.status == Protocol.Status.APPROVAL,
    }
    if not include_document:
        return context
    # Collaboration, on the full page only. The heading, approval and history
    # fragments pass `include_document=False` and render none of it, so reading
    # the feed, the files and the generated tasks for them would be three
    # queries no partial would use. The three collaboration fragments build
    # their own context and do not come through here at all.
    if detail_tab == 'collaboration':
        context['comment_form'] = comment_form or ProtocolCommentForm()
        context['attachment_form'] = attachment_form or ProtocolAttachmentForm()
        context['comments'] = get_protocol_comments(protocol)
        context['attachments'] = get_protocol_attachments(protocol, request.user)
        # «Нормативные документы»: read from Documentation, which owns the links.
        from documents.permissions import can_link_document_to_protocol
        from documents.selectors import build_document_links_for, build_linkable_documents

        can_link = can_link_document_to_protocol(protocol, request.user)
        context['document_links'] = build_document_links_for(request.user, protocol=protocol)
        context['can_link_documents'] = can_link
        context['linkable_documents'] = build_linkable_documents(request.user) if can_link else []
    elif detail_tab == 'activities':
        context['related_tasks'] = get_related_protocol_tasks(protocol, request.user)
    if can_edit:
        directory = get_editor_directory()
        context['form'] = form or ProtocolDraftForm(protocol)
        # A posted form holds the author's own input; the live client must
        # never replace it with a clean render of the stored draft.
        context['form_is_bound'] = form is not None
        context.update(directory)
        # The speaker selector offers exactly the current participants; the
        # browser keeps it in step as rows are added or removed.
        context['speaker_options'] = _speaker_options(
            protocol, context['form'], directory['employees']
        )
        # The prototypes the `<template>` elements render; the browser clones
        # them and renumbers, so no markup is assembled in JavaScript.
        context['empty_row'] = {
            'index': 0, 'text': '', 'user': '', 'department': '',
            'speaker': '', 'requires_approval': False, 'errors': {},
        }
        context['empty_action_row'] = {
            'index': 0, 'text': '', 'due_date': '', 'assignees': [],
            'split_for_assignees': False, 'requires_attachment': False, 'errors': {},
        }
        context['empty_assignee'] = {'user': '', 'department': ''}
    else:
        context['participants'] = protocol.participants.all()
        context['agenda_items'] = protocol.agenda_items.all()
        context['speeches'] = protocol.speeches.select_related('speaker')
        # `task` is the reverse one-to-one to the real `tasks.Task` an archived
        # protocol produced; joined here so the read-only cards can link to it
        # without a query per decision.
        # `tasks` is a list now, not a single task: a decision marked
        # `split_for_assignees` archived into one task per assignee.
        context['actions'] = protocol.actions.select_related('department').prefetch_related(
            'assignees__user', 'tasks__status', 'tasks__individual_assignee'
        )
    return context


def _speaker_options(protocol, form, employees):
    """Author first, then the participant rows as they are currently rendered."""
    names = {
        employee.pk: employee.get_full_name() or employee.get_username()
        for employee in employees
    }
    # A stored participant keeps its snapshot name even if the profile moved on.
    names.update(
        {
            participant.user_id: participant.display_name
            for participant in protocol.participants.all()
        }
    )
    options = [{'value': str(protocol.author_id), 'label': names.get(protocol.author_id, '')}]
    for row in form.participant_rows:
        value = row.get('user') or ''
        user_id = _as_int(value)
        if user_id is None or user_id not in names:
            continue
        options.append({'value': value, 'label': names[user_id]})
    return options


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
