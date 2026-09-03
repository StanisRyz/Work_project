"""The rules the СМК module must not lose.

Deliberately small: everything else here is already covered where it lives —
the completion guard by `tasks.tests`, the assignee/department pairing by the
protocol editor's own tests. What is new, and therefore tested, is who may
create and archive an СМК record, that a measure really becomes a `Task`, that the task
reaches the common registry with its СМК source named, that its исполнители
are notified through the common notification and email queue, that «Требуется
вложение» travels onto the task and is enforced there, and that nothing is
written before the creation is confirmed.
"""

import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from notifications.email_delivery import process_delivery
from notifications.models import Notification, NotificationDelivery
from notifications.services import get_notification_header_state
from tasks.models import Task
from tasks.services import TaskWorkflowError, add_task_attachment, complete_task

from .models import SmkHistoryEvent, SmkSource
from .permissions import can_archive_smk_source, can_create_smk_task
from .services import SmkWorkflowError, archive_smk_source, create_smk_source


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='smk-attachments-'))
class SmkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The migration already seeded «Отдел СМК»; reuse it rather than
        # creating a second row with the same unique code.
        cls.department = Department.objects.get(code='SMK')
        cls.smk = cls._user('smk_user', UserProfile.Role.SMK)
        cls.manager = cls._user('manager_user', UserProfile.Role.MANAGER)
        cls.admin = cls._user('admin_user', UserProfile.Role.ADMIN)
        cls.employee = cls._user('otk_user', UserProfile.Role.OTK)
        # Deliberately not today: «дата аудита» is the audit's own date, and a
        # test that used today's could not tell it from `created_at`.
        cls.audit_date = timezone.localdate() - timedelta(days=2)

    @classmethod
    def _user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.department = cls.department
        user.userprofile.save()
        return user

    def _actions(self, assignees=None, requires_attachment=False, non_conformity=None):
        return [
            {
                'text': 'Провести обучение персонала',
                'department': self.department,
                'due_date': timezone.localdate() + timedelta(days=7),
                'non_conformity': non_conformity,
                'requires_attachment': requires_attachment,
                'assignees': list(assignees or [self.employee]),
            }
        ]

    def _post_data(self, **overrides):
        """The flat POST the creation page really sends, minus the flag."""
        data = {
            'origin': SmkSource.Origin.INTERNAL_AUDIT,
            'audit_date': self.audit_date.isoformat(),
            'nonconformities-TOTAL_FORMS': '1',
            'nonconformities-0-text': 'Не ведётся журнал поверки',
            'actions-TOTAL_FORMS': '1',
            'actions-0-text': 'Завести журнал поверки',
            'actions-0-due_date': (timezone.localdate() + timedelta(days=7)).isoformat(),
            'actions-0-assignees': [str(self.employee.pk)],
            'actions-0-assignee_departments': [str(self.department.pk)],
        }
        data.update(overrides)
        return data

    # ------------------------------------------------------------ permissions

    def test_only_smk_manager_and_admin_may_create(self):
        for user in (self.smk, self.manager, self.admin):
            with self.subTest(user=user.username):
                self.assertTrue(can_create_smk_task(user))
        self.assertFalse(can_create_smk_task(self.employee))

    def test_unauthorized_user_cannot_create_smk_task(self):
        """Refused by the service, not only by the view that calls it."""
        with self.assertRaises(SmkWorkflowError):
            create_smk_source(
                origin=SmkSource.Origin.INTERNAL_AUDIT,
                audit_date=self.audit_date,
                non_conformities=['Несоответствие'],
                actions=self._actions(),
                created_by=self.employee,
            )
        self.assertFalse(SmkSource.objects.exists())
        # And the form page is not even reachable for them.
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('smk:create')).status_code, 404)

    def test_smk_user_skips_the_task_type_choice(self):
        """One kind of task is not a choice; a manager still gets the menu."""
        self.client.force_login(self.smk)
        self.assertRedirects(self.client.get(reverse('tasks:create')), reverse('smk:create'))
        self.client.force_login(self.manager)
        response = self.client.get(reverse('tasks:create'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('smk:create'))

    # ----------------------------------------------------------- task creation

    def test_each_corrective_action_creates_one_linked_task(self):
        source = create_smk_source(
            origin=SmkSource.Origin.EXTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Первое', 'Второе'],
            actions=self._actions([self.employee, self.smk]),
            created_by=self.smk,
        )
        self.assertEqual(source.non_conformities.count(), 2)
        action = source.actions.get()
        task = Task.objects.get(smk_action=action)
        self.assertEqual(task.source_type, Task.SourceType.SMK)
        self.assertEqual(task.smk_source_id, source.pk)
        self.assertEqual(task.task_text, action.task_text)
        self.assertEqual(task.due_date, action.due_date)
        self.assertEqual(task.department_id, self.department.pk)
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        self.assertCountEqual(
            task.assignees.values_list('user_id', flat=True),
            [self.employee.pk, self.smk.pk],
        )

    # -------------------------------------------------------------- visibility

    def test_smk_task_appears_in_the_task_registry(self):
        source = create_smk_source(
            origin=SmkSource.Origin.INTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Несоответствие'],
            actions=self._actions(),
            created_by=self.smk,
        )
        task = Task.objects.get(smk_source=source)
        self.client.force_login(self.employee)
        response = self.client.get(reverse('tasks:list'), {'tab': 'my'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('tasks:detail', args=[task.pk]))
        # The registry names the source «СМК» and links to the record itself.
        self.assertContains(response, source.label)
        self.assertContains(response, reverse('smk:detail', args=[source.pk]))

    # ------------------------------------------------- required attachment

    def test_required_attachment_travels_onto_the_task(self):
        """The measure's checkbox is the task's own snapshot, per measure."""
        source = create_smk_source(
            origin=SmkSource.Origin.EXTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Несоответствие'],
            actions=[
                *self._actions(requires_attachment=True),
                {
                    'text': 'Обновить инструкцию',
                    'department': self.department,
                    'due_date': timezone.localdate() + timedelta(days=3),
                    'non_conformity': None,
                    'requires_attachment': False,
                    'assignees': [self.employee],
                },
            ],
            created_by=self.smk,
        )
        required, optional = source.actions.all()
        self.assertTrue(required.requires_attachment)
        self.assertTrue(Task.objects.get(smk_action=required).requires_attachment)
        self.assertFalse(optional.requires_attachment)
        self.assertFalse(Task.objects.get(smk_action=optional).requires_attachment)

    def test_task_requiring_an_attachment_is_not_completable_without_one(self):
        """The existing guard, reached through an СМК task — no new rule."""
        source = create_smk_source(
            origin=SmkSource.Origin.EXTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Несоответствие'],
            actions=self._actions(requires_attachment=True),
            created_by=self.smk,
        )
        task = Task.objects.get(smk_source=source)
        with self.assertRaises(TaskWorkflowError):
            complete_task(task, self.employee, 'Выполнено')
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'IN_PROGRESS')

        add_task_attachment(
            task, self.employee, SimpleUploadedFile('report.pdf', b'%PDF-1.4 ', 'application/pdf'),
        )
        complete_task(task, self.employee, 'Выполнено')
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')

    # ------------------------------------------------------ confirmation step

    def test_nothing_is_created_before_the_creation_is_confirmed(self):
        """A valid POST without the flag answers with the summary and writes nothing."""
        self.client.force_login(self.smk)
        response = self.client.post(reverse('smk:create'), self._post_data())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SmkSource.objects.exists())
        self.assertFalse(Task.objects.filter(source_type=Task.SourceType.SMK).exists())
        # The confirmation the page shows is built from the validated data.
        self.assertContains(response, 'Подтверждение создания задачи СМК')
        self.assertEqual(response.context['confirmation'], {
            'origin_label': 'Внутренний аудит',
            'audit_date': self.audit_date,
            'non_conformity_count': 1,
            'action_count': 1,
            'assignees': [self.employee.get_full_name() or self.employee.username],
        })

        response = self.client.post(reverse('smk:create'), self._post_data(confirmed='1'))
        source = SmkSource.objects.get()
        self.assertRedirects(response, reverse('smk:detail', args=[source.pk]))
        self.assertEqual(Task.objects.filter(smk_source=source).count(), 1)

    def test_the_confirmed_post_carries_the_attachment_requirement(self):
        """The checkbox posted by the form reaches the task, end to end."""
        self.client.force_login(self.smk)
        self.client.post(reverse('smk:create'), self._post_data(
            confirmed='1', **{'actions-0-requires_attachment': 'on'},
        ))
        self.assertTrue(Task.objects.get(source_type=Task.SourceType.SMK).requires_attachment)

    # -------------------------------------------------------------- audit date

    def test_the_record_stores_the_audit_date_apart_from_its_creation(self):
        """«Дата аудита» is the author's own answer, never `created_at`."""
        self.client.force_login(self.smk)
        self.client.post(reverse('smk:create'), self._post_data(confirmed='1'))
        source = SmkSource.objects.get()
        self.assertEqual(source.audit_date, self.audit_date)
        self.assertNotEqual(source.audit_date, timezone.localdate(source.created_at))

    # --------------------------------------------------------------- page tabs

    def test_the_record_page_renders_both_tabs(self):
        """«Акт аудита» by default, «Связанные мероприятия» on request."""
        source = create_smk_source(
            origin=SmkSource.Origin.EXTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Не ведётся журнал поверки'],
            actions=self._actions(non_conformity=0),
            created_by=self.smk,
        )
        task = Task.objects.get(smk_source=source)
        self.client.force_login(self.employee)

        response = self.client.get(reverse('smk:detail', args=[source.pk]))
        self.assertEqual(response.context['detail_tab'], 'act')
        self.assertContains(response, 'Акт аудита')
        self.assertContains(response, 'Выявленные несоответствия')
        self.assertContains(response, 'Корректирующие мероприятия')
        # The information card, and the audit date it now carries.
        self.assertContains(response, 'Дата аудита')
        self.assertContains(response, self.audit_date.strftime('%d.%m.%Y'))
        # The measure names the finding it answers, by its number on screen.
        self.assertContains(response, 'Связано с несоответствием №1')
        # No event timeline on this page.
        self.assertNotContains(response, 'История событий')

        response = self.client.get(reverse('smk:detail', args=[source.pk]), {'tab': 'activities'})
        self.assertEqual(response.context['detail_tab'], 'activities')
        self.assertContains(response, reverse('tasks:detail', args=[task.pk]))

        # «История» is the third tab, and it opens on the trail the creation
        # wrote: «создана» plus one line per задача a measure produced.
        response = self.client.get(reverse('smk:detail', args=[source.pk]), {'tab': 'history'})
        self.assertEqual(response.context['detail_tab'], 'history')
        self.assertContains(response, 'История записи СМК')
        self.assertContains(response, 'Запись СМК создана')
        self.assertContains(response, f'создана задача №{task.pk}')
        # An unknown tab falls back to the record rather than answering 404.
        self.assertEqual(
            self.client.get(
                reverse('smk:detail', args=[source.pk]), {'tab': 'nope'},
            ).context['detail_tab'],
            'act',
        )

    # ---------------------------------------------------------------- registry

    def _source(self):
        return create_smk_source(
            origin=SmkSource.Origin.INTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Не ведётся журнал поверки'],
            actions=self._actions(),
            created_by=self.smk,
        )

    def test_the_registry_lists_a_new_record_under_work(self):
        """The section exists, is reachable from «Качество», and opens on «Работа».

        A record is live the moment it is created and stays there — nothing but
        «Архивировать» moves it — so a freshly created one must be in the first
        tab and absent from the second.
        """
        source = self._source()
        self.client.force_login(self.employee)

        response = self.client.get(reverse('smk:list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['tab'], 'work')
        self.assertEqual([row['source'] for row in response.context['sources']], [source])
        # The row carries what the table promises: the record, its audit type,
        # the real tasks its measures produced and the derived state — «Создана»
        # while no task has moved yet.
        self.assertContains(response, source.label)
        self.assertContains(response, reverse('smk:detail', args=[source.pk]))
        self.assertEqual(response.context['sources'][0]['task_count'], 1)
        self.assertEqual(response.context['sources'][0]['state']['code'], 'created')
        self.assertContains(response, 'Создана')
        # The navigation entry itself — rendered on every page by the sidebar.
        self.assertContains(response, f'href="{reverse("smk:list")}"')

        archive = self.client.get(reverse('smk:list'), {'tab': 'archive'})
        self.assertEqual(archive.context['sources'], [])

    # --------------------------------------------------------------- archiving

    def test_only_permitted_roles_archive_and_the_record_moves_to_the_archive(self):
        """СМК/руководитель/администратор may archive; an ordinary user may not.

        And archiving only moves the record: its task, and the link between
        them, are exactly what they were before.
        """
        source = self._source()
        task = Task.objects.get(smk_source=source)

        # An ordinary assignee reads the record but is offered nothing.
        self.client.force_login(self.employee)
        self.assertFalse(
            self.client.get(reverse('smk:detail', args=[source.pk])).context['can_archive'],
        )
        with self.assertRaises(SmkWorkflowError):
            archive_smk_source(source, actor=self.employee)
        source.refresh_from_db()
        self.assertEqual(source.status, SmkSource.Status.ACTIVE)

        self.client.force_login(self.smk)
        response = self.client.post(
            reverse('smk:archive', args=[source.pk]), follow=True,
        )
        self.assertEqual(response.status_code, 200)
        source.refresh_from_db()
        self.assertEqual(source.status, SmkSource.Status.ARCHIVED)
        self.assertEqual(source.archived_by, self.smk)
        self.assertIsNotNone(source.archived_at)
        # Untouched by the transition.
        task.refresh_from_db()
        self.assertEqual(task.smk_source_id, source.pk)

        # It is now in «Архив», gone from «Работа», and still readable — the
        # button being the only thing that disappeared.
        archive = self.client.get(reverse('smk:list'), {'tab': 'archive'})
        self.assertEqual([row['source'] for row in archive.context['sources']], [source])
        self.assertEqual(archive.context['sources'][0]['state']['code'], 'archived')
        work = self.client.get(reverse('smk:list'))
        self.assertEqual(work.context['sources'], [])
        detail = self.client.get(reverse('smk:detail', args=[source.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.context['can_archive'])
        # «Архив» wins over whatever the tasks say, on both pages.
        self.assertEqual(detail.context['state']['label'], 'Архив')

    # --------------------------------------------------- related activities

    def test_the_activities_tab_references_the_task_in_five_columns(self):
        """The record points at the task; it never restates it.

        The same five columns the protocol table has — №, мероприятие,
        исполнители, срок, статус — and the статус is the task's own, so the
        page cannot drift from the work it describes. The long мероприятие text
        is clamped for display only: it is rendered in full, and the browser is
        what cuts it at three lines.
        """
        long_text = 'Провести обучение персонала. ' * 20
        actions = self._actions(assignees=[self.employee])
        actions[0]['text'] = long_text
        source = create_smk_source(
            origin=SmkSource.Origin.INTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Не ведётся журнал поверки'],
            actions=actions,
            created_by=self.smk,
        )
        task = Task.objects.get(smk_source=source)
        self.client.force_login(self.employee)

        response = self.client.get(
            reverse('smk:detail', args=[source.pk]), {'tab': 'activities'},
        )
        self.assertEqual(response.context['actions'][0]['task'], task)
        # The link to the task is how the record points at the work.
        self.assertContains(response, reverse('tasks:detail', args=[task.pk]))
        # Clamped in the browser, never in the database or the response.
        self.assertContains(response, 'text-clamp-3')
        self.assertContains(response, long_text.strip())
        # The columns the СМК table no longer carries.
        self.assertNotContains(response, 'Несоответствие')
        self.assertNotContains(response, 'Вложение')
        self.assertNotContains(response, 'Открыть задачу')

    # --------------------------------------------------------------- history

    def test_archiving_is_recorded_in_the_history(self):
        """The trail gains «в архив» — and only for a user allowed to do it."""
        source = create_smk_source(
            origin=SmkSource.Origin.EXTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Не ведётся журнал поверки'],
            actions=self._actions(),
            created_by=self.smk,
        )
        self.assertFalse(
            source.history_events.filter(
                event_type=SmkHistoryEvent.EventType.ARCHIVED,
            ).exists(),
        )

        # A refused attempt writes no event: `_record()` runs inside the same
        # transaction as the change it describes, and there was no change.
        with self.assertRaises(SmkWorkflowError):
            archive_smk_source(source, actor=self.employee)
        self.assertFalse(source.history_events.filter(event_type='ARCHIVED').exists())

        self.client.force_login(self.manager)
        self.client.post(reverse('smk:archive', args=[source.pk]))
        event = source.history_events.get(event_type=SmkHistoryEvent.EventType.ARCHIVED)
        self.assertEqual(event.actor, self.manager)
        # And the record is still readable, with its trail, from the archive.
        response = self.client.get(reverse('smk:detail', args=[source.pk]), {'tab': 'history'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Запись СМК помещена в архив')

    # ----------------------------------------------------------------- state

    def test_the_displayed_state_follows_the_tasks_and_the_shelf(self):
        """Создана → В работе → Завершена, and «Архив» over all of them.

        The four are derived on every read, never stored: `SmkSource.status`
        keeps only the shelf, so completing a task has to move the pill without
        anything writing to the record.
        """
        actions = self._actions(assignees=[self.employee]) + [
            {
                'text': 'Обновить инструкцию',
                'department': self.department,
                'due_date': timezone.localdate() + timedelta(days=9),
                'non_conformity': None,
                'requires_attachment': False,
                'assignees': [self.employee],
            },
        ]
        source = create_smk_source(
            origin=SmkSource.Origin.INTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Не ведётся журнал поверки'],
            actions=actions,
            created_by=self.smk,
        )
        first, second = Task.objects.filter(smk_source=source).order_by('pk')
        self.client.force_login(self.employee)
        url = reverse('smk:detail', args=[source.pk])

        self.assertEqual(self.client.get(url).context['state']['label'], 'Создана')

        complete_task(first, self.employee, 'Проведено')
        self.assertEqual(self.client.get(url).context['state']['label'], 'В работе')

        complete_task(second, self.employee, 'Обновлено')
        self.assertEqual(self.client.get(url).context['state']['label'], 'Завершена')
        # Nothing was written to the record to make that happen.
        source.refresh_from_db()
        self.assertEqual(source.status, SmkSource.Status.ACTIVE)

        # Archiving overrides the derived answer, in the list as on the page.
        archive_smk_source(source, actor=self.smk)
        self.assertEqual(self.client.get(url).context['state']['label'], 'Архив')

    # --------------------------------------------------------- notifications

    def test_every_assignee_is_notified_of_the_smk_task(self):
        """The measure reaches its исполнитель the same way every task does.

        One notification per assignee, sourced from the task itself, visible in
        the bell and carrying exactly one email delivery — created through
        `notifications.services`, so there is nothing СМК-specific to keep in
        step with the rest of the system.
        """
        second = self._user('smk_second_assignee', UserProfile.Role.TO)
        source = create_smk_source(
            origin=SmkSource.Origin.INTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Не ведётся журнал поверки'],
            actions=self._actions(assignees=[self.employee, second]),
            created_by=self.smk,
        )
        task = Task.objects.get(smk_source=source)

        assigned = Notification.objects.filter(
            event_type=Notification.EventType.SMK_TASK_ASSIGNED,
        )
        self.assertSetEqual(
            {item.recipient for item in assigned}, {self.employee, second}
        )
        for item in assigned:
            self.assertEqual(item.source_type, Notification.SourceType.TASK)
            self.assertEqual(item.related_task, task)
            self.assertIsNone(item.related_act_id)
            self.assertIsNone(item.related_protocol_id)
            self.assertEqual(item.related_url, reverse('tasks:detail', args=[task.pk]))
            self.assertEqual(item.title, f'Назначена задача по записи {source.label}')
            # Queued for email through the common delivery table, once.
            self.assertEqual(item.deliveries.count(), 1)

        # And the bell really shows it to the assignee.
        header = get_notification_header_state(self.employee)
        self.assertEqual(header['unread_count'], 1)
        self.assertEqual(header['items'][0].related_task, task)

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        APP_BASE_URL='https://quality.example.test',
        DEFAULT_FROM_EMAIL='quality@example.test',
    )
    def test_the_smk_notification_is_sent_by_the_common_email_worker(self):
        """No СМК email path: the existing queue renders and sends this one."""
        self.employee.email = 'smk_assignee@example.test'
        self.employee.save(update_fields=['email'])
        source = create_smk_source(
            origin=SmkSource.Origin.INTERNAL_AUDIT,
            audit_date=self.audit_date,
            non_conformities=['Не ведётся журнал поверки'],
            actions=self._actions(),
            created_by=self.smk,
        )
        task = Task.objects.get(smk_source=source)
        delivery = NotificationDelivery.objects.get(
            notification__related_task=task,
            notification__recipient=self.employee,
        )
        self.assertEqual(delivery.status, NotificationDelivery.Status.PENDING)

        self.assertEqual(
            process_delivery(delivery.pk), NotificationDelivery.Status.SENT
        )
        message = mail.outbox[-1]
        self.assertEqual(message.to, ['smk_assignee@example.test'])
        self.assertIn(f'Задача №{task.pk}', message.body)
        self.assertIn(source.label, message.body)
        self.assertIn(f'https://quality.example.test/quality/tasks/{task.pk}/', message.body)
        # The stored source code is never what the recipient reads.
        self.assertNotIn('SMK_TASK_ASSIGNED', message.body)

    # ----------------------------------------------------------- permissions

    def test_an_ordinary_user_is_offered_no_smk_action(self):
        """Reading is open; creating and archiving are the three roles only.

        One table rather than three tests: what matters is that the same answer
        governs the button, the page and the POST, so a regular user cannot
        reach an action by typing its URL either.
        """
        source = self._source()
        allowed = (self.smk, self.manager, self.admin)
        for user in allowed:
            self.assertTrue(can_create_smk_task(user), user)
            self.assertTrue(can_archive_smk_source(source, user), user)
        self.assertFalse(can_create_smk_task(self.employee))
        self.assertFalse(can_archive_smk_source(source, self.employee))

        # The regular user: list and record readable, neither action offered,
        # and both endpoints refused rather than merely hidden.
        self.client.force_login(self.employee)
        listing = self.client.get(reverse('smk:list'))
        self.assertEqual(listing.status_code, 200)
        self.assertFalse(listing.context['can_create'])
        self.assertNotContains(listing, reverse('smk:create'))
        detail = self.client.get(reverse('smk:detail', args=[source.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, 'Архивировать')
        self.assertEqual(self.client.get(reverse('smk:create')).status_code, 404)
        self.client.post(reverse('smk:archive', args=[source.pk]))
        source.refresh_from_db()
        self.assertEqual(source.status, SmkSource.Status.ACTIVE)
