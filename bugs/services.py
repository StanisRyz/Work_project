"""The one way a bug report is written.

A single entry point: `report_bug()` stores the report, turns it into a real
`tasks.Task` for the responsible accounts and tells them about it — together or
not at all. There is no second write path and no second channel: the task comes
from `tasks.services.create_bug_report_task()` and the bell entry, the
real-time refresh and the email all come from
`notifications.services.notify_bug_reported()`, exactly as an act, a protocol
and an СМК record reach their people.

The task is what keeps a report from being read once and forgotten: it lands in
«Мои задачи» of everybody responsible, is completed with an execution comment
like any other, and is closed by whoever fixes the bug. One task per report,
shared rather than split — five people are not asked to fix one bug five times.

The permission is re-checked here, under the transaction, and not only in the
view.
"""

import logging

from django.db import transaction
from django.utils import timezone

from ecosystem.logging_utils import log_event
from ecosystem.workdays import add_working_days

from .models import BugReport
from .permissions import can_report_bug, get_bug_responsible_users


logger = logging.getLogger('ecosystem.workflow')

# Long enough for a real description, short enough that a paste of an entire
# page cannot become a notification. The form refuses more; this is the rule.
MAX_MESSAGE_LENGTH = 4000

# How long a reported bug may wait before its task is overdue. Working days
# only — `ecosystem.workdays` is the one place weekday arithmetic lives, and
# there is deliberately no holiday calendar to fall out of date. The same shape
# of constant `acts` and `protocols` keep for their own deadlines.
BUG_TASK_WORKING_DAYS = 3


class BugWorkflowError(Exception):
    pass


def report_bug(*, reporter, message, page_url=''):
    """Store one report, raise the task for it, and notify the responsible.

    `message` is the text the person typed; `page_url` is where they were, and
    it is taken from the request rather than from the submitted form — a
    reporter never states their own location. Both are already stripped by the
    caller; this function checks the *right* to report and the emptiness of the
    message, because a report saying nothing helps nobody.

    One `atomic()` block over the report, its task and its notifications: a
    failure anywhere leaves none of them behind. Returns the stored
    `BugReport`.
    """
    if not can_report_bug(reporter):
        log_event(
            logger,
            'INFO',
            'bug.operation_rejected',
            operation='report',
            actor_user_id=getattr(reporter, 'pk', None),
            reason='not_permitted',
            outcome='rejected',
        )
        raise BugWorkflowError('Отправка сообщения об ошибке недоступна.')

    message = (message or '').strip()
    if not message:
        raise BugWorkflowError('Опишите ошибку.')
    if len(message) > MAX_MESSAGE_LENGTH:
        raise BugWorkflowError('Описание слишком длинное — сократите его.')

    from notifications.services import notify_bug_reported
    from tasks.services import TaskWorkflowError, create_bug_report_task

    with transaction.atomic():
        report = BugReport.objects.create(
            reporter=reporter,
            message=message,
            page_url=(page_url or '')[:500],
        )
        # Read inside the transaction, so the recipients are the ones marked at
        # the moment the report was filed. An empty list is not an error: the
        # report is still stored and readable in Django Admin, which is where
        # somebody would notice that nobody is marked responsible — but there
        # is then nobody to raise a task on, so none is raised.
        recipients = list(get_bug_responsible_users())
        task = None
        if recipients:
            try:
                task = create_bug_report_task(
                    report,
                    [user.pk for user in recipients],
                    created_by=reporter,
                    due_date=add_working_days(
                        timezone.localdate(), BUG_TASK_WORKING_DAYS,
                    ),
                )
            except TaskWorkflowError as exc:
                # Rolls the report back with it: a report that reached nobody's
                # queue is exactly the state this function exists to prevent.
                raise BugWorkflowError(str(exc)) from exc
        # After the report and its task exist, never before: a notification
        # must not describe work a later failure would roll back. One
        # notification, not two — the task is the same fact, and telling
        # somebody about it twice is noise.
        notify_bug_reported(report, reporter, recipients)
    log_event(
        logger,
        'INFO',
        'bug.reported',
        bug_report_id=report.pk,
        task_id=getattr(task, 'pk', None),
        actor_user_id=reporter.pk,
        recipient_count=len(recipients),
        outcome='ok',
    )
    return report
