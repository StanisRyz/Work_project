"""«Сообщить об ошибке»: the POST behind the topbar button, and the report page.

The button in the topbar is the shared confirmation modal
(`includes/confirm_modal.html`) with a required comment, so the submission
arrives here as an ordinary CSRF-protected POST with a `comment` field — there
is no bug-specific JavaScript and no second modal anywhere in the project.

The view parses, redirects and renders; `bugs/services.py` decides and writes.
A denial is a 404, exactly as the act, protocol and СМК views answer one.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import BugReport
from .permissions import can_report_bug, can_view_bug_report
from .services import BugWorkflowError, report_bug


def _safe_next(request):
    """Where to send the reporter back to, or the dashboard.

    The topbar renders `?next=` as the page the button was pressed on, and it
    is validated here before it is ever used: an open redirect would turn a
    button present on every page into a phishing vector. Same-host, non-scheme
    URLs only — everything else falls back to «Главная».
    """
    candidate = request.POST.get('next') or request.GET.get('next') or ''
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse('dashboard:home')


@login_required
@require_POST
def bug_report_create(request):
    """Store the report and tell the responsible accounts — POST only.

    The comment field is named `comment` because that is what the shared modal
    posts; it is the report's `message` from here on. The reporter is always
    `request.user` and the page is always the validated `next` — neither is
    taken from the submitted body, so a report cannot be filed on somebody
    else's behalf or claim to come from a page nobody visited.
    """
    if not can_report_bug(request.user):
        raise Http404('No bug report endpoint matches the given query.')
    destination = _safe_next(request)
    try:
        report = report_bug(
            reporter=request.user,
            message=request.POST.get('comment', ''),
            page_url=destination,
        )
    except BugWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'{report.label} отправлено. Спасибо — ответственные уведомлены.',
        )
    return redirect(destination)


@login_required
def bug_report_detail(request, pk):
    """One report, read-only.

    This is what the notification opens, so everybody it was sent to can read
    it; `can_view_bug_report()` is the same answer the notification recipients
    were chosen by, asked again per request.
    """
    report = get_object_or_404(
        BugReport.objects.select_related(
            'reporter__userprofile__department',
        ).prefetch_related('tasks__status'),
        pk=pk,
    )
    if not can_view_bug_report(report, request.user):
        raise Http404('No BugReport matches the given query.')
    return render(request, 'bugs/detail.html', {
        'header_title': report.label,
        'report': report,
    })
