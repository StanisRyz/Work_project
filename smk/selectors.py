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


def build_confirmation_summary(cleaned):
    """What the confirmation step shows before anything is written.

    Built from the *validated* structure, never from the raw POST: the numbers
    and names on screen are exactly what `create_smk_source()` would store, so
    a row the form rejected can never be counted in.

    Read-only and side-effect free — it is rendered both into the page (for the
    server-side confirmation step) and, by `smk_form.js`, into the dialog
    without a round trip.
    """
    from .models import SmkSource

    assignees = []
    for action in cleaned['actions']:
        for user in action['assignees']:
            label = user.get_full_name() or user.username
            if label not in assignees:
                assignees.append(label)
    return {
        'origin_label': SmkSource.Origin(cleaned['origin']).label,
        'audit_date': cleaned['audit_date'],
        'non_conformity_count': len(cleaned['non_conformities']),
        'action_count': len(cleaned['actions']),
        'assignees': assignees,
    }


# The two tabs of the record page. `act` is the default, exactly as the act
# page defaults to its own first tab, and an unknown value falls back to it
# rather than 404ing on a bookmark.
DETAIL_TABS = ('act', 'activities')


def resolve_detail_tab(value):
    return value if value in DETAIL_TABS else DETAIL_TABS[0]


def _measure_row(action):
    """One корректирующее мероприятие with everything its card shows."""
    return {
        'action': action,
        'assignees': [item.user for item in action.assignees.all()],
        # At most one task per measure, by the `unique_smk_action_task`
        # constraint — the card links to that task, never to a search.
        'task': next(iter(action.tasks.all()), None),
    }


def get_source_detail(source):
    """One СМК record: its findings, its measures and the real tasks.

    The tasks are read through `SmkCorrectiveAction.tasks` rather than looked
    up by source type: the relation is what links a measure to the work it
    produced, and each measure has at most one task by database constraint.

    Both tabs are built from this one read, so «Акт аудита» and «Связанные
    мероприятия» can never describe different work.
    """
    rows = [
        _measure_row(action)
        for action in source.actions.select_related('department', 'non_conformity')
        .prefetch_related('assignees__user', 'tasks__status')
    ]
    return {
        'source': source,
        # A finding carries no status of its own: what is being done about it
        # is the state of the measures that name it, and a second answer here
        # could only disagree with them. The wrapper stays so the template
        # keeps one shape to read.
        'non_conformities': [
            {'item': finding} for finding in source.non_conformities.all()
        ],
        'actions': rows,
        # What «Количество задач» in the information card counts: the real
        # tasks that exist, not the measures that should have produced them.
        'task_count': sum(1 for row in rows if row['task'] is not None),
    }
