from django.contrib.auth import views as auth_views

from .navigation import get_default_landing_url


class AppLoginView(auth_views.LoginView):
    """Login that lands every user on `/quality/acts/`.

    `?next=` still wins, so a deep link kept across the login screen — including
    the Django admin — is honoured.
    """

    template_name = 'accounts/login.html'

    def get_success_url(self):
        return self.get_redirect_url() or get_default_landing_url(self.request.user)
