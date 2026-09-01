"""Bring acts that are already in flight onto the new deadline and task model.

Two backfills over the **active** acts only — `CREATED_OTK`, `KO_REVIEW`,
`TO_ANALYSIS`, `OTK_REVIEW`. Archived acts are historical records and are left
exactly as they are: their deadline described the old rule and their route is
over.

1. `due_date` is recomputed as the act's own creation date plus three working
   days, the same rule `acts.models.calculate_act_due_date()` applies to new
   acts. `ActDefect.detected_at` is never read.
2. The routing task the act's current stage would have is created if it is
   missing. `CREATED_OTK` gets none — that work belongs to the creator, who
   already holds the act — and a stage whose role has no eligible active user
   is skipped rather than failing.

Idempotent by construction: an act that already has an active `ACT_WORKFLOW`
task for its stage is left alone, and the deadline is derived from `created_at`
rather than from the current value, so a second run computes the same date.
Nothing is deleted, no existing corrective-action or protocol task is touched,
and no notification or realtime event is emitted — a migration is not a
workflow transition.

Historical models throughout (`apps.get_model`), so a later change to the
current ORM cannot alter what this migration did on the day it ran. The only
imports from application code are `ecosystem.workdays.add_working_days` and the
two constants below, which are pure arithmetic and plain strings.
"""

from django.db import migrations

from ecosystem.workdays import add_working_days
from django.utils import timezone


# Restated rather than imported from `acts.models` / `tasks.services`: a data
# migration must keep meaning what it meant when it was written, whatever those
# modules become later.
ACT_REVIEW_WORKING_DAYS = 3
OTK_ROLE = 'otk'
KO_ROLE = 'ko'
TO_ROLE = 'to'

# The act status the routing task belongs to → its stage and the role that has
# to act. `CREATED_OTK` is deliberately absent: it creates no task.
STAGE_BY_STATUS = {
    'KO_REVIEW': ('KO_REVIEW', KO_ROLE),
    'TO_ANALYSIS': ('TO_ANALYSIS', TO_ROLE),
    'OTK_REVIEW': ('OTK_REVIEW', OTK_ROLE),
}

ACTIVE_STATUS_CODES = ('CREATED_OTK', *STAGE_BY_STATUS)

STAGE_TEXT = {
    'KO_REVIEW': 'Рассмотреть акт и внести решение КО.',
    'TO_ANALYSIS': 'Выполнить анализ ТО по акту.',
    'OTK_REVIEW': 'Проверить акт и утвердить его или вернуть в ТО.',
}


def _active_users_for_role(User, role):
    """Active accounts with an active profile carrying `role`, by pk."""
    return list(
        User.objects.filter(
            is_active=True,
            userprofile__is_active=True,
            userprofile__role=role,
        ).order_by('pk')
    )


def backfill_active_acts(apps, schema_editor):
    Act = apps.get_model('acts', 'Act')
    Task = apps.get_model('tasks', 'Task')
    TaskAssignee = apps.get_model('tasks', 'TaskAssignee')
    TaskStatus = apps.get_model('references', 'TaskStatus')
    User = apps.get_model('auth', 'User')

    in_progress = TaskStatus.objects.filter(code='IN_PROGRESS', is_active=True).first()
    role_users = {}

    acts = (
        Act.objects.filter(status__code__in=ACTIVE_STATUS_CODES)
        .select_related('status')
        .order_by('pk')
    )
    for act in acts:
        # The act's own creation date, read in the project's local time zone so
        # the result matches what `calculate_act_due_date()` writes today.
        created_on = timezone.localtime(act.created_at).date()
        due_date = add_working_days(created_on, ACT_REVIEW_WORKING_DAYS)
        if act.due_date != due_date:
            act.due_date = due_date
            # `updated_at` is `auto_now`, so it is listed explicitly.
            act.save(update_fields=['due_date', 'updated_at'])

        stage_and_role = STAGE_BY_STATUS.get(act.status.code)
        if stage_and_role is None or in_progress is None:
            continue
        stage, role = stage_and_role

        # Already represented in the queue — nothing to add. Checked per act
        # rather than in bulk so a partially backfilled database converges.
        if Task.objects.filter(
            act=act, source_type='ACT_WORKFLOW'
        ).exclude(status__code='COMPLETED').exists():
            continue

        if role not in role_users:
            role_users[role] = _active_users_for_role(User, role)
        assignees = role_users[role]
        if not assignees:
            # A plant with no active holder of the role simply gets no task;
            # the stage still appears on the act itself.
            continue

        profile = getattr(assignees[0], 'userprofile', None)
        task = Task.objects.create(
            source_type='ACT_WORKFLOW',
            act=act,
            workflow_stage=stage,
            task_text=STAGE_TEXT[stage],
            # A routing task belongs to a role, so the department is only a
            # label; `Task.department` is nullable for exactly this source.
            department=getattr(profile, 'department', None),
            due_date=act.due_date,
            created_by=act.created_by,
            status=in_progress,
        )
        TaskAssignee.objects.bulk_create(
            [TaskAssignee(task=task, user=user) for user in assignees]
        )


class Migration(migrations.Migration):

    dependencies = [
        ('acts', '0025_act_task_split'),
        ('references', '0003_simplify_task_statuses'),
        ('tasks', '0012_act_rejection_task'),
    ]

    operations = [
        # Not reversible as data: the acts' previous deadlines were derived
        # from defect detection dates that are still on the defects, and the
        # routing tasks are new rows a rollback of the schema removes anyway.
        migrations.RunPython(backfill_active_acts, migrations.RunPython.noop),
    ]
