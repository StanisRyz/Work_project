"""The single page of the plate-cutting calculator.

One `login_required` GET and nothing else: there is no model, no migration,
no JSON endpoint and no real-time channel. The view only hands the agreed
coefficients to the template, which renders them into the length selector;
every calculation then happens in the browser.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .constants import HOLE_SECONDS, PLATE_LENGTH_RANGES


@login_required
def plate_cutting_page(request):
    return render(request, 'plate_cutting/page.html', {
        'active_page': 'plate_cutting',
        'header_title': 'Калькулятор рубки пластин',
        'length_ranges': PLATE_LENGTH_RANGES,
        'hole_seconds': HOLE_SECONDS,
    })
