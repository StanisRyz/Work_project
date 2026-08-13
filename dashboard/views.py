from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from accounts.navigation import is_admin_user


@login_required
def home(request):
    # The application root is a landing page for administrators only; everybody
    # else starts in their working area, matching the login redirect.
    if not is_admin_user(request.user):
        return redirect('acts:list')
    return render(request, 'dashboard/home.html', {'active_page': 'dashboard'})
