"""The calculator page and the compact JSON interface behind it.

Ordinary Django views: `login_required` for everything, Django's own CSRF
middleware for every state-changing request, and `services.py` as the only
place a row is written. No REST framework is involved.
"""
import json
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .export import build_journal_xlsx
from .models import WindingEntry
from .services import (
    CalculatorValidationError,
    confirm_production,
    confirmed_entries,
    create_entry,
    unlock_production,
)

EXPORT_FILENAME = 'проработка.xlsx'


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
@require_POST
def entry_create(request):
    """Create the calculated case, or return the one that already exists."""
    try:
        entry, created = create_entry(request.user, _payload(request))
    except CalculatorValidationError as error:
        return _validation_response(error)
    return JsonResponse({'entry': entry.to_payload(), 'created': created}, status=201 if created else 200)


@login_required
@require_POST
def entry_confirm_production(request, pk):
    entry = get_object_or_404(WindingEntry, pk=pk)
    try:
        data = _payload(request)
        entry = confirm_production(
            request.user, entry, data.get('batchQuantity'), data.get('actualBatchTimeHours'),
        )
    except CalculatorValidationError as error:
        return _validation_response(error)
    return JsonResponse({'entry': entry.to_payload()})


@login_required
@require_POST
def entry_unlock_production(request, pk):
    entry = get_object_or_404(WindingEntry, pk=pk)
    return JsonResponse({'entry': unlock_production(request.user, entry).to_payload()})


@login_required
@require_GET
def export_journal(request):
    """The authoritative export: built from the database, not from the tab."""
    entries = list(confirmed_entries())
    if not entries:
        return JsonResponse({'detail': 'Нет подтверждённых строк для выгрузки.'}, status=400)
    response = HttpResponse(
        build_journal_xlsx(entries),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = (
        f"attachment; filename*=UTF-8''{quote(EXPORT_FILENAME)}"
    )
    return response
