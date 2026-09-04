"""Who receives a bug report, and who may read one.

Both answers live here so a view, a service and a template can never disagree.
Neither keys on a role: «ответственный за ошибки» is
`accounts.UserProfile.is_bug_responsible`, an individual flag set in Django
Admin, and reading a report is allowed to the person who filed it plus the
people who have to act on it.
"""

from django.contrib.auth import get_user_model

from acts.permissions import is_manager_or_admin


def can_report_bug(user):
    """Every authenticated user may report a bug.

    Deliberately open: a bug is most often found by whoever happens to hit it,
    and a report nobody could file would leave the system silent about its own
    failures. The button is in the topbar of every page for the same reason.
    """
    return bool(getattr(user, 'is_authenticated', False))


def get_bug_responsible_users():
    """The active accounts marked «Ответственный за ошибки» in Django Admin.

    Read live on every report, so ticking or unticking the box in Admin takes
    effect immediately and nothing has to be restarted. Inactive accounts and
    inactive profiles are excluded here rather than in the caller, exactly as
    the notification service excludes them again on the way in.
    """
    return get_user_model().objects.select_related('userprofile').filter(
        is_active=True,
        userprofile__is_active=True,
        userprofile__is_bug_responsible=True,
    ).order_by('pk')


def can_view_bug_report(report, user):
    """Its author, anybody responsible for bugs, and administrators.

    The notification links here, so every recipient must be able to open what
    they were told about; the author may re-read what they sent. Nobody else
    has a reason to, and a report can quote anything the reporter saw on screen.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if report.reporter_id == user.pk:
        return True
    if is_manager_or_admin(user):
        return True
    profile = getattr(user, 'userprofile', None)
    return bool(
        profile
        and profile.is_active
        and profile.is_bug_responsible
    )
