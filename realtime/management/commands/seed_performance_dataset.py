"""Generate a synthetic dataset large enough to measure something.

Development and test environments only. Every row is obviously synthetic — the
generated text says so — because this data must never be mistaken for, or mixed
into, real production content. Production data is never copied or used here.

Safety rules:

* dry-run by default; `--execute` is required to write anything;
* refuses to run with `DEBUG=False` unless `--i-know-this-is-not-development`
  is also given, so it cannot be aimed at a real deployment by accident;
* everything is created inside one transaction, so a failure leaves nothing
  half-written;
* the generated users get an unusable password and are marked inactive-safe
  demo accounts by their username prefix.
"""

import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


# Everything this command creates carries the marker, so it is always obvious
# what is synthetic and a later cleanup can find it.
MARKER = 'PERF-SYNTHETIC'
USERNAME_PREFIX = 'perf_user_'

BATCH_SIZE = 500


class Command(BaseCommand):
    help = (
        'Создаёт синтетический набор данных для локальных измерений '
        'производительности. Только для среды разработки/тестирования. '
        'По умолчанию dry-run; запись требует --execute.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--users', type=int, default=50)
        parser.add_argument('--acts', type=int, default=5000)
        parser.add_argument('--tasks', type=int, default=10000)
        parser.add_argument('--comments', type=int, default=20000)
        parser.add_argument('--history', type=int, default=20000)
        parser.add_argument('--notifications-per-user', type=int, default=20)
        parser.add_argument('--seed', type=int, default=20260805, help='Для воспроизводимости.')
        parser.add_argument(
            '--execute',
            action='store_true',
            help='Действительно записать данные. Без него команда только показывает план.',
        )
        parser.add_argument(
            '--i-know-this-is-not-development',
            action='store_true',
            help='Требуется при DEBUG=False.',
        )

    def handle(self, *args, **options):
        counts = {
            'users': options['users'],
            'acts': options['acts'],
            'tasks': options['tasks'],
            'comments': options['comments'],
            'history': options['history'],
            'notifications': options['users'] * options['notifications_per_user'],
        }
        for name, value in counts.items():
            if value < 0:
                raise CommandError(f'Количество "{name}" не может быть отрицательным.')

        if not settings.DEBUG and not options['i_know_this_is_not_development']:
            raise CommandError(
                'DEBUG=False: это похоже на рабочую среду. Синтетические данные '
                'туда не добавляются. Если это подготовленный тестовый стенд, '
                'повторите с --i-know-this-is-not-development.'
            )

        self.stdout.write('План создания синтетических данных:')
        for name, value in counts.items():
            self.stdout.write(f'  {name:<16} {value}')
        self.stdout.write(f'  маркер           {MARKER}')

        if not options['execute']:
            self.stdout.write('')
            self.stdout.write('DRY-RUN: ничего не записано. Повторите с --execute.')
            return None

        random.seed(options['seed'])
        with transaction.atomic():
            created = self._create(counts)

        self.stdout.write('')
        self.stdout.write('Создано:')
        for name, value in created.items():
            self.stdout.write(f'  {name:<16} {value}')
        return None

    # -- creation ----------------------------------------------------------

    def _create(self, counts):
        from accounts.models import Department, UserProfile
        from acts.models import Act, ActComment, ActHistoryEvent
        from notifications.models import Notification
        from references.models import ActStatus, DefectType, Operation, TaskStatus

        department, _ = Department.objects.get_or_create(
            code='PERF_DEP', defaults={'name': f'{MARKER} подразделение'}
        )
        operation, _ = Operation.objects.get_or_create(
            code='PERF_OP', defaults={'name': f'{MARKER} операция'}
        )
        defect_type, _ = DefectType.objects.get_or_create(
            code='PERF_DEFECT', defaults={'name': f'{MARKER} дефект'}
        )
        statuses = list(ActStatus.objects.all())
        if not statuses:
            raise CommandError('Нет ActStatus: выполните migrate и seed_references.')
        task_status = TaskStatus.objects.filter(code='IN_PROGRESS').first()
        if task_status is None:
            raise CommandError('Нет TaskStatus IN_PROGRESS: выполните seed_references.')

        users = self._create_users(counts['users'], department)
        created = {'users': len(users)}

        now = timezone.now()
        acts = [
            Act(
                created_by=random.choice(users),
                party_number=f'P-{index:06d}',
                nomenclature=f'{MARKER} изделие {index}',
                operation=operation,
                defect_type=defect_type,
                status=random.choice(statuses),
                description=f'{MARKER} описание {index}',
            )
            for index in range(counts['acts'])
        ]
        # Act numbers are entered by hand in the application, so the synthetic
        # dataset assigns its own clearly marked ones before the bulk insert.
        for index, act in enumerate(acts):
            act.number = f'{MARKER}-{now.year}-{index:06d}'
        Act.objects.bulk_create(acts, batch_size=BATCH_SIZE)
        act_ids = list(
            Act.objects.filter(number__startswith=MARKER).values_list('pk', flat=True)
        )
        created['acts'] = len(acts)

        if act_ids:
            ActComment.objects.bulk_create(
                [
                    ActComment(
                        act_id=random.choice(act_ids),
                        author=random.choice(users),
                        text=f'{MARKER} комментарий {index}',
                    )
                    for index in range(counts['comments'])
                ],
                batch_size=BATCH_SIZE,
            )
            created['comments'] = counts['comments']

            ActHistoryEvent.objects.bulk_create(
                [
                    ActHistoryEvent(
                        act_id=random.choice(act_ids),
                        user=random.choice(users),
                        event_type=ActHistoryEvent.EventType.COMMENT_ADDED,
                        message=f'{MARKER} событие {index}',
                    )
                    for index in range(counts['history'])
                ],
                batch_size=BATCH_SIZE,
            )
            created['history'] = counts['history']

            created['tasks'] = self._create_tasks(
                counts['tasks'], act_ids, users, department, task_status
            )

        Notification.objects.bulk_create(
            [
                Notification(
                    recipient=user,
                    actor=None,
                    event_type=Notification.EventType.COMMENT_ADDED,
                    title=f'{MARKER} уведомление',
                    message=f'{MARKER} текст {index}',
                    related_act_id=random.choice(act_ids) if act_ids else None,
                    deduplication_key=f'{MARKER}-{user.pk}-{index}',
                    created_at=now - timedelta(minutes=index),
                )
                for user in users
                for index in range(counts['notifications'] // max(1, len(users)))
            ],
            batch_size=BATCH_SIZE,
        )
        created['notifications'] = Notification.objects.filter(
            deduplication_key__startswith=MARKER
        ).count()
        return created

    def _create_users(self, total, department):
        from accounts.models import UserProfile

        model = get_user_model()
        existing = list(model.objects.filter(username__startswith=USERNAME_PREFIX))
        needed = max(0, total - len(existing))
        roles = [
            UserProfile.Role.OTK,
            UserProfile.Role.KO,
            UserProfile.Role.TO,
            UserProfile.Role.MANAGER,
        ]
        for index in range(needed):
            user = model.objects.create(
                username=f'{USERNAME_PREFIX}{len(existing) + index:04d}',
                is_active=True,
            )
            # No usable password: these accounts exist to own rows, never to
            # log in, so no credential is created or stored anywhere.
            user.set_unusable_password()
            user.save(update_fields=['password'])
            profile = user.userprofile
            profile.role = roles[index % len(roles)]
            profile.department = department
            profile.is_active = True
            profile.save()
            existing.append(user)
        return existing[:total] if total else existing

    def _create_tasks(self, total, act_ids, users, department, task_status):
        from acts.models import ActCorrectiveAction, ActRootAnalysis
        from tasks.models import Task, TaskAssignee

        if not total:
            return 0
        due = timezone.localdate() + timedelta(days=7)
        roots = ActRootAnalysis.objects.bulk_create(
            [
                ActRootAnalysis(act_id=random.choice(act_ids), root_cause=f'{MARKER} причина {index}')
                for index in range(total)
            ],
            batch_size=BATCH_SIZE,
        )
        roots = list(
            ActRootAnalysis.objects.filter(root_cause__startswith=MARKER).order_by('pk')[:total]
        )
        actions = ActCorrectiveAction.objects.bulk_create(
            [
                ActCorrectiveAction(
                    root_analysis=root,
                    comment=f'{MARKER} мероприятие {index}',
                    department=department,
                    due_date=due,
                )
                for index, root in enumerate(roots)
            ],
            batch_size=BATCH_SIZE,
        )
        actions = list(
            ActCorrectiveAction.objects.filter(comment__startswith=MARKER).order_by('pk')[:total]
        )
        tasks = Task.objects.bulk_create(
            [
                Task(
                    source_action=action,
                    act_id=action.root_analysis.act_id,
                    root_analysis=action.root_analysis,
                    task_text=f'{MARKER} задача {index}',
                    department=department,
                    due_date=due,
                    created_by=random.choice(users),
                    status=task_status,
                )
                for index, action in enumerate(actions)
            ],
            batch_size=BATCH_SIZE,
        )
        tasks = list(Task.objects.filter(task_text__startswith=MARKER).order_by('pk')[:total])
        TaskAssignee.objects.bulk_create(
            [TaskAssignee(task=task, user=random.choice(users)) for task in tasks],
            batch_size=BATCH_SIZE,
            ignore_conflicts=True,
        )
        return len(tasks)
