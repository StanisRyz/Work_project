"""The one way an СМК record — and the tasks it produces — is written.

A single entry point: `create_smk_source()`. The record, its findings, its
measures and the real `tasks.Task` rows appear together or not at all, because
a record whose measures reached nobody is worse than no record. The permission
is re-checked here, under the transaction, and not only in the view.

`tasks.services.create_smk_action_task()` owns the task itself; this module
owns the decision to create one. That split is the same one
`protocols/services.py` keeps, and it is why no second task system exists.
"""

import logging

from django.db import transaction

from ecosystem.logging_utils import log_event

from .models import (
    SmkActionAssignee,
    SmkCorrectiveAction,
    SmkNonConformity,
    SmkSource,
)
from .permissions import can_create_smk_task


logger = logging.getLogger('ecosystem.workflow')


class SmkWorkflowError(Exception):
    pass


def create_smk_source(*, origin, non_conformities, actions, created_by):
    """Store one СМК record and turn every measure into a real task.

    `non_conformities` is a list of strings; `actions` a list of
    `{'text', 'department', 'due_date', 'assignees'}` dicts as
    `SmkSourceForm` produces them. Both are already validated — this function
    checks the *right* to write, not the shape of what is written, and a
    malformed structure is a programming error rather than a user error.
    """
    from tasks.services import TaskWorkflowError, create_smk_action_task

    if not can_create_smk_task(created_by):
        log_event(
            logger,
            'INFO',
            'smk.operation_rejected',
            operation='create_source',
            actor_user_id=getattr(created_by, 'pk', None),
            reason='not_permitted',
            outcome='rejected',
        )
        raise SmkWorkflowError('Создание задачи СМК недоступно.')
    if not actions:
        raise SmkWorkflowError('Добавьте хотя бы одно корректирующее мероприятие.')

    with transaction.atomic():
        source = SmkSource.objects.create(origin=origin, created_by=created_by)
        SmkNonConformity.objects.bulk_create(
            [
                SmkNonConformity(source=source, text=text, display_order=index)
                for index, text in enumerate(non_conformities)
            ]
        )
        for index, item in enumerate(actions):
            action = SmkCorrectiveAction.objects.create(
                source=source,
                task_text=item['text'],
                department=item['department'],
                due_date=item['due_date'],
                display_order=index,
            )
            SmkActionAssignee.objects.bulk_create(
                [
                    SmkActionAssignee(action=action, user=user)
                    for user in item['assignees']
                ]
            )
            try:
                create_smk_action_task(
                    source,
                    action,
                    [user.pk for user in item['assignees']],
                    created_by=created_by,
                )
            except TaskWorkflowError as exc:
                # Rolls the whole record back: half an audit record, with some
                # measures assigned and others silently lost, is not a state
                # this module ever leaves behind.
                raise SmkWorkflowError(str(exc)) from exc
    log_event(
        logger,
        'INFO',
        'smk.source_created',
        smk_source_id=source.pk,
        origin=source.origin,
        actor_user_id=created_by.pk,
        non_conformity_count=len(non_conformities),
        action_count=len(actions),
        outcome='ok',
    )
    return source
