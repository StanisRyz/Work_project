"""Presentation helpers shared by every registry: sortable headers and deadlines.

Nothing here decides who sees what or which rows exist — the owning module's
state builder does that. These tags only rewrite the page's own query string
(so scope, filters and search survive a click) and phrase a date for people.
"""

from django import template
from django.utils import timezone
from django.utils.html import format_html

register = template.Library()


def plural_ru(number, one, few, many):
    """«1 день», «2 дня», «5 дней» — Russian agreement for a count."""
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def _days(number):
    return f'{number} {plural_ru(number, "день", "дня", "дней")}'


@register.filter
def due_hint(value, today=None):
    """«сегодня», «завтра», «через 3 дня», «просрочен на 2 дня» — or ''.

    Calendar days against the local date: the deadline itself is a calendar
    date, and «через 3 рабочих дня» would be a second deadline rule.
    """
    if not value:
        return ''
    today = today or timezone.localdate()
    delta = (value - today).days
    if delta == 0:
        return 'сегодня'
    if delta == 1:
        return 'завтра'
    if delta > 1:
        return f'через {_days(delta)}'
    return f'просрочен на {_days(-delta)}'


@register.filter
def due_tone(value, today=None):
    """'overdue', 'soon' (today or tomorrow) or '' — the pill's colour."""
    if not value:
        return ''
    delta = (value - (today or timezone.localdate())).days
    if delta < 0:
        return 'overdue'
    if delta <= 1:
        return 'soon'
    return ''


@register.simple_tag(takes_context=True)
def query_with(context, **changes):
    """The current query string with `changes` applied, starting with «?».

    A value of `None` removes the key. `page` never survives a change of
    filter or order: the rows it pointed at are different rows now.
    """
    request = context.get('request')
    params = request.GET.copy() if request is not None else None
    if params is None:
        from django.http import QueryDict

        params = QueryDict(mutable=True)
    params.pop('page', None)
    for key, value in changes.items():
        if value is None or value == '':
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f'?{encoded}' if encoded else '?'


@register.simple_tag(takes_context=True)
def sortable_th(context, label, field, current_sort='', css_class=''):
    """A `<th>` whose label orders the list by `field`, toggling the direction.

    `current_sort` is what the state builder accepted (`field` or `-field`),
    never the raw parameter, so an unknown value draws no arrow.
    """
    if current_sort == field:
        target, arrow, aria = f'-{field}', ' ↑', 'ascending'
    elif current_sort == f'-{field}':
        target, arrow, aria = field, ' ↓', 'descending'
    else:
        target, arrow, aria = field, '', 'none'
    href = query_with(context, sort=target)
    return format_html(
        '<th class="{}" aria-sort="{}"><a class="sort-link{}" href="{}">{}<span aria-hidden="true">{}</span></a></th>',
        css_class,
        aria,
        ' sort-link--active' if arrow else '',
        href,
        label,
        arrow,
    )
