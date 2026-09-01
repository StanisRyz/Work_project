"""Shared read-side state for the acts registry and the act detail page.

Both the full page and every live fragment go through these helpers, so a
refreshed block can never disagree with what a reload would render, and act
visibility is always decided by `acts.permissions`, never by the browser.
"""

from django.db.models import Count, Q
from django.utils import timezone

from references.models import ActStatus

from .models import Act
from .permissions import (
    can_clear_all_acts,
    can_create_act,
    get_all_visible_acts_queryset,
    get_archived_acts_queryset,
)
from .services import get_visible_acts_for_user


SCOPES = ('my', 'all', 'archive')
DUE_CHOICES = ('', 'overdue', 'not_overdue')

# The route the act travels, and which step each status sits on.
ROUTE_STEPS = (
    ('Создан ОТК', 'CREATED_OTK'),
    ('Рассмотрение КО', 'KO_REVIEW'),
    ('Анализ ТО', 'TO_ANALYSIS'),
    ('Проверка ОТК', 'OTK_REVIEW'),
    ('Архивирован', 'ARCHIVED'),
)

ROUTE_INDEX = {
    'CREATED_OTK': 0,
    'KO_REVIEW': 1,
    'TO_ANALYSIS': 2,
    'OTK_REVIEW': 3,
    'ACTIONS_ASSIGNED': 3,
    'ARCHIVED': 4,
}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def build_act_list_state(user, query_params):
    """Return everything the registry needs for `user` and these GET params."""
    scope = query_params.get('scope', 'my')
    if scope not in SCOPES:
        scope = 'my'

    active_acts = get_visible_acts_for_user(user)
    if scope == 'archive':
        visible_acts = get_archived_acts_queryset(user)
    elif scope == 'all':
        visible_acts = get_all_visible_acts_queryset(user).select_related(
            'created_by', 'priority', 'status'
        ).exclude(status__code='ARCHIVED')
    else:
        visible_acts = active_acts.exclude(status__code='ARCHIVED')
    has_visible_acts = visible_acts.exists()
    today = timezone.localdate()

    status = query_params.get('status')
    act_type = query_params.get('act_type', '')
    due = query_params.get('due', '')
    search = query_params.get('search', '').strip()
    if due not in DUE_CHOICES:
        due = ''
    if act_type not in Act.Type.values:
        act_type = ''

    acts = visible_acts
    if status:
        acts = acts.filter(status_id=status)
    if act_type:
        acts = acts.filter(act_type=act_type)
    if due == 'overdue':
        acts = acts.filter(due_date__lt=today)
    elif due == 'not_overdue':
        acts = acts.filter(due_date__gte=today)
    if search:
        # Canonical ownership: the number and the nomenclature belong to the
        # act, ЗНП and the party number to its defects. Every defect matches,
        # not just the first one, so `distinct()` collapses the join.
        acts = acts.filter(
            Q(number__icontains=search)
            | Q(nomenclature__icontains=search)
            | Q(defects__znp_number__icontains=search)
            | Q(defects__party_number__icontains=search)
        ).distinct()
    has_filters = bool(status or act_type or due or search)
    kpis = {
        'total': acts.count(),
        'overdue': acts.filter(due_date__lt=today).count(),
        'created_otk': acts.filter(status__code='CREATED_OTK').count(),
        'ko_review': acts.filter(status__code='KO_REVIEW').count(),
        'to_analysis': acts.filter(status__code='TO_ANALYSIS').count(),
    }

    return {
        # `distinct=True`: the ЗНП search joins the defects table, which would
        # otherwise multiply the count.
        'acts': acts.annotate(defects_total=Count('defects', distinct=True)),
        'kpis': kpis,
        'today': today,
        'has_visible_acts': has_visible_acts,
        'has_filters': has_filters,
        'statuses': ActStatus.objects.filter(is_active=True).exclude(
            code__in={'ACTIONS_ASSIGNED', 'CLOSED', 'CANCELLED'}
        ),
        'act_types': Act.Type.choices,
        'selected': {
            'scope': scope,
            'status': status or '',
            'act_type': act_type,
            'due': due,
            'search': search,
        },
        'can_create': can_create_act(user),
        'can_clear_all_acts': can_clear_all_acts(user),
        'scope': scope,
    }


# --------------------------------------------------------------------------
# Detail page building blocks
# --------------------------------------------------------------------------

def build_route_steps(act):
    """Route with each step marked completed / current / future."""
    dates = {
        'CREATED_OTK': act.created_at,
        'KO_REVIEW': act.ko_decision_at,
        'TO_ANALYSIS': act.to_analysis_at,
        'OTK_REVIEW': act.approved_at,
        'ARCHIVED': act.approved_at,
    }
    status_code = getattr(act.status, 'code', '')
    current_index = ROUTE_INDEX.get(status_code, 0)
    steps = []
    for index, (name, code) in enumerate(ROUTE_STEPS):
        if index < current_index or (status_code == 'ARCHIVED' and index == current_index):
            state = 'completed'
        elif index == current_index:
            state = 'current'
        else:
            state = 'future'
        steps.append({'name': name, 'date': dates.get(code), 'state': state})
    return steps


def get_history_events(act):
    return list(act.history_events.select_related('user', 'from_status', 'to_status'))


def group_history_events(history_events):
    """Group events into local-date buckets, preserving their order."""
    groups = []
    for event in history_events:
        event_date = timezone.localtime(event.created_at).date()
        if not groups or groups[-1]['date'] != event_date:
            groups.append({'date': event_date, 'events': []})
        groups[-1]['events'].append(event)
    return groups


def get_act_comments(act):
    return act.comments.select_related('author')


def get_related_tasks(act, user):
    """«Связанные мероприятия» — the corrective-action tasks of this act.

    `source_type=ACT` only. An `ACT_WORKFLOW` row is a routing entry for the
    stage the act waits on, not work created by the ТО analysis, and listing it
    here would turn the section into a second copy of the act's own route.
    """
    from tasks.models import Task
    from tasks.permissions import get_readable_tasks_queryset

    return (
        get_readable_tasks_queryset(user)
        .filter(act=act, source_type=Task.SourceType.ACT)
        .select_related('department', 'status', 'root_analysis', 'completed_by')
        .prefetch_related('assignees__user__userprofile')
    )
