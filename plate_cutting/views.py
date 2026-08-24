"""The page of the plate-cutting calculator and the saved-set endpoints.

The calculation itself is still entirely the browser's: the page view only
hands over the agreed coefficients. What the three JSON views add is the saved
package *sets* — ordinary `login_required` Django views, Django's own CSRF
middleware on the one that writes, and `services.py` as the only place a
preset row is created. No REST framework is involved.

The payloads carry inputs only. Neither seconds, hours nor an expanded formula
crosses this boundary in either direction — the calculator recomputes them.
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .constants import HOLE_SECONDS, PLATE_LENGTH_RANGES
from .models import PlateCuttingPreset
from .services import (
    PlateCuttingValidationError,
    create_preset,
    preset_detail,
    preset_summary,
    search_presets,
)


@login_required
# The page's own fetch calls read the token from the cookie, so the cookie must
# exist even though the template posts no form of its own.
@ensure_csrf_cookie
def plate_cutting_page(request):
    return render(request, 'plate_cutting/page.html', {
        'active_page': 'plate_cutting',
        'header_title': 'Калькулятор рубки пластин',
        'length_ranges': PLATE_LENGTH_RANGES,
        'hole_seconds': HOLE_SECONDS,
    })


@login_required
@require_POST
def preset_create(request):
    """Save the packages currently on screen under the entered name."""
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'detail': 'Некорректный запрос.'}, status=400)
    try:
        preset = create_preset(request.user, data)
    except PlateCuttingValidationError as error:
        return JsonResponse({'detail': str(error)}, status=400)
    return JsonResponse({'preset': preset_summary(preset)}, status=201)


@login_required
@require_GET
def preset_list(request):
    """The saved sets, filtered by the modal's search field. Shared by all."""
    presets = search_presets(request.GET.get('q', ''))
    response = JsonResponse({'presets': [preset_summary(preset) for preset in presets]})
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    return response


@login_required
@require_GET
def preset_load(request, pk):
    """The one set the user chose, with its packages in the saved order."""
    preset = get_object_or_404(
        PlateCuttingPreset.objects.select_related('author').prefetch_related('packages'),
        pk=pk,
    )
    response = JsonResponse({'preset': preset_detail(preset)})
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    return response
