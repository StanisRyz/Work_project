import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActCorrectiveActionAssignee, ActRootAnalysis
from protocols.models import (
    QUALITY_PROTOCOL_TYPE_CODE, Protocol, ProtocolAction, ProtocolApproval, ProtocolType,
)
from realtime.sync import build_sync_state
from references.models import ActStatus, DefectType, Operation, TaskStatus

from .models import Task, TaskAssignee, TaskAttachment
from .permissions import (
    can_complete_task, can_upload_task_attachment, get_visible_tasks_queryset,
)
from .services import TaskWorkflowError, complete_task, replace_task_assignees


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='task-attachments-'))
class TaskViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(code='TO', name='ТО')
        cls.other_department = Department.objects.create(code='KO', name='КО')
        cls.status_archived = ActStatus.objects.get(code='ARCHIVED')
        cls.task_status = TaskStatus.objects.get(code='IN_PROGRESS')
        cls.operation = Operation.objects.create(code='OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='DEF', name='Дефект')
        cls.employee = cls._user('employee', UserProfile.Role.TO, cls.department)
        cls.other_employee = cls._user('other', UserProfile.Role.TO, cls.department)
        cls.manager = cls._user('manager', UserProfile.Role.MANAGER, cls.department)
        cls.mas = cls._user('mas_employee', UserProfile.Role.MAS, cls.department)
        cls.creator = cls._user('otk', UserProfile.Role.OTK, cls.other_department)
        cls.act = Act.objects.create(
            created_by=cls.creator, nomenclature='Изделие', status=cls.status_archived,
        )

    @classmethod
    def _user(cls, username, role, department):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.department = department
        user.userprofile.save()
        return user

    def _task(self, responsible, due_date, extra_assignees=()):
        root = ActRootAnalysis.objects.create(act=self.act, root_cause=f'Причина {ActRootAnalysis.objects.count()}')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment=f'Мероприятие {root.pk}', department=self.department,
            due_date=due_date,
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=responsible)
        for user in extra_assignees:
            ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=user)
        task = Task.objects.create(
            source_action=action, act=self.act, root_analysis=root, task_text=action.comment,
            department=self.department, due_date=due_date,
            created_by=self.creator, status=self.task_status,
        )
        TaskAssignee.objects.create(task=task, user=responsible)
        for user in extra_assignees:
            TaskAssignee.objects.create(task=task, user=user)
        return task

    def test_employee_sees_only_own_tasks_and_overdue_first(self):
        future = self._task(self.employee, timezone.localdate() + timedelta(days=3))
        overdue = self._task(self.employee, timezone.localdate() - timedelta(days=1))
        hidden = self._task(self.other_employee, timezone.localdate())
        self.client.force_login(self.employee)

        response = self.client.get(reverse('tasks:list'))

        self.assertContains(response, str(future.pk))
        self.assertContains(response, str(overdue.pk))
        self.assertNotContains(response, reverse('tasks:detail', args=[hidden.pk]))
        self.assertEqual(list(response.context['tasks'])[0], overdue)
        self.assertContains(response, 'task-row--overdue')
        self.assertContains(response, 'По акту')
        self.assertContains(response, reverse('tasks:detail', args=[overdue.pk]))
        self.assertContains(response, reverse('acts:detail', args=[self.act.pk]))
        self.assertNotContains(response, future.task_text)
        self.assertNotContains(response, 'Исполнители</th>')
        # «Тип задачи» is the source type; «Статус» is the task's own workflow
        # status. They are separate columns and never the same value.
        self.assertContains(response, '№ задачи</th><th>Тип задачи</th><th>Источник</th><th>Статус</th><th>Срок <a class="task-sort-link"')

    def test_every_employee_can_read_other_tasks_but_cannot_complete_them(self):
        own_task = self._task(self.employee, timezone.localdate())
        other_task = self._task(self.other_employee, timezone.localdate())
        self.client.force_login(self.employee)
        response = self.client.get(reverse('tasks:detail', args=[other_task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('tasks:complete', args=[other_task.pk]))
        self.assertEqual(
            self.client.post(
                reverse('tasks:complete', args=[other_task.pk]),
                {'execution_comment': 'Не должно сохраниться.'},
            ).status_code,
            404,
        )

        self.client.force_login(self.manager)
        response = self.client.get(reverse('tasks:detail', args=[own_task.pk]))
        self.assertContains(response, self.act.number)

    def test_administrator_sees_every_task(self):
        first_task = self._task(self.employee, timezone.localdate())
        second_task = self._task(self.other_employee, timezone.localdate())
        administrator = User.objects.create_superuser(username='admin_user', password='demo12345')
        self.client.force_login(administrator)

        response = self.client.get(reverse('tasks:list'), {'tab': 'all'})

        self.assertContains(response, reverse('tasks:detail', args=[first_task.pk]))
        self.assertContains(response, reverse('tasks:detail', args=[second_task.pk]))

    def test_each_authenticated_user_can_read_shared_task(self):
        task = self._task(self.employee, timezone.localdate(), [self.other_employee])
        self.client.force_login(self.other_employee)
        self.assertEqual(self.client.get(reverse('tasks:detail', args=[task.pk])).status_code, 200)
        unrelated = self._user('unrelated', UserProfile.Role.TO, self.department)
        self.client.force_login(unrelated)
        self.assertEqual(self.client.get(reverse('tasks:detail', args=[task.pk])).status_code, 200)

    def test_assignee_completes_shared_task_once(self):
        task = self._task(self.employee, timezone.localdate(), [self.other_employee])
        complete_task(task, self.other_employee, 'Мероприятие выполнено.')
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.completed_by, self.other_employee)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.execution_comment, 'Мероприятие выполнено.')
        with self.assertRaises(TaskWorkflowError):
            complete_task(task, self.employee, 'Повторное завершение.')

    def test_mas_reads_and_completes_assigned_work_but_not_protocol_approval_tasks(self):
        ordinary = self._task(self.mas, timezone.localdate())
        someone_elses = self._task(self.employee, timezone.localdate())
        self.assertIn(ordinary, get_visible_tasks_queryset(self.mas))
        self.assertNotIn(someone_elses, get_visible_tasks_queryset(self.mas))
        self.client.force_login(self.mas)
        self.assertEqual(
            self.client.get(reverse('tasks:detail', args=[ordinary.pk])).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse('tasks:detail', args=[someone_elses.pk])).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse('tasks:complete', args=[someone_elses.pk]),
                {'execution_comment': 'Чужая задача.'},
            ).status_code,
            404,
        )
        completed = self.client.post(
            reverse('tasks:complete', args=[ordinary.pk]),
            {'execution_comment': 'Работа выполнена мастером.'},
        )
        self.assertRedirects(
            completed,
            f"{reverse('tasks:list')}?tab=archive&number={ordinary.pk}",
        )
        ordinary.refresh_from_db()
        self.assertEqual(ordinary.status.code, 'COMPLETED')
        self.assertEqual(ordinary.completed_by, self.mas)

        protocol = Protocol.objects.create(
            protocol_type=ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE),
            number=100,
            author=self.creator,
        )
        approval_task = Task.objects.create(
            source_type=Task.SourceType.PROTOCOL_APPROVAL,
            protocol=protocol,
            task_text='Согласовать протокол',
            department=self.department,
            due_date=timezone.localdate(),
            created_by=self.creator,
            status=self.task_status,
        )
        TaskAssignee.objects.create(task=approval_task, user=self.mas)

        self.assertFalse(can_complete_task(approval_task, self.mas))
        refused = self.client.post(
            reverse('tasks:complete', args=[approval_task.pk]),
            {'execution_comment': 'Попытка обычного завершения.'},
        )
        self.assertEqual(refused.status_code, 400)
        approval_task.refresh_from_db()
        self.assertEqual(approval_task.status.code, 'IN_PROGRESS')

    def test_completing_a_task_bumps_updated_at_and_moves_the_tasks_revision(self):
        # `updated_at` is `auto_now=True`, but Django only bumps it when the
        # field is explicitly listed in `save(update_fields=...)` — this is the
        # A regression the sync-revision token alone would not catch,
        # since `completed_at` (already in `update_fields`) moves the token too.
        task = self._task(self.employee, timezone.localdate())
        before_updated_at = task.updated_at
        before_revision = build_sync_state(self.employee)['revisions']['tasks']

        complete_task(task, self.employee, 'Мероприятие выполнено.')
        task.refresh_from_db()

        self.assertGreater(task.updated_at, before_updated_at)
        after_revision = build_sync_state(self.employee)['revisions']['tasks']
        self.assertNotEqual(before_revision, after_revision)

    def test_repeated_completion_does_not_move_updated_at(self):
        task = self._task(self.employee, timezone.localdate())
        complete_task(task, self.employee, 'Первое завершение.')
        task.refresh_from_db()
        updated_at_after_first = task.updated_at

        with self.assertRaises(TaskWorkflowError):
            complete_task(task, self.employee, 'Повторное завершение.')

        task.refresh_from_db()
        self.assertEqual(task.updated_at, updated_at_after_first)
        self.assertEqual(task.execution_comment, 'Первое завершение.')

    def test_replacing_assignees_is_atomic_and_bumps_updated_at(self):
        # Assignments live in a child table, so writing them leaves the task
        # row — and the sync revision derived from it — untouched unless the
        # service saves the parent explicitly. That is what this guards.
        task = self._task(self.employee, timezone.localdate())
        before = task.updated_at

        replace_task_assignees(task, [self.other_employee], actor=self.manager)

        task.refresh_from_db()
        self.assertGreater(task.updated_at, before)
        self.assertEqual(
            list(TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True)),
            [self.other_employee.pk],
        )

    def test_replacing_assignees_adds_and_removes_in_one_operation(self):
        task = self._task(self.employee, timezone.localdate())

        replace_task_assignees(task, [self.employee, self.other_employee])

        self.assertEqual(
            sorted(TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True)),
            sorted([self.employee.pk, self.other_employee.pk]),
        )

    def test_replacing_with_the_same_assignees_changes_nothing(self):
        task = self._task(self.employee, timezone.localdate())
        before = task.updated_at

        replace_task_assignees(task, [self.employee])

        task.refresh_from_db()
        self.assertEqual(task.updated_at, before, 'a no-op must not move the revision')

    def test_a_task_may_never_be_left_without_an_assignee(self):
        task = self._task(self.employee, timezone.localdate())

        with self.assertRaises(TaskWorkflowError):
            replace_task_assignees(task, [])

        self.assertEqual(TaskAssignee.objects.filter(task=task).count(), 1)

    def test_tabs_respect_permissions_and_archive(self):
        own = self._task(self.employee, timezone.localdate())
        other = self._task(self.other_employee, timezone.localdate())
        completed = self._task(self.employee, timezone.localdate() - timedelta(days=3))
        other_completed = self._task(self.other_employee, timezone.localdate() - timedelta(days=2))
        complete_task(completed, self.employee, 'Выполнено.')
        complete_task(other_completed, self.other_employee, 'Выполнено другим сотрудником.')

        self.client.force_login(self.employee)
        my_response = self.client.get(reverse('tasks:list'))
        self.assertContains(my_response, reverse('tasks:detail', args=[own.pk]))
        self.assertNotContains(my_response, reverse('tasks:detail', args=[other.pk]))
        self.assertNotContains(my_response, reverse('tasks:detail', args=[completed.pk]))
        employee_all_response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        self.assertContains(employee_all_response, reverse('tasks:detail', args=[own.pk]))
        self.assertContains(employee_all_response, reverse('tasks:detail', args=[other.pk]))
        archive_response = self.client.get(reverse('tasks:list'), {'tab': 'archive'})
        self.assertContains(archive_response, reverse('tasks:detail', args=[completed.pk]))
        self.assertContains(archive_response, reverse('tasks:detail', args=[other_completed.pk]))
        self.assertNotContains(archive_response, 'task-row--overdue')

        self.client.force_login(self.manager)
        all_response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        self.assertContains(all_response, reverse('tasks:detail', args=[own.pk]))
        self.assertContains(all_response, reverse('tasks:detail', args=[other.pk]))
        self.assertNotContains(all_response, reverse('tasks:detail', args=[completed.pk]))

    def test_registry_filters_combine_and_reset_preserves_tab(self):
        matching = self._task(self.employee, timezone.localdate() - timedelta(days=1))
        hidden = self._task(self.employee, timezone.localdate() + timedelta(days=4))
        self.client.force_login(self.employee)
        response = self.client.get(reverse('tasks:list'), {
            'tab': 'my', 'number': matching.pk, 'source': self.act.number,
            'status': 'act', 'due': 'overdue', 'sort': 'nearest',
        })
        self.assertContains(response, reverse('tasks:detail', args=[matching.pk]))
        self.assertNotContains(response, reverse('tasks:detail', args=[hidden.pk]))
        self.assertContains(response, '?tab=my')
        empty = self.client.get(reverse('tasks:list'), {'number': 'not-a-number'})
        self.assertContains(empty, 'Задачи не найдены')

    def test_due_date_sorting_and_links(self):
        nearest = self._task(self.employee, timezone.localdate() + timedelta(days=1))
        farthest = self._task(self.employee, timezone.localdate() + timedelta(days=5))
        overdue = self._task(self.employee, timezone.localdate() - timedelta(days=1))
        self.client.force_login(self.employee)

        default_response = self.client.get(reverse('tasks:list'))
        self.assertEqual(list(default_response.context['tasks'])[0], overdue)
        nearest_response = self.client.get(reverse('tasks:list'), {'sort': 'nearest'})
        self.assertEqual(list(nearest_response.context['tasks'])[0], overdue)
        farthest_response = self.client.get(reverse('tasks:list'), {'sort': 'farthest'})
        self.assertEqual(list(farthest_response.context['tasks'])[0], farthest)
        self.assertContains(farthest_response, reverse('tasks:detail', args=[nearest.pk]))
        self.assertContains(farthest_response, reverse('acts:detail', args=[self.act.pk]))

    def test_detail_shows_task_card_and_preserves_return_query(self):
        task = self._task(self.employee, timezone.localdate() - timedelta(days=1), [self.other_employee])
        self.client.force_login(self.employee)
        response = self.client.get(reverse('tasks:detail', args=[task.pk]), {'tab': 'all', 'source': self.act.number})
        self.assertEqual(response.context['header_title'], f'Задача {task.pk}')
        self.assertNotContains(response, '<section class="task-detail-card">\n    <h1>')
        self.assertContains(response, 'Статус')
        self.assertContains(response, str(task.status))
        self.assertContains(response, 'Тип задачи')
        self.assertContains(response, 'По акту')
        self.assertContains(response, 'Корневая причина')
        self.assertContains(response, 'Исполнители')
        self.assertContains(response, self.other_employee.username)
        self.assertContains(response, 'tab=all&amp;source=')

    def test_completion_requires_comment_and_redirects_to_filtered_archive(self):
        task = self._task(self.employee, timezone.localdate(), [self.other_employee])
        self.client.force_login(self.other_employee)
        url = reverse('tasks:complete', args=[task.pk])
        invalid = self.client.post(url, {'execution_comment': '   ', 'list_query': 'tab=all'})
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, 'Укажите результат выполнения задачи.', status_code=400)
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'IN_PROGRESS')

        response = self.client.post(url, {'execution_comment': 'Работа выполнена.', 'list_query': 'tab=all'})
        self.assertRedirects(response, f'{reverse("tasks:list")}?tab=archive&number={task.pk}')
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.completed_by, self.other_employee)
        self.assertEqual(task.execution_comment, 'Работа выполнена.')
        archive = self.client.get(reverse('tasks:list'), {'tab': 'archive', 'number': task.pk})
        self.assertContains(archive, reverse('tasks:detail', args=[task.pk]))

    def test_a_required_attachment_blocks_completion_until_one_file_exists(self):
        """`Task.requires_attachment`: the backend is the only authority.

        The page announces the requirement and the button stays live, so this
        posts the completion exactly as a user would and checks what the server
        answers. The execution comment stays required independently of it, and
        a task without the flag is untouched.
        """
        task = self._task(self.employee, timezone.localdate())
        task.requires_attachment = True
        task.save(update_fields=['requires_attachment'])
        complete_url = reverse('tasks:complete', args=[task.pk])
        self.client.force_login(self.employee)

        # The notice is on the page; the file input is never HTML-required,
        # because uploading and completing are separate requests.
        detail = self.client.get(reverse('tasks:detail', args=[task.pk]))
        self.assertContains(detail, 'Для выполнения этой задачи необходимо добавить вложение.')

        refused = self.client.post(complete_url, {'execution_comment': 'Сделано.'})

        self.assertEqual(refused.status_code, 400)
        self.assertContains(
            refused, 'Для выполнения этой задачи необходимо добавить вложение.', status_code=400
        )
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'IN_PROGRESS')

        # One attachment is the whole rule — no extension, count, description
        # or particular uploader is asked for. A colleague eligible to upload
        # to this shared task satisfies it for everyone on it.
        self.client.force_login(self.other_employee)
        TaskAssignee.objects.create(task=task, user=self.other_employee)
        self.client.post(
            reverse('tasks:add_attachment', args=[task.pk]),
            {'file': SimpleUploadedFile('акт.txt', b'result')},
        )
        self.assertEqual(task.attachments.count(), 1)

        self.client.force_login(self.employee)
        allowed = self.client.post(complete_url, {'execution_comment': 'Сделано.'})

        self.assertRedirects(
            allowed, f'{reverse("tasks:list")}?tab=archive&number={task.pk}'
        )
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')

        # And a task that never carried the flag still finishes with nothing
        # attached, exactly as before.
        plain = self._task(self.employee, timezone.localdate())
        self.assertFalse(plain.requires_attachment)
        self.client.post(
            reverse('tasks:complete', args=[plain.pk]), {'execution_comment': 'Сделано.'}
        )
        plain.refresh_from_db()
        self.assertEqual(plain.status.code, 'COMPLETED')
        self.assertEqual(plain.attachments.count(), 0)

    def test_optional_attachment_is_uploadable_downloadable_and_never_required(self):
        """Files on a task are optional, protected, and not part of finishing it."""
        task = self._task(self.employee, timezone.localdate())
        upload_url = reverse('tasks:add_attachment', args=[task.pk])

        # A user with no relation to the task may read it, and reading has
        # never granted a write: the upload is refused rather than stored.
        self.client.force_login(self.other_employee)
        refused = self.client.post(
            upload_url, {'file': SimpleUploadedFile('чужой.txt', b'nope')}
        )
        self.assertEqual(refused.status_code, 404)
        self.assertEqual(TaskAttachment.objects.count(), 0)

        # The assignee of an active task may attach one.
        self.client.force_login(self.employee)
        response = self.client.post(
            upload_url, {'file': SimpleUploadedFile('отчёт.txt', b'result')}
        )
        self.assertRedirects(response, reverse('tasks:detail', args=[task.pk]))
        attachment = TaskAttachment.objects.get()
        self.assertEqual(attachment.task_id, task.pk)
        self.assertEqual(attachment.uploaded_by, self.employee)
        self.assertEqual(attachment.original_name, 'отчёт.txt')
        # The stored path never carries the name the browser sent.
        self.assertNotIn('отчёт', attachment.file.name)

        # Anyone who may read the task may download its protected file.
        self.client.force_login(self.other_employee)
        download = self.client.get(
            reverse('tasks:download_attachment', args=[task.pk, attachment.pk])
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b''.join(download.streaming_content), b'result')

        # A second task is completed with the execution comment and no file at
        # all, and completion never removes what is already attached.
        bare = self._task(self.employee, timezone.localdate())
        self.client.force_login(self.employee)
        self.client.post(
            reverse('tasks:complete', args=[bare.pk]),
            {'execution_comment': 'Выполнено без вложений.'},
        )
        bare.refresh_from_db()
        self.assertEqual(bare.status.code, 'COMPLETED')
        self.assertFalse(bare.attachments.exists())

        self.client.post(
            reverse('tasks:complete', args=[task.pk]),
            {'execution_comment': 'Выполнено.'},
        )
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.attachments.count(), 1)
        # A finished task no longer accepts uploads, but still hands out the
        # file it has.
        self.assertFalse(can_upload_task_attachment(task, self.employee))
        self.assertEqual(
            self.client.get(
                reverse('tasks:download_attachment', args=[task.pk, attachment.pk])
            ).status_code,
            200,
        )

    def test_unassigned_manager_cannot_complete_task(self):
        task = self._task(self.employee, timezone.localdate())
        self.client.force_login(self.manager)
        response = self.client.post(reverse('tasks:complete', args=[task.pk]), {'execution_comment': 'Не должно сохраниться.'})
        self.assertEqual(response.status_code, 400)
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        self.assertEqual(task.execution_comment, '')

    def test_unassigned_administrator_can_complete_task(self):
        task = self._task(self.employee, timezone.localdate())
        administrator = User.objects.create_superuser(username='admin_complete', password='demo12345')
        self.client.force_login(administrator)
        response = self.client.post(
            reverse('tasks:complete', args=[task.pk]), {'execution_comment': 'Завершено администратором.'}
        )
        self.assertRedirects(response, f'{reverse("tasks:list")}?tab=archive&number={task.pk}')
        task.refresh_from_db()
        self.assertEqual(task.completed_by, administrator)


