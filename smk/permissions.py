"""Who may create and read an СМК record.

One module, one answer: the views, the templates and the task-type chooser all
ask these functions, so the button a user sees and the request the server
accepts can never disagree. Role checks are imported from `acts.permissions` —
that is where «what role is this user» is answered for the whole project, and a
second implementation would be a second truth.

Nothing here decides who may *complete* an СМК task. That is
`tasks.permissions.can_complete_task()`, unchanged: an assignee of the task, or
an administrator. Creating the record and executing its measures are separate
rights on purpose.
"""

from acts.permissions import is_manager_or_admin, is_smk

from .models import SmkSource


def can_create_smk_task(user):
    """Отдел СМК, руководители и администраторы — nobody else.

    The single gate: the chooser, the form page and the POST that stores the
    record all call it, and an unauthenticated user is refused by
    `is_smk()`/`is_manager_or_admin()` before any role is read.
    """
    return is_smk(user) or is_manager_or_admin(user)


def requires_task_type_choice(user):
    """Whether this user is shown the «тип задачи» step before the form.

    Руководитель and администратор may in future create more than one kind of
    task, so they choose first. An СМК employee has exactly one kind and is
    taken straight to it — a one-option menu is not a choice.
    """
    return is_manager_or_admin(user)


def can_view_smk_source(source, user):
    """Reading an СМК record is open to every authenticated user.

    The same rule acts, protocols and tasks already apply: a task in the common
    registry links to its source, and a link that 404s for the person holding
    the task would be worse than useless.
    """
    return bool(getattr(user, 'is_authenticated', False))


def get_readable_smk_sources_queryset(user):
    """Every СМК record an authenticated user may open."""
    if not getattr(user, 'is_authenticated', False):
        return SmkSource.objects.none()
    return SmkSource.objects.select_related('created_by')
