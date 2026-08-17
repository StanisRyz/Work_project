"""The calculator page and the compact JSON interface behind it.

Ordinary Django views: `login_required` for everything, Django's own CSRF
middleware for every state-changing request, and `services.py` as the only
place a row is written. No REST framework is involved.

Reading is open to every authenticated user; every *mutation* additionally
goes through `@workup_manager_required`, which is `can_manage_workup()` and
nothing else. The template's `can_manage_workup` flag only decides which
controls are drawn — a crafted request meets the same check here.
"""
import functools
import json
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .export import build_journal_xlsx
from .models import WindingEntry
from .permissions import can_manage_workup
from .services import (
    CalculatorValidationError,
    confirm_production,
    create_entry,
    create_manual_entry,
    delete_entry,
    journal_entries,
    unlock_production,
)

EXPORT_FILENAME = 'проработка.xlsx'

FORBIDDEN_MESSAGE = 'Изменять «Проработку» может только сотрудник с ролью ПДО.'


def workup_manager_required(view):
    """Refuse a journal mutation with JSON 403 unless the user is ПДО.

    A JSON answer, not the HTML login redirect: every caller here is a
    `fetch()` from the calculator page, and the check runs before the view
    touches the request body, so a rejected request changes nothing.
    """
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_manage_workup(request.user):
            return JsonResponse({'detail': FORBIDDEN_MESSAGE}, status=403)
        return view(request, *args, **kwargs)

    return wrapper


def _payload(request):
    """The decoded JSON body, or a validation error the view can return."""
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CalculatorValidationError({'__all__': 'Некорректный запрос.'})
    if not isinstance(data, dict):
        raise CalculatorValidationError({'__all__': 'Некорректный запрос.'})
    return data


def _validation_response(error):
    return JsonResponse({'detail': str(error), 'errors': error.errors}, status=400)


@login_required
# The page's own fetch calls read the token from the cookie, so the cookie
# must exist even though the template posts no form of its own.
@ensure_csrf_cookie
def calculator_page(request):
    return render(request, 'calculator/page.html', {
        'active_page': 'calculator', 'header_title': 'Калькулятор',
        # Presentation metadata only: it decides which controls are rendered,
        # never whether a request is allowed.
        'can_manage_workup': can_manage_workup(request.user),
    })


@login_required
@require_GET
def entry_list(request):
    """The shared journal: every authenticated user sees the same rows."""
    entries = [entry.to_payload() for entry in WindingEntry.objects.all()]
    response = JsonResponse({'entries': entries})
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    return response


@login_required
@workup_manager_required
@require_POST
def entry_create(request):
    """Create the calculated case, or return the one that already exists.

    Only ПДО reaches it: a non-ПДО user still calculates and still sees the
    result, the calculation simply is not persisted.
    """
    try:
        entry, created = create_entry(request.user, _payload(request))
    except CalculatorValidationError as error:
        return _validation_response(error)
    return JsonResponse({'entry': entry.to_payload(), 'created': created}, status=201 if created else 200)


@login_required
@workup_manager_required
@require_POST
def entry_manual_create(request):
    """Add a row by hand from «Проработка»; a duplicate is a valid request."""
    try:
        entry = create_manual_entry(request.user, _payload(request))
    except CalculatorValidationError as error:
        return _validation_response(error)
    return JsonResponse({'entry': entry.to_payload(), 'created': True}, status=201)


@login_required
@workup_manager_required
@require_POST
def entry_confirm_production(request, pk):
    entry = get_object_or_404(WindingEntry, pk=pk)
    try:
        entry = confirm_production(request.user, entry, _payload(request))
    except CalculatorValidationError as error:
        return _validation_response(error)
    return JsonResponse({'entry': entry.to_payload()})


@login_required
@workup_manager_required
@require_POST
def entry_unlock_production(request, pk):
    entry = get_object_or_404(WindingEntry, pk=pk)
    return JsonResponse({'entry': unlock_production(request.user, entry).to_payload()})


@login_required
@workup_manager_required
@require_POST
def entry_delete(request, pk):
    """Delete exactly the row the primary key names.

    The permission is checked before the lookup, so an unauthorised request
    cannot even probe which primary keys exist; a missing row is the ordinary
    404. The response is deliberately tiny — the client removes the one row it
    asked about and nothing else.
    """
    entry = get_object_or_404(WindingEntry, pk=pk)
    delete_entry(entry)
    return JsonResponse({'deleted': True, 'id': pk})


@login_required
@require_GET
def export_journal(request):
    """The authoritative export: built from the database, not from the tab.

    Always available — every row goes in, confirmed or not, and an empty
    journal still yields a workbook with the header row.
    """
    entries = list(journal_entries())
    response = HttpResponse(
        build_journal_xlsx(entries),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = (
        f"attachment; filename*=UTF-8''{quote(EXPORT_FILENAME)}"
    )
    return response
