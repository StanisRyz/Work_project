from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import Notification
from .services import get_notification_header_state, mark_notifications_read


@login_required
def notification_list(request):
    selected_filter = request.GET.get('filter', 'all')
    if selected_filter not in {'all', 'unread'}:
        selected_filter = 'all'
    notifications = Notification.objects.filter(recipient=request.user).select_related(
        'actor',
        'related_act',
    )
    if selected_filter == 'unread':
        notifications = notifications.filter(is_read=False)
    page_obj = Paginator(notifications, 20).get_page(request.GET.get('page'))
    return render(
        request,
        'notifications/list.html',
        {
            'active_page': 'notifications',
            'header_title': 'Уведомления',
            'page_obj': page_obj,
            'selected_filter': selected_filter,
        },
    )


@login_required
@require_GET
def notification_header_fragment(request):
    """Current bell state for the real-time client.

    The recipient is always `request.user`: the endpoint accepts no user id and
    no filter, so a client cannot ask for somebody else's notifications. It is
    a pure read — a GET never marks anything read — and the markup is rendered
    by Django from the same partial the full page uses, so the client never
    builds notification HTML from an SSE payload.
    """
    state = get_notification_header_state(request.user)
    items_html = render_to_string(
        'notifications/includes/header_items.html',
        {'recent_notifications': state['items']},
        request=request,
    )
    response = JsonResponse(
        {
            'unread_count': state['unread_count'],
            'items_html': items_html,
            'generated_at': timezone.now().isoformat(),
            'latest_notification_id': state['latest_notification_id'],
        }
    )
    # Personal, always-changing state: never let a cache or proxy keep it.
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response['Pragma'] = 'no-cache'
    response['Vary'] = 'Cookie'
    return response


@login_required
@require_POST
def mark_notification_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    mark_notifications_read(request.user, scope='single', notification_ids=[notification.pk])
    selected_filter = request.POST.get('filter')
    if selected_filter == 'unread':
        return redirect(f"{reverse('notifications:list')}?filter=unread")
    return redirect('notifications:list')


@login_required
@require_POST
def mark_all_notifications_read(request):
    mark_notifications_read(request.user, scope='all')
    return redirect('notifications:list')


@login_required
@require_POST
def mark_notifications_read_bulk(request):
    ids = []
    for raw_id in request.POST.getlist('ids'):
        try:
            ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue

    # `ids` comes from the client, so it only ever narrows what is marked: the
    # service still scopes every row to the authenticated recipient.
    _changed, unread_count = mark_notifications_read(
        request.user, scope='bell', notification_ids=ids
    )
    return JsonResponse({'unread_count': unread_count})
