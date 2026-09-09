from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.utils.http import url_has_allowed_host_and_scheme

from .navigation import get_default_landing_url


class AppLoginView(auth_views.LoginView):
    """Login that lands every user on the dashboard at `/`.

    `?next=` still wins, so a deep link kept across the login screen — including
    the Django admin — is honoured.
    """

    template_name = 'accounts/login.html'

    def get_success_url(self):
        return self.get_redirect_url() or get_default_landing_url(self.request.user)


class AppPasswordChangeView(auth_views.PasswordChangeView):
    """«Сменить пароль» from the profile menu.

    Django's own `PasswordChangeView` and `PasswordChangeForm`, unchanged in
    every part that matters: the current password is verified against the
    stored hash, the new one goes through `validate_password()` with whatever
    `AUTH_PASSWORD_VALIDATORS` the project configures (deliberately empty here —
    see `ecosystem/settings.py`), `set_password()` does the hashing, and the
    view's own `form_valid()` calls `update_session_auth_hash()` so the user
    stays logged in on this device instead of being bounced to the login page.
    Nothing about hashing, storage or session handling is reimplemented.

    What is ours is only where it lands. The dialog in the topbar posts here
    from whatever page the user was on, so success returns them to that page
    with a message rather than to a «done» stub; the target is validated
    against the host, exactly as the login view validates `?next=`. A rejected
    form falls back to this view's own page, which shows the same three fields
    with Django's field errors beside them — that page is also the
    no-JavaScript path of the menu item.
    """

    template_name = 'accounts/password_change.html'

    def _redirect_target(self):
        """The page to return to, taken from the request and never trusted raw."""
        target = self.request.POST.get('next') or self.request.GET.get('next') or ''
        if target and url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return target
        return ''

    def get_success_url(self):
        return self._redirect_target() or get_default_landing_url(self.request.user)

    def form_valid(self, form):
        # `super()` saves the new password and re-signs the session; the
        # message is the only thing added, and it is shown by `base.html` on
        # the page the user came from.
        response = super().form_valid(form)
        messages.success(self.request, 'Пароль изменён.')
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['header_title'] = 'Смена пароля'
        context['next'] = self._redirect_target()
        return context
