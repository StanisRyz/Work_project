from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Notification
from .services import mark_notifications_read


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
