"""Post-import smoke checks for the migrated PostgreSQL database.

Two independent suites:

* :func:`run_read_only_checks` touches nothing — it loads the real migrated
  rows through the same querysets and permission helpers the views use.
* :func:`run_write_checks` exercises a full act → defect → analysis → task →
  notification round trip inside one transaction that is **always** rolled
  back, then proves nothing survived.

Neither suite sends email or performs any external action;
`EMAIL_NOTIFICATIONS_ENABLED` must stay false and is verified first.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts import permissions as act_permissions
from acts.models import (
    Act,
    ActAttachment,
    ActComment,
    ActCorrectiveAction,
    ActCorrectiveActionAssignee,
    ActDefect,
    ActHistoryEvent,
    ActNumberSequence,
    ActRootAnalysis,
)
from notifications.models import Notification, NotificationDelivery
from references.models import ActStatus, DefectType, Operation, Priority, TaskStatus
from tasks import permissions as task_permissions
from tasks.models import Task, TaskAssignee
from tasks.services import TaskWorkflowError, complete_task

from .database_transfer import (
    TransferError,
    normalize_relative_path,
    resolve_inside,
    safe_path_label,
)


SMOKE_USERNAME = 'smoke_check_temporary_user'
SMOKE_DEPARTMENT_CODE = 'SMOKE_CHECK_TEMP_DEPARTMENT'
SMOKE_MARKER = 'smoke-check-temporary'


class _Rollback(Exception):
    """Internal signal used to always roll the write suite back."""


class CheckCollector:
    def __init__(self, kind):
        self.kind = kind
        self.checks = []

    def record(self, name, ok, details, **extra):
        entry = {'name': name, 'status': 'ok' if ok else 'failed', 'details': details}
        entry.update(extra)
        self.checks.append(entry)
        return entry

    def run(self, name, function):
        """Run one check; any unhandled problem becomes a recorded failure."""
        try:
            details = function()
        except Exception as exc:  # noqa: BLE001 - every failure must be reported
            return self.record(name, False, f'{type(exc).__name__}: {exc}')
        return self.record(name, True, details)

    def run_isolated(self, name, function):
        """Same, but inside a savepoint so a failure cannot poison the outer
        transaction the write suite runs in."""
        try:
            with transaction.atomic():
                details = function()
        except Exception as exc:  # noqa: BLE001 - every failure must be reported
            return self.record(name, False, f'{type(exc).__name__}: {exc}')
        return self.record(name, True, details)

    @property
    def failures(self):
        return [check for check in self.checks if check['status'] == 'failed']

    def as_dict(self):
        return {
            'kind': self.kind,
            'ok': not self.failures,
            'checks': self.checks,
            'failures': [check['name'] for check in self.failures],
        }


def require_postgresql():
    if connection.vendor != 'postgresql':
        raise TransferError(
            f'Smoke-проверки выполняются только на PostgreSQL, текущий backend — '
            f'{connection.vendor}.'
        )


def require_email_disabled():
    if getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
        raise TransferError(
            'Smoke-проверки запрещены при EMAIL_NOTIFICATIONS_ENABLED=true: '
            'реальная отправка email во время репетиции недопустима.'
        )


# --------------------------------------------------------------------------
# Read-only suite
# --------------------------------------------------------------------------

def run_read_only_checks():
    collector = CheckCollector('read')

    collector.run('users_and_profiles', _check_users_and_profiles)
    collector.run('acts_registry', _check_acts_registry)
    collector.run('defects_history_comments', _check_defects_history_comments)
    collector.run('to_analysis', _check_to_analysis)
    collector.run('tasks', _check_tasks)
    collector.run('notifications', _check_notifications)
    collector.run('permissions', _check_permissions)
    collector.run('attachment_files', _check_attachment_files)
    collector.run('view_queries', _check_view_queries)

    return collector.as_dict()


def _check_users_and_profiles():
    users = list(User.objects.select_related('userprofile').order_by('pk')[:200])
    profiles = UserProfile.objects.select_related('user', 'department').count()
    without_profile = [user.username for user in users if not hasattr(user, 'userprofile')]
    if without_profile:
        raise AssertionError(
            'Пользователи без профиля: ' + ', '.join(sorted(without_profile)[:10]) + '.'
        )
    for user in users:
        # Touching the related objects proves the FK actually resolves.
        user.userprofile.display_name
        user.userprofile.department_label
    return f'Пользователей — {User.objects.count()}, профилей — {profiles}.'


def _check_acts_registry():
    active_codes = ['CREATED_OTK', 'KO_REVIEW', 'TO_ANALYSIS', 'OTK_REVIEW', 'ACTIONS_ASSIGNED']
    active = Act.objects.select_related(
        'created_by', 'operation', 'defect_type', 'priority', 'status'
    ).filter(status__code__in=active_codes)
    archived = Act.objects.select_related('status').filter(status__code='ARCHIVED')
    for act in list(active[:100]) + list(archived[:100]):
        str(act)
        act.status.name
        act.operation.name
        act.defect_type.name
    return (
        f'Всего актов — {Act.objects.count()}, активных — {active.count()}, '
        f'архивных — {archived.count()}.'
    )


def _check_defects_history_comments():
    defects = ActDefect.objects.select_related('act', 'defect_type', 'operation')
    history = ActHistoryEvent.objects.select_related('act', 'user', 'from_status', 'to_status')
    comments = ActComment.objects.select_related('act', 'author')
    for defect in defects[:200]:
        defect.defect_type.name
        defect.get_workshop_display()
        defect.get_ko_decision_display()
    for event in history[:200]:
        event.get_event_type_display()
    for comment in comments[:200]:
        str(comment)
    return (
        f'Дефектов — {defects.count()}, событий истории — {history.count()}, '
        f'комментариев — {comments.count()}.'
    )


def _check_to_analysis():
    roots = ActRootAnalysis.objects.select_related('act').prefetch_related(
        'corrective_actions__assignees__user'
    )
    actions = ActCorrectiveAction.objects.select_related('root_analysis__act', 'department')
    assignees = ActCorrectiveActionAssignee.objects.select_related(
        'corrective_action', 'user'
    )
    for root in roots[:100]:
        for action in root.corrective_actions.all():
            action.department.name
            for assignee in action.assignees.all():
                assignee.user.get_username()
    return (
        f'Корневых проработок — {roots.count()}, мероприятий — {actions.count()}, '
        f'исполнителей мероприятий — {assignees.count()}.'
    )


def _check_tasks():
    tasks = Task.objects.select_related(
        'status', 'act', 'department', 'root_analysis', 'completed_by', 'source_action'
    ).prefetch_related('assignees__user')
    for task in tasks[:200]:
        task.status.name
        task.act.number
        for assignee in task.assignees.all():
            assignee.user.get_username()
    completed = tasks.filter(status__code='COMPLETED').count()
    return (
        f'Задач — {tasks.count()}, исполнителей задач — {TaskAssignee.objects.count()}, '
        f'завершённых — {completed}.'
    )


def _check_notifications():
    notifications = Notification.objects.select_related('recipient', 'actor', 'related_act')
    deliveries = NotificationDelivery.objects.select_related('notification')
    for notification in notifications[:200]:
        notification.get_event_type_display()
        notification.related_act.number
    statuses = {}
    for status, _label in NotificationDelivery.Status.choices:
        statuses[status] = deliveries.filter(status=status).count()
    unread = notifications.filter(is_read=False).count()
    return (
        f'Уведомлений — {notifications.count()} (непрочитанных — {unread}), '
        f'доставок — {deliveries.count()}, по статусам: {statuses}.'
    )


def _check_permissions():
    users = list(User.objects.select_related('userprofile').order_by('pk')[:50])
    if not users:
        return 'Пользователей нет — проверять права не на чем.'
    acts = list(Act.objects.select_related('status', 'created_by').order_by('pk')[:50])
    attachments = list(ActAttachment.objects.select_related('act__status')[:20])
    tasks = list(Task.objects.select_related('status')[:20])
    evaluated = 0
    for user in users:
        act_permissions.get_visible_acts_queryset(user).count()
        act_permissions.get_archived_acts_queryset(user).count()
        act_permissions.can_create_act(user)
        act_permissions.has_full_act_access(user)
        task_permissions.get_visible_tasks_queryset(user).count()
        for act in acts:
            act_permissions.can_view_act(act, user)
            act_permissions.can_send_to_ko(act, user)
            act_permissions.can_apply_ko_decision(act, user)
            act_permissions.can_apply_to_analysis(act, user)
            act_permissions.can_approve_act(act, user)
            act_permissions.can_close_act(act, user)
            evaluated += 1
        for attachment in attachments:
            act_permissions.can_download_attachment(attachment, user)
            act_permissions.can_delete_attachment(attachment, user)
        for task in tasks:
            task_permissions.can_view_task(task, user)
            task_permissions.can_complete_task(task, user)
    return f'Проверено сочетаний пользователь/акт — {evaluated}.'


def _check_attachment_files():
    media_root = Path(settings.MEDIA_ROOT)
    missing = []
    checked = 0
    for attachment_pk, raw_name in ActAttachment.objects.order_by('pk').values_list('pk', 'file'):
        if not (raw_name or '').strip():
            missing.append(f'id={attachment_pk}: пустой путь')
            continue
        relative = normalize_relative_path(raw_name)
        target = resolve_inside(media_root, relative)
        if not target.is_file():
            missing.append(f'id={attachment_pk}: {relative}')
            continue
        checked += 1
    if missing:
        raise AssertionError(
            f'Отсутствуют файлы вложений ({len(missing)}): ' + '; '.join(missing[:10]) + '.'
        )
    return f'Все файлы вложений на месте — {checked} в {safe_path_label(media_root)}.'


def _check_view_queries():
    """Repeat the shaped queries the registries and detail pages issue."""
    today = timezone.localdate()
    Act.objects.filter(due_date__lt=today).select_related('status').count()
    Act.objects.filter(due_date__gte=today).select_related('status').count()
    Act.objects.filter(act_type=Act.Type.OPERATIONAL_CONTROL).count()
    Act.objects.filter(number__icontains='АОК').order_by('-created_at').count()
    Task.objects.filter(status__code='IN_PROGRESS').order_by('due_date').count()
    Task.objects.filter(due_date__lt=today, status__code='IN_PROGRESS').count()
    len(list(Notification.objects.filter(is_read=False).order_by('-created_at')[:5]))
    len(
        list(
            NotificationDelivery.objects.filter(
                status=NotificationDelivery.Status.PENDING
            ).order_by('available_at')[:100]
        )
    )
    ActStatus.objects.count()
    TaskStatus.objects.count()
    Operation.objects.filter(is_active=True).count()
    DefectType.objects.filter(is_active=True).count()
    Priority.objects.count()
    Department.objects.filter(is_active=True).count()
    counters = list(ActNumberSequence.objects.order_by('year').values_list('year', 'last_value'))
    return f'Типовые запросы выполнены, счётчики номеров — {counters}.'


# --------------------------------------------------------------------------
# Write suite (always rolled back)
# --------------------------------------------------------------------------

def run_write_checks():
    collector = CheckCollector('write')
    state = {}

    try:
        with transaction.atomic():
            _write_scenario(collector, state)
            raise _Rollback
    except _Rollback:
        pass

    leftovers = _find_leftovers()
    collector.record(
        'rollback',
        not leftovers,
        'Тестовые данные не сохранились.' if not leftovers
        else 'После отката остались записи: ' + '; '.join(leftovers) + '.',
    )
    return collector.as_dict()


def _write_scenario(collector, state):
    collector.run_isolated('create_user', lambda: _create_user(state))
    collector.run_isolated('user_profile_created', lambda: _check_created_profile(state))
    collector.run_isolated('create_act', lambda: _create_act(state))
    collector.run_isolated('next_act_number', lambda: _next_act_number(state))
    collector.run_isolated('create_defect', lambda: _create_defect(state))
    collector.run_isolated('add_comment', lambda: _add_comment(state))
    collector.run_isolated('create_history', lambda: _create_history(state))
    collector.run_isolated('create_corrective_action', lambda: _create_corrective_action(state))
    collector.run_isolated('create_task', lambda: _create_task(state))
    collector.run_isolated('create_notification', lambda: _create_notification(state))
    collector.run_isolated('complete_task', lambda: _complete_task(state))
    collector.run_isolated('constraints', lambda: _check_constraints(state))
    collector.run_isolated('relations', lambda: _check_relations(state))


def _reference(model, description):
    instance = model.objects.order_by('pk').first()
    if instance is None:
        raise AssertionError(f'В базе нет ни одной записи {description}.')
    return instance


def _create_user(state):
    user = User.objects.create_user(
        username=SMOKE_USERNAME,
        first_name='Проверка',
        last_name='Переноса',
    )
    # The account exists only inside the rolled-back transaction and must never
    # be loginable, so it gets no usable password at all.
    user.set_unusable_password()
    user.save(update_fields=['password'])
    state['user'] = user
    return f'Временный пользователь создан (pk={user.pk}).'


def _check_created_profile(state):
    profile = UserProfile.objects.get(user=state['user'])
    state['profile'] = profile
    return f'UserProfile создан автоматически (роль — {profile.role}).'


def _create_act(state):
    status = ActStatus.objects.filter(code='CREATED_OTK').first() or _reference(
        ActStatus, 'ActStatus'
    )
    act = Act.objects.create(
        created_by=state['user'],
        party_number='SMOKE-000',
        nomenclature=SMOKE_MARKER,
        operation=_reference(Operation, 'Operation'),
        defect_type=_reference(DefectType, 'DefectType'),
        status=status,
        description=SMOKE_MARKER,
    )
    state['act'] = act
    return f'Акт создан с автоматическим номером {act.number}.'


def _next_act_number(state):
    year = timezone.localdate().year
    counter = ActNumberSequence.objects.get(year=year)
    expected = Act._format_number(year, counter.last_value)
    if state['act'].number != expected:
        raise AssertionError(
            f'Номер акта {state["act"].number} не совпадает со счётчиком {expected}.'
        )
    following = Act.objects.create(
        created_by=state['user'],
        party_number='SMOKE-001',
        nomenclature=SMOKE_MARKER,
        operation=_reference(Operation, 'Operation'),
        defect_type=_reference(DefectType, 'DefectType'),
        status=state['act'].status,
        description=SMOKE_MARKER,
    )
    state['second_act'] = following
    if following.number == state['act'].number:
        raise AssertionError('Второй акт получил тот же номер.')
    return f'Следующий автоматический номер выдан корректно: {following.number}.'


def _create_defect(state):
    defect = ActDefect.objects.create(
        act=state['act'],
        defect_type=_reference(DefectType, 'DefectType'),
        workshop=ActDefect.Workshop.MP_SHOP,
        description=SMOKE_MARKER,
        detected_at=timezone.localdate(),
    )
    state['defect'] = defect
    return f'Дефект создан (pk={defect.pk}).'


def _add_comment(state):
    comment = ActComment.objects.create(
        act=state['act'], author=state['user'], text=SMOKE_MARKER
    )
    state['comment'] = comment
    return f'Комментарий добавлен (pk={comment.pk}).'


def _create_history(state):
    event = ActHistoryEvent.objects.create(
        act=state['act'],
        user=state['user'],
        event_type=ActHistoryEvent.EventType.CREATED,
        message=SMOKE_MARKER,
        to_status=state['act'].status,
    )
    state['history'] = event
    return f'Событие истории создано (pk={event.pk}).'


def _create_corrective_action(state):
    department, _created = Department.objects.get_or_create(
        code=SMOKE_DEPARTMENT_CODE, defaults={'name': 'Временное подразделение проверки'}
    )
    state['department'] = department
    root = ActRootAnalysis.objects.create(act=state['act'], root_cause=SMOKE_MARKER)
    action = ActCorrectiveAction.objects.create(
        root_analysis=root,
        comment=SMOKE_MARKER,
        department=department,
        due_date=timezone.localdate() + timedelta(days=7),
    )
    assignee = ActCorrectiveActionAssignee.objects.create(
        corrective_action=action, user=state['user']
    )
    state['root'] = root
    state['action'] = action
    state['action_assignee'] = assignee
    return f'Корневая проработка и мероприятие созданы (pk={action.pk}).'


def _create_task(state):
    status = TaskStatus.objects.filter(code='IN_PROGRESS').first()
    if status is None:
        raise AssertionError('Не найден статус задачи IN_PROGRESS.')
    task = Task.objects.create(
        source_action=state['action'],
        act=state['act'],
        root_analysis=state['root'],
        task_text=SMOKE_MARKER,
        department=state['department'],
        due_date=timezone.localdate() + timedelta(days=7),
        created_by=state['user'],
        status=status,
    )
    assignee = TaskAssignee.objects.create(task=task, user=state['user'])
    state['task'] = task
    state['task_assignee'] = assignee
    return f'Задача и исполнитель созданы (pk={task.pk}).'


def _create_notification(state):
    notification = Notification.objects.create(
        recipient=state['user'],
        actor=state['user'],
        event_type=Notification.EventType.ACTION_ASSIGNED,
        title=SMOKE_MARKER,
        message=SMOKE_MARKER,
        related_act=state['act'],
        deduplication_key=f'{SMOKE_MARKER}:{state["act"].pk}',
    )
    state['notification'] = notification
    if NotificationDelivery.objects.filter(notification=notification).exists():
        raise AssertionError('Создание уведомления неожиданно создало доставку email.')
    return f'Внутреннее уведомление создано (pk={notification.pk}), доставка email не создана.'


def _complete_task(state):
    try:
        completed = complete_task(state['task'], state['user'], 'Проверка переноса выполнена.')
    except TaskWorkflowError as exc:
        raise AssertionError(f'Завершение тестовой задачи отклонено: {exc}.') from exc
    if completed.status.code != 'COMPLETED':
        raise AssertionError(f'Статус задачи после завершения — {completed.status.code}.')
    if completed.completed_by_id != state['user'].pk or completed.completed_at is None:
        raise AssertionError('Не заполнены сведения о завершении задачи.')
    return 'Тестовая задача завершена и переведена в статус COMPLETED.'


def _check_constraints(state):
    checked = []
    with transaction.atomic():
        try:
            with transaction.atomic():
                TaskAssignee.objects.create(task=state['task'], user=state['user'])
        except IntegrityError:
            checked.append('unique_task_assignee')
        else:
            raise AssertionError('Дубликат TaskAssignee не был отклонён.')

        try:
            with transaction.atomic():
                ActCorrectiveActionAssignee.objects.create(
                    corrective_action=state['action'], user=state['user']
                )
        except IntegrityError:
            checked.append('unique_corrective_action_assignee')
        else:
            raise AssertionError('Дубликат ActCorrectiveActionAssignee не был отклонён.')

        try:
            with transaction.atomic():
                Notification.objects.create(
                    recipient=state['user'],
                    event_type=Notification.EventType.ACTION_ASSIGNED,
                    title=SMOKE_MARKER,
                    message=SMOKE_MARKER,
                    related_act=state['act'],
                    deduplication_key=state['notification'].deduplication_key,
                )
        except IntegrityError:
            checked.append('unique_notification_recipient_event')
        else:
            raise AssertionError('Дубликат уведомления не был отклонён.')

        try:
            with transaction.atomic():
                Act.objects.create(
                    number=state['act'].number,
                    created_by=state['user'],
                    party_number='SMOKE-DUP',
                    nomenclature=SMOKE_MARKER,
                    operation=_reference(Operation, 'Operation'),
                    defect_type=_reference(DefectType, 'DefectType'),
                    status=state['act'].status,
                    description=SMOKE_MARKER,
                )
        except IntegrityError:
            checked.append('unique_act_number')
        else:
            raise AssertionError('Дубликат номера акта не был отклонён.')
    return 'Ограничения сработали: ' + ', '.join(checked) + '.'


def _check_relations(state):
    act = Act.objects.select_related('status', 'created_by').get(pk=state['act'].pk)
    if act.defects.count() != 1:
        raise AssertionError('Связь акт → дефекты нарушена.')
    if act.comments.count() != 1 or act.history_events.count() != 1:
        raise AssertionError('Связи акт → комментарии/история нарушены.')
    if act.root_analyses.count() != 1:
        raise AssertionError('Связь акт → корневые проработки нарушена.')
    task = Task.objects.select_related('act', 'root_analysis', 'source_action').get(
        pk=state['task'].pk
    )
    if task.act_id != act.pk or task.root_analysis_id != state['root'].pk:
        raise AssertionError('Связи задачи с актом и корневой проработкой нарушены.')
    if task.source_action.root_analysis_id != task.root_analysis_id:
        raise AssertionError('Задача ссылается на мероприятие другой корневой проработки.')
    if Notification.objects.filter(related_act=act, recipient=state['user']).count() != 1:
        raise AssertionError('Связь уведомление → акт нарушена.')
    return 'Связи акта, мероприятия, задачи и уведомления согласованы.'


def _find_leftovers():
    leftovers = []
    if User.objects.filter(username=SMOKE_USERNAME).exists():
        leftovers.append('auth.User')
    if UserProfile.objects.filter(user__username=SMOKE_USERNAME).exists():
        leftovers.append('accounts.UserProfile')
    if Department.objects.filter(code=SMOKE_DEPARTMENT_CODE).exists():
        leftovers.append('accounts.Department')
    if Act.objects.filter(nomenclature=SMOKE_MARKER).exists():
        leftovers.append('acts.Act')
    if ActDefect.objects.filter(description=SMOKE_MARKER).exists():
        leftovers.append('acts.ActDefect')
    if ActComment.objects.filter(text=SMOKE_MARKER).exists():
        leftovers.append('acts.ActComment')
    if ActHistoryEvent.objects.filter(message=SMOKE_MARKER).exists():
        leftovers.append('acts.ActHistoryEvent')
    if ActRootAnalysis.objects.filter(root_cause=SMOKE_MARKER).exists():
        leftovers.append('acts.ActRootAnalysis')
    if ActCorrectiveAction.objects.filter(comment=SMOKE_MARKER).exists():
        leftovers.append('acts.ActCorrectiveAction')
    if Task.objects.filter(task_text=SMOKE_MARKER).exists():
        leftovers.append('tasks.Task')
    if Notification.objects.filter(title=SMOKE_MARKER).exists():
        leftovers.append('notifications.Notification')
    return leftovers


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_smoke_checks(include_write=True):
    require_postgresql()
    require_email_disabled()

    started = datetime.now(dt_timezone.utc)
    read = run_read_only_checks()
    write = run_write_checks() if include_write else {'kind': 'write', 'ok': None, 'checks': []}
    finished = datetime.now(dt_timezone.utc)

    return {
        'checked_at': started.isoformat(),
        'duration_seconds': round((finished - started).total_seconds(), 3),
        'vendor': connection.vendor,
        'email_notifications_enabled': bool(
            getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False)
        ),
        'read': read,
        'write': write,
        'ok': read['ok'] and (write['ok'] in (True, None)),
    }
