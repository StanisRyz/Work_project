"""«What is waiting for me», counted once for the menu and the landing page.

Every number is the owning module's own queryset, never a restated rule:
- «Акты» — `acts.permissions.get_visible_acts_queryset()`, the «Мои акты»
  work queue the registry's first tab shows (archived acts excluded, as there);
- «Задачи» — the «Мои задачи» tab: `get_visible_tasks_queryset()` narrowed to
  the user's own assignments, closed ones excluded;
- «Протоколы» — `ProtocolApproval` rows still `PENDING` for this user on the
  *current* revision of a protocol that is in approval: exactly the approvals
  the protocol page offers them «Согласовать» on.
"""

from django.db.models import F
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from acts.permissions import get_visible_acts_queryset
from protocols.models import Protocol, ProtocolApproval
from tasks.permissions import get_visible_tasks_queryset


def my_work_counts(user):
    if not getattr(user, 'is_authenticated', False):
        return {}
    today = timezone.localdate()
    acts = get_visible_acts_queryset(user).exclude(status__code='ARCHIVED')
    tasks = (
        get_visible_tasks_queryset(user).filter(assignees__user=user)
        .exclude(status__is_final=True).distinct()
    )
    approvals = ProtocolApproval.objects.filter(
        user=user,
        status=ProtocolApproval.Status.PENDING,
        protocol__status=Protocol.Status.APPROVAL,
        revision=F('protocol__revision'),
    )
    counts = {
        'acts': acts.count(),
        'acts_overdue': acts.filter(due_date__lt=today).count(),
        'tasks': tasks.count(),
        'tasks_overdue': tasks.filter(due_date__lt=today).count(),
        'approvals': approvals.count(),
    }
    counts['quality'] = counts['acts'] + counts['tasks'] + counts['approvals']
    return counts


def my_work_counts_context(request):
    """`my_counts` for the menu — lazy, so a live fragment rendered with a
    `RequestContext` never pays for counts it does not display."""
    return {'my_counts': SimpleLazyObject(lambda: my_work_counts(request.user))}
