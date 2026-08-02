from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Notification


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
    notification.mark_read()
    selected_filter = request.POST.get('filter')
    if selected_filter == 'unread':
        return redirect(f"{reverse('notifications:list')}?filter=unread")
    return redirect('notifications:list')


@login_required
@require_POST
def mark_all_notifications_read(request):
    Notification.objects.filter(recipient=request.user, is_read=False).update(
        is_read=True,
        read_at=timezone.now(),
    )
    return redirect('notifications:list')