class TaskSourceTests(TestCase):
    """The source contract: which relation shapes may exist at all.

    Structural only. No production code creates a protocol-sourced task yet;
    these tests build the rows directly to prove the schema is ready and that
    the wrong shapes cannot be stored.
    """

    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(code='TO', name='ТО')
        cls.user = User.objects.create_user(username='source_user', password='demo12345')
        cls.task_status = TaskStatus.objects.get(code='IN_PROGRESS')
        cls.act = Act.objects.create(
            created_by=cls.user, nomenclature='Изделие',
            status=ActStatus.objects.get(code='ARCHIVED'),
        )
        cls.root_analysis = ActRootAnalysis.objects.create(act=cls.act, root_cause='Причина')
        cls.corrective_action = ActCorrectiveAction.objects.create(
            root_analysis=cls.root_analysis, comment='Мероприятие',
            department=cls.department, due_date=timezone.localdate(),
        )
        protocol_type = ProtocolType.objects.get_or_create(
            code=QUALITY_PROTOCOL_TYPE_CODE, defaults={'name': 'Протокол по качеству'},
        )[0]
        cls.protocol = Protocol.objects.create(
            protocol_type=protocol_type, number=1, author=cls.user,
        )
        cls.protocol_action = ProtocolAction.objects.create(
            protocol=cls.protocol, task_text='Решение протокола',
            department=cls.department, due_date=timezone.localdate(),
        )

    def _task_kwargs(self, **overrides):
        return {
            'task_text': 'Задача', 'department': self.department,
            'due_date': timezone.localdate(), 'created_by': self.user,
            'status': self.task_status, **overrides,
        }

    def test_database_rejects_mixed_and_incomplete_task_sources(self):
        invalid_shapes = {
            'act source carrying a protocol': self._task_kwargs(
                source_type=Task.SourceType.ACT, act=self.act,
                root_analysis=self.root_analysis, source_action=self.corrective_action,
                protocol=self.protocol,
            ),
            'act source missing its corrective action': self._task_kwargs(
                source_type=Task.SourceType.ACT, act=self.act, root_analysis=self.root_analysis,
            ),
            'protocol action source without the action': self._task_kwargs(
                source_type=Task.SourceType.PROTOCOL_ACTION, protocol=self.protocol,
            ),
            'protocol approval source still holding act relations': self._task_kwargs(
                source_type=Task.SourceType.PROTOCOL_APPROVAL, protocol=self.protocol,
                act=self.act, root_analysis=self.root_analysis,
                source_action=self.corrective_action,
            ),
        }
        for label, kwargs in invalid_shapes.items():
            with self.subTest(shape=label):
                # Each attempt gets its own savepoint: an `IntegrityError`
                # breaks the surrounding transaction otherwise.
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Task.objects.create(**kwargs)
        self.assertEqual(Task.objects.count(), 0)

    def test_protocol_shaped_tasks_are_storable_and_approvals_never_complete(self):
        action_task = Task.objects.create(**self._task_kwargs(
            source_type=Task.SourceType.PROTOCOL_ACTION,
            protocol=self.protocol, protocol_action=self.protocol_action,
        ))
        action_task.full_clean(exclude=['task_text'])
        self.assertIsNone(action_task.act_id)

        # The cross-table rule no check constraint can express.
        other_protocol = Protocol.objects.create(
            protocol_type=self.protocol.protocol_type, number=2, author=self.user,
        )
        mismatched = Task(**self._task_kwargs(
            source_type=Task.SourceType.PROTOCOL_ACTION,
            protocol=other_protocol, protocol_action=self.protocol_action,
        ))
        with self.assertRaises(ValidationError):
            mismatched.clean()

        # Agreeing to a protocol is its own decision — the ordinary completion
        # workflow must refuse it even for a user who is otherwise entitled.
        approval_task = Task.objects.create(**self._task_kwargs(
            source_type=Task.SourceType.PROTOCOL_APPROVAL, protocol=self.protocol,
        ))
        TaskAssignee.objects.create(task=approval_task, user=self.user)
        self.assertFalse(can_complete_task(approval_task, self.user))
        with self.assertRaises(TaskWorkflowError):
            complete_task(approval_task, self.user, 'Согласовано.')
        approval_task.refresh_from_db()
        self.assertEqual(approval_task.status.code, 'IN_PROGRESS')


