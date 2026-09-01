"""How a task's origin and its real outcome are shown, in one place.

The registry, the live fragment and the task page all need the same two
answers — «откуда эта задача» and «что с ней на самом деле стало» — and a task
can come from an act or from either kind of protocol row. Branching on
`source_type` in each template would mean three copies of the same conditional
drifting apart, so it happens here once and the templates render the result.

Reads only, and never a permission check. Links are built with `reverse()`, so
a moved route follows automatically and no public URL is spelled out.
"""

from django.urls import reverse

from protocols.selectors import APPROVAL_STATUS_VARIANTS

from .models import Task


def describe_task_source(task):
    """`{'label', 'url'}` for whatever produced this task.

    Branching is on `source_type`, never on which nullable relation happens to
    be filled: a NULL cannot tell an absent origin from a wrong one.
    """
    if task.source_type in {
        Task.SourceType.ACT,
        Task.SourceType.ACT_WORKFLOW,
        Task.SourceType.ACT_REJECTION,
    }:
        if task.act_id is None:
            return {'label': '', 'url': ''}
        return {'label': task.act.number, 'url': reverse('acts:detail', args=[task.act_id])}
    if task.protocol_id is None:
        return {'label': '', 'url': ''}
    return {
        'label': f'{task.protocol.protocol_type.name} №{task.protocol.number}',
        'url': reverse('protocols:detail', args=[task.protocol_id]),
    }


def get_task_approval(task):
    """The `ProtocolApproval` this queue entry belongs to, or `None`."""
    if task.source_type != Task.SourceType.PROTOCOL_APPROVAL:
        return None
    # A reverse one-to-one raises rather than returning None when absent.
    return getattr(task, 'protocol_approval', None)


def describe_task_state(task):
    """What the «Статус» column says — the *business* result, not the queue's.

    For an ordinary task that is its own workflow status. An approval task is
    only a work-queue entry: it is closed as «Выполнено» both when the person
    approved and when someone else returned the protocol and cancelled their
    round. `ProtocolApproval` is the authoritative answer in that case, so a
    cancelled approval is never presented as one that was given.
    """
    approval = get_task_approval(task)
    if approval is not None:
        return {
            'label': approval.get_status_display(),
            'variant': APPROVAL_STATUS_VARIANTS.get(approval.status, 'pending'),
        }
    return {'label': str(task.status), 'variant': ''}


def describe_task_type(task):
    """«Тип задачи» for the registry and the task page.

    An `ACT_WORKFLOW` row names the stage it stands for — «Этап обработки акта:
    рассмотрение КО» — because four of them share one source type and the act
    number alone does not say what is being asked. Read from the stored
    `workflow_stage`, so an archived entry keeps its historical meaning.
    """
    label = task.get_source_type_display()
    if task.source_type == Task.SourceType.ACT_WORKFLOW and task.workflow_stage:
        return f'{label}: {task.get_workflow_stage_display().lower()}'
    return label


def describe_task(task):
    """One registry row: the task plus its source and its real state."""
    return {
        'task': task,
        'type_label': describe_task_type(task),
        'source': describe_task_source(task),
        'state': describe_task_state(task),
    }
