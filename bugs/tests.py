"""What «Сообщить об ошибке» must not lose.

Deliberately small: the modal is the shared one, the task model is the shared
one and the delivery pipeline is the shared one, so what is new — and therefore
tested — is who receives a report, that the report itself is stored with its
page, that it raises one real task in «Задачи» so it cannot be read once and
forgotten, that the notification reaches the bell and the email queue through
the common service, and that a report is readable by exactly the people it
concerns.
"""

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, UserProfile
from notifications.email_delivery import process_delivery
from notifications.models import Notification, NotificationDelivery
from notifications.services import get_notification_header_state
from tasks.models import Task
from tasks.services import complete_task

from .models import BugReport
from .permissions import get_bug_responsible_users
from .services import BugWorkflowError, report_bug


class BugReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(code='DEP', name='Отдел')
        cls.reporter = cls._user('reporter', UserProfile.Role.OTK)
        # Two responsible accounts, and one ordinary employee who is not.
        cls.responsible = cls._user('responsible', UserProfile.Role.TO, bug=True)
        cls.responsible_two = cls._user('responsible2', UserProfile.Role.KO, bug=True)
        cls.bystander = cls._user('bystander', UserProfile.Role.KO)

    @classmethod
    def _user(cls, username, role, bug=False):
        user = User.objects.create_user(
            username=username, password='demo12345', email=f'{username}@example.com',
        )
        user.userprofile.role = role
        user.userprofile.department = cls.department
        user.userprofile.is_bug_responsible = bug
        user.userprofile.save()
        return user

    def test_only_accounts_marked_in_admin_receive_a_report(self):
        """The recipient list is the `is_bug_responsible` flag and nothing else.

        Not a role: the two responsible accounts here hold different roles, and
        the third employee shares a role with one of them yet hears nothing.
        Unmarking somebody in Admin takes effect on the very next report.
        """
        self.assertEqual(
            sorted(user.username for user in get_bug_responsible_users()),
            ['responsible', 'responsible2'],
        )

        report = report_bug(
            reporter=self.reporter,
            message='  На странице акта не открывается вкладка «Вложения».  ',
            page_url='/quality/acts/12/',
        )

        # The report is stored, stripped, with the page it was sent from.
        self.assertEqual(BugReport.objects.count(), 1)
        self.assertEqual(report.reporter, self.reporter)
        self.assertEqual(
            report.message, 'На странице акта не открывается вкладка «Вложения».',
        )
        self.assertEqual(report.page_url, '/quality/acts/12/')

        # Exactly the two marked accounts, through the common pipeline: bell
        # entry, `BUG` source pointing at the report, and one email delivery.
        notifications = Notification.objects.filter(
            event_type=Notification.EventType.BUG_REPORTED,
        )
        self.assertEqual(
            sorted(item.recipient.username for item in notifications),
            ['responsible', 'responsible2'],
        )
        for notification in notifications:
            self.assertEqual(notification.source_type, Notification.SourceType.BUG)
            self.assertEqual(notification.related_bug_report, report)
            self.assertEqual(notification.deliveries.count(), 1)
        self.assertEqual(
            get_notification_header_state(self.responsible)['unread_count'], 1,
        )
        self.assertEqual(
            get_notification_header_state(self.bystander)['unread_count'], 0,
        )

        # Unmarking one in Admin is all it takes to stop the next report.
        profile = self.responsible_two.userprofile
        profile.is_bug_responsible = False
        profile.save(update_fields=['is_bug_responsible'])
        report_bug(reporter=self.reporter, message='Вторая ошибка.')
        self.assertEqual(
            Notification.objects.filter(
                event_type=Notification.EventType.BUG_REPORTED,
                recipient=self.responsible_two,
            ).count(),
            1,
        )

    def test_a_report_raises_one_shared_task_for_the_responsible(self):
        """The report becomes work, so it cannot be read once and forgotten.

        One task per report — shared, not split: five responsible people are
        not asked to fix one bug five times, and whoever fixes it closes it for
        the rest. It is an ordinary work item from there on: it appears in
        «Мои задачи» of every responsible account, names the report as its
        source, and is completed with an execution comment like any other.
        """
        report = report_bug(
            reporter=self.reporter,
            message='Не открывается вкладка «Вложения».',
            page_url='/quality/acts/12/',
        )

        task = Task.objects.get()
        self.assertEqual(report.task, task)
        self.assertEqual(task.source_type, Task.SourceType.BUG)
        self.assertEqual(task.bug_report, report)
        self.assertEqual(task.task_text, report.message)
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        # Shared, and belonging to no single department: its people are chosen
        # by a flag and may sit in any number of them.
        self.assertIsNone(task.individual_assignee)
        self.assertIsNone(task.department)
        self.assertEqual(
            sorted(item.user.username for item in task.assignees.all()),
            ['responsible', 'responsible2'],
        )
        # A deadline in working days, never today.
        self.assertGreater(task.due_date, report.created_at.date())

        # It is in both responsible people's queue, and the registry names the
        # report as its source and links to it.
        self.client.force_login(self.responsible)
        listing = self.client.get(reverse('tasks:list'))
        rows = [row for row in listing.context['rows'] if row['task'].pk == task.pk]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source']['label'], report.label)
        self.assertEqual(
            rows[0]['source']['url'], reverse('bugs:detail', args=[report.pk]),
        )
        self.assertEqual(rows[0]['type_label'], 'Ошибка в системе')

        # Completed by whoever fixes it, in the ordinary way — and that closes
        # it for the other responsible account too.
        complete_task(task, self.responsible, 'Исправлено, вкладка открывается.')
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.completed_by, self.responsible)
        self.client.force_login(self.responsible_two)
        self.assertNotIn(
            task.pk,
            [row['task'].pk for row in self.client.get(reverse('tasks:list')).context['rows']],
        )

    def test_the_topbar_button_posts_the_report_and_comes_back(self):
        """The whole round trip the modal makes, and its refusals.

        The button posts `comment` to `bugs:report` with `?next=` — that is the
        shared confirmation modal's own contract — and the view sends the
        reporter back to the page they were on. An empty description is refused
        without writing anything, and a foreign `next` never redirects off-site.
        """
        self.client.force_login(self.reporter)
        url = reverse('bugs:report')

        response = self.client.post(
            f'{url}?next=/quality/tasks/',
            {'comment': 'Кнопка «Согласовать» не нажимается.'},
        )
        self.assertRedirects(
            response, '/quality/tasks/', fetch_redirect_response=False,
        )
        report = BugReport.objects.get()
        self.assertEqual(report.page_url, '/quality/tasks/')

        # An empty description writes nothing at all.
        self.client.post(url, {'comment': '   '})
        self.assertEqual(BugReport.objects.count(), 1)

        # An off-site `next` is never followed.
        self.client.post(
            f'{url}?next=https://example.com/', {'comment': 'Ещё одна ошибка.'},
        )
        self.assertEqual(
            BugReport.objects.latest('pk').page_url, reverse('dashboard:home'),
        )

        # GET is not a way to file one, and neither is an anonymous request.
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(url, {'comment': 'x'}).status_code, 302)
        self.assertEqual(BugReport.objects.count(), 2)

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        APP_BASE_URL='https://quality.example.test',
        DEFAULT_FROM_EMAIL='quality@example.test',
    )
    def test_the_notification_opens_a_page_only_the_right_people_may_read(self):
        """The link in the bell, and who may follow it.

        A responsible account and the author may open the report; an unrelated
        employee gets a 404, not an empty page. The email the delivery queue
        renders is the ordinary one — no bug-specific mail path exists.
        """
        report = report_bug(
            reporter=self.reporter, message='Ошибка на главной.', page_url='/',
        )
        notification = Notification.objects.get(recipient=self.responsible)
        detail_url = reverse('bugs:detail', args=[report.pk])
        self.assertEqual(notification.related_url, detail_url)
        self.assertEqual(notification.related_label, 'Открыть сообщение об ошибке')

        for user in (self.responsible, self.reporter):
            self.client.force_login(user)
            page = self.client.get(detail_url)
            self.assertEqual(page.status_code, 200, user.username)
            self.assertContains(page, 'Ошибка на главной.')

        self.client.force_login(self.bystander)
        self.assertEqual(self.client.get(detail_url).status_code, 404)

        # The common email worker renders it with no branch of its own.
        delivery = NotificationDelivery.objects.filter(
            notification__recipient=self.responsible,
        ).get()
        process_delivery(delivery.pk)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, NotificationDelivery.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(report.label, mail.outbox[0].subject)

    def test_a_report_with_nobody_marked_is_still_stored(self):
        """No recipients is not an error — the report must not be lost.

        It stays readable in Django Admin, which is where somebody would notice
        that nobody is marked «Ответственный за ошибки». There is nobody to
        raise a task on, so none is raised.
        """
        UserProfile.objects.update(is_bug_responsible=False)
        report = report_bug(reporter=self.reporter, message='Никого нет.')
        self.assertEqual(BugReport.objects.get(), report)
        self.assertFalse(
            Notification.objects.filter(
                event_type=Notification.EventType.BUG_REPORTED,
            ).exists()
        )
        # No task either: there is nobody to raise it on, and a task with no
        # assignee is refused by `_save_new_task()` anyway.
        self.assertFalse(Task.objects.exists())
        self.assertIsNone(report.task)

    def test_an_empty_description_is_refused_by_the_service(self):
        with self.assertRaises(BugWorkflowError):
            report_bug(reporter=self.reporter, message='   ')
        self.assertFalse(BugReport.objects.exists())
