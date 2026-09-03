"""The rules the СМК module must not lose.

Deliberately small: everything else here is already covered where it lives —
the completion guard by `tasks.tests`, the assignee/department pairing by the
protocol editor's own tests. What is new, and therefore tested, is who may
create and archive an СМК record, that a measure really becomes a `Task`, that the task
reaches the common registry with its СМК source named, that «Требуется
вложение» travels onto the task and is enforced there, and that nothing is
written before the creation is confirmed.
"""

import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from tasks.models import Task
from tasks.services import TaskWorkflowError, add_task_attachment, complete_task

from .models import SmkSource
from .permissions import can_create_smk_task
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
        self.assertIn(source, list(response.context['sources']))
        # The row carries what the table promises: the record, its audit type
        # and the real tasks its measures produced.
        self.assertContains(response, source.label)
        self.assertContains(response, reverse('smk:detail', args=[source.pk]))
        self.assertEqual(response.context['sources'][0].task_count, 1)
        # The navigation entry itself — rendered on every page by the sidebar.
        self.assertContains(response, f'href="{reverse("smk:list")}"')

        archive = self.client.get(reverse('smk:list'), {'tab': 'archive'})
        self.assertNotIn(source, list(archive.context['sources']))

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
        self.assertIn(source, list(archive.context['sources']))
        work = self.client.get(reverse('smk:list'))
        self.assertNotIn(source, list(work.context['sources']))
        detail = self.client.get(reverse('smk:detail', args=[source.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.context['can_archive'])