class TaskRegistrySourceTests(TestCase):
    """The shared registry with all three sources side by side.

    One scenario rather than a filter matrix: what the source-aware stage can
    get wrong is the registry conflating «тип задачи» with «статус», a protocol
    task pointing at the wrong page, «Источник» failing to find a protocol, and
    a `PROTOCOL_ACTION` task quietly losing the ordinary completion flow it is
    supposed to keep.
    """

    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(code='TO', name='ТО')
        cls.employee = User.objects.create_user(username='reg_employee', password='demo12345')
        cls.employee.userprofile.department = cls.department
        cls.employee.userprofile.role = UserProfile.Role.TO
        cls.employee.userprofile.save(update_fields=['department', 'role'])
        cls.in_progress = TaskStatus.objects.get(code='IN_PROGRESS')

        cls.act = Act.objects.create(
            created_by=cls.employee, number='АОК-2026-00034', nomenclature='Изделие',
            status=ActStatus.objects.get(code='ARCHIVED'),
        )
        root_analysis = ActRootAnalysis.objects.create(act=cls.act, root_cause='Причина')
        corrective_action = ActCorrectiveAction.objects.create(
            root_analysis=root_analysis, comment='Мероприятие',
            department=cls.department, due_date=timezone.localdate(),
        )
        cls.protocol = Protocol.objects.create(
            protocol_type=ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE),
            number=7, author=cls.employee,
        )
        protocol_action = ProtocolAction.objects.create(
            protocol=cls.protocol, task_text='Проверить оснастку',
            department=cls.department, due_date=timezone.localdate(),
        )

        cls.act_task = cls._task(
            source_type=Task.SourceType.ACT, act=cls.act,
            root_analysis=root_analysis, source_action=corrective_action,
        )
        cls.approval_task = cls._task(
            source_type=Task.SourceType.PROTOCOL_APPROVAL, protocol=cls.protocol,
        )
        cls.action_task = cls._task(
            source_type=Task.SourceType.PROTOCOL_ACTION, protocol=cls.protocol,
            protocol_action=protocol_action,
        )

    @classmethod
    def _task(cls, **overrides):
        task = Task.objects.create(
            task_text='Задача', department=cls.department, due_date=timezone.localdate(),
            created_by=cls.employee, status=cls.in_progress, **overrides,
        )
        TaskAssignee.objects.create(task=task, user=cls.employee)
        return task

    def setUp(self):
        self.client.force_login(self.employee)

    def _rows(self, **params):
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', **params})
        return response, {row['task'].pk: row for row in response.context['rows']}

    def test_registry_is_source_aware_and_protocol_action_stays_completable(self):
        response, rows = self._rows()
        self.assertEqual(
            set(rows), {self.act_task.pk, self.approval_task.pk, self.action_task.pk}
        )

        # «Тип задачи» is the origin; «Статус» is the workflow state. The two
        # are separate values, and the act keeps pointing at the act.
        self.assertEqual(rows[self.act_task.pk]['type_label'], 'По акту')
        self.assertEqual(rows[self.act_task.pk]['state']['label'], 'В работе')
        self.assertEqual(
            rows[self.act_task.pk]['source'],
            {'label': 'АОК-2026-00034', 'url': reverse('acts:detail', args=[self.act.pk])},
        )
        # Both protocol sources name the protocol and link to it, never to an act.
        protocol_url = reverse('protocols:detail', args=[self.protocol.pk])
        for task in (self.approval_task, self.action_task):
            self.assertEqual(rows[task.pk]['source']['label'], 'Качество №7')
            self.assertEqual(rows[task.pk]['source']['url'], protocol_url)
        self.assertEqual(rows[self.action_task.pk]['type_label'], 'По протоколу')
        self.assertEqual(rows[self.approval_task.pk]['type_label'], 'Согласование протокола')

        # «Тип задачи» filters on the source type, one value at a time.
        for value, expected in (
            ('ACT', {self.act_task.pk}),
            ('PROTOCOL_APPROVAL', {self.approval_task.pk}),
            ('PROTOCOL_ACTION', {self.action_task.pk}),
        ):
            self.assertEqual(set(self._rows(source_type=value)[1]), expected)
        # An unknown value is ignored rather than filtering everything away.
        self.assertEqual(len(self._rows(source_type='hack')[1]), 3)

        # «Источник» finds acts as before, and now protocols by name and number.
        self.assertEqual(set(self._rows(source='АОК-2026')[1]), {self.act_task.pk})
        protocol_tasks = {self.approval_task.pk, self.action_task.pk}
        for term in ('ачеств', 'Качество №7', '№7', '7'):
            self.assertEqual(set(self._rows(source=term)[1]), protocol_tasks, term)
        self.assertEqual(self._rows(source='нет такого')[1], {})

        # A `PROTOCOL_ACTION` task is an ordinary task: its page opens, shows
        # the protocol as the source, hides the act-only root analysis, and
        # completes through the normal flow.
        detail = self.client.get(reverse('tasks:detail', args=[self.action_task.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'По протоколу')
        self.assertContains(detail, protocol_url)
        self.assertNotContains(detail, 'Корневая причина')
        self.assertTrue(detail.context['can_complete'])
        self.client.post(
            reverse('tasks:complete', args=[self.action_task.pk]),
            {'execution_comment': 'Оснастка проверена.'},
        )
        self.action_task.refresh_from_db()
        self.assertEqual(self.action_task.status.code, 'COMPLETED')

    def test_archived_approval_task_shows_the_real_decision_not_the_queue_state(self):
        # The queue row is closed either way; only `ProtocolApproval` says
        # whether the person approved or their round was cancelled.
        cancelled = ProtocolApproval.objects.create(
            protocol=self.protocol, revision=1, user=self.employee,
            status=ProtocolApproval.Status.CANCELLED, task=self.approval_task,
            display_name='Сотрудник',
        )
        self.approval_task.status = TaskStatus.objects.get(code='COMPLETED')
        self.approval_task.completed_at = timezone.now()
        self.approval_task.save(update_fields=['status', 'completed_at'])

        rows = self._rows(tab='archive')[1]
        state = rows[self.approval_task.pk]['state']
        self.assertEqual(state['label'], 'Отменено')
        self.assertEqual(state['variant'], 'cancelled')
        self.assertNotEqual(state['label'], str(self.approval_task.status))

        cancelled.status = ProtocolApproval.Status.APPROVED
        cancelled.save(update_fields=['status'])
        self.assertEqual(
            self._rows(tab='archive')[1][self.approval_task.pk]['state']['label'], 'Согласовано'
        )
