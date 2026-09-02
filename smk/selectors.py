"""Read-side state for the СМК pages.

Reads only — every write stays in `smk/services.py`, and every permission in
`smk/permissions.py`.
"""

from django.contrib.auth.models import User

from accounts.models import Department


def get_editor_directory():
    """The department/employee options the form's selectors are built from.

    The same mechanism the protocol editor and the ТО analysis form use: the
    page renders every active employee once, tagged with `data-department-id`,
    and the browser only filters what is already there. No directory endpoint
    is involved, and the server re-checks the department of every submitted
    employee anyway.
    """
    return {
        'departments': Department.objects.filter(is_active=True),
        'employees': User.objects.filter(is_active=True, userprofile__is_active=True)
        .select_related('userprofile__department')
        .order_by('last_name', 'first_name', 'username'),
    }


def get_source_detail(source):
    """One СМК record with its findings, its measures and their real tasks.

    The tasks are read through `SmkCorrectiveAction.tasks` rather than looked
    up by source type: the relation is what links a measure to the work it
    produced, and each measure has at most one task by database constraint.
    """
    actions = (
        source.actions.select_related('department')
        .prefetch_related('assignees__user', 'tasks__status')
    )
    return {
        'source': source,
        'non_conformities': list(source.non_conformities.all()),
        'actions': [
            {
                'action': action,
                'assignees': [item.user for item in action.assignees.all()],
                'task': next(iter(action.tasks.all()), None),
            }
            for action in actions
        ],
    }
