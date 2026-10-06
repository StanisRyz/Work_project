"""The «Подразделение | Сотрудник» directory every person picker is drawn from.

One function for every form that picks people the same way — the protocol
editor, the СМК form and a board's «Новая доска» and «Участники»: the page
renders every active employee once, tagged with `data-department-id`, and the
browser (`static/js/employee_picker.js`) only filters what is already there.
There is no directory endpoint, and whoever is posted back is checked again by
the form or the service that receives it.
"""

from django.contrib.auth.models import User

from .models import Department


def get_employee_directory():
    """`{'departments', 'employees'}`: active departments and active employees.

    An employee is an active account with an active profile; each carries its
    department through `select_related`, so the options cost one query.
    """
    return {
        'departments': Department.objects.filter(is_active=True),
        'employees': User.objects.filter(is_active=True, userprofile__is_active=True)
        .select_related('userprofile__department')
        .order_by('last_name', 'first_name', 'username'),
    }
