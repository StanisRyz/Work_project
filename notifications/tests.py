from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import BadHeaderError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActCorrectiveActionAssignee, ActHistoryEvent, ActRootAnalysis
from acts.services import (
    add_act_comment,
    add_act_history_event,
    apply_ko_decision,
    apply_structured_to_analysis,
    apply_to_analysis,
    approve_act,
    return_to_ko,
    return_to_otk,
    return_to_to,
    send_to_ko,
)
from notifications.email_delivery import process_delivery
from notifications.models import Notification, NotificationDelivery
from notifications.services import create_notifications, notify_action_assigned, notify_history_event
from references.models import ActStatus, DefectType, Operation, TaskStatus


class NotificationTestMixin:
    @classmethod
    def setUpTestData(cls):
        cls.statuses = {}
        for code, name in (
            ('CREATED_OTK', 'Создан ОТК'),
            ('KO_REVIEW', 'На рассмотрении КО'),
            ('TO_ANALYSIS', 'На анализе ТО'),
            ('OTK_REVIEW', 'На проверке ОТК'),
            ('ARCHIVED', 'Архивирован'),
        ):
            cls.statuses[code], _ = ActStatus.objects.get_or_create(code=code, defaults={'name': name})
        cls.task_status, _ = TaskStatus.objects.get_or_create(
            code='IN_PROGRESS',
            defaults={'name': 'В работе'},
        )
        cls.operation = Operation.objects.create(code='NOTIFY_OP', name='Операция уведомлений')
        cls.defect_type = DefectType.objects.create(code='NOTIFY_DEFECT', name='Дефект уведомлений')
        cls.department = Department.objects.create(code='NOTIFY_TO', name='Технологический отдел')
        cls.otk = cls.create_user('notify_otk', UserProfile.Role.OTK, email='otk@example.test')
        cls.ko = cls.create_user('notify_ko', UserProfile.Role.KO, email='ko@example.test')
        cls.ko_second = cls.create_user('notify_ko_second', UserProfile.Role.KO, email='ko2@example.test')
        cls.to = cls.create_user('notify_to', UserProfile.Role.TO, email='to@example.test')
        cls.to_second = cls.create_user('notify_to_second', UserProfile.Role.TO, email='to2@example.test')
        for user in (cls.to, cls.to_second):
            user.userprofile.department = cls.department
            user.userprofile.save(update_fields=['department'])

    @classmethod
    def create_user(cls, username, role, email=''):
        user = User.objects.create_user(username=username, password='test-password', email=email)
        user.userprofile.role = role
        user.userprofile.save(update_fields=['role'])
        return user

    def create_act(self, status_code='CREATED_OTK', **overrides):
        values = {
            'created_by': self.otk,
            'party_number': '1/2-3',
            'nomenclature': 'Изделие',
            'operation': self.operation,
            'defect_type': self.defect_type,
            'status': self.statuses[status_code],
            'description': 'Описание',
        }
        values.update(overrides)
        return Act.objects.create(**values)

    def create_action(self, act, assignees=None):
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment='Корректирующее мероприятие',
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=1),
        )
        for user in assignees or [self.to]:
            ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=user)
        return action


class NotificationModelTests(NotificationTestMixin, TestCase):
    def test_read_state_and_constraints(self):
        act = self.create_act()
        notification = Notification.objects.create(
            recipient=self.otk,
            actor=self.ko,
            event_type=Notification.EventType.COMMENT_ADDED,
            title='Новое уведомление',
            message='Сообщение',
            related_act=act,
            deduplication_key='comment:1',
        )

        self.assertTrue(notification.mark_read())
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)
        self.assertFalse(notification.mark_read())

        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(
                recipient=self.otk,
                actor=self.ko,
                event_type=Notification.EventType.COMMENT_ADDED,
                title='Дубликат',
                message='Сообщение',
                related_act=act,
                deduplication_key='comment:1',
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(
                recipient=self.ko,
                actor=self.otk,
                event_type=Notification.EventType.COMMENT_ADDED,
                title='Некорректное состояние',
                message='Сообщение',
                related_act=act,
                deduplication_key='bad-state',
                is_read=True,
                read_at=None,
            )


class NotificationRoutingTests(NotificationTestMixin, TestCase):
    def assert_recipients(self, event_type, expected):
        self.assertSetEqual(
            set(Notification.objects.filter(event_type=event_type).values_list('recipient__username', flat=True)),
            set(expected),
        )

    def test_role_queue_transitions_notify_all_active_target_users(self):
        inactive = self.create_user('inactive_ko', UserProfile.Role.KO)
        inactive.is_active = False
        inactive.save(update_fields=['is_active'])
        inactive_profile = self.create_user('inactive_profile_ko', UserProfile.Role.KO)
        inactive_profile.userprofile.is_active = False
        inactive_profile.userprofile.save(update_fields=['is_active'])
        act = self.create_act()

        send_to_ko(act, self.otk)
        self.assert_recipients(Notification.EventType.ACT_SENT_TO_KO, ['notify_ko', 'notify_ko_second'])

        apply_ko_decision(act, self.ko, [(None, Act.KoDecision.ALLOW_NO_REWORK, 'Допустить')])
        self.assert_recipients(Notification.EventType.ACT_SENT_TO_TO, ['notify_to', 'notify_to_second'])

        apply_to_analysis(act, self.to, 'Причина', 'Мероприятие')
        self.assert_recipients(Notification.EventType.ACT_SENT_TO_OTK, ['notify_otk'])

    def test_every_return_routes_to_previous_processing_stage_without_comment_duplicate(self):
        returned_otk = self.create_act('KO_REVIEW')
        return_to_otk(returned_otk, self.ko, 'Исправить данные.')
        self.assert_recipients(Notification.EventType.ACT_RETURNED_TO_OTK, ['notify_otk'])

        returned_ko = self.create_act('TO_ANALYSIS')
        return_to_ko(returned_ko, self.to, 'Уточнить решение.')
        self.assert_recipients(Notification.EventType.ACT_RETURNED_TO_KO, ['notify_ko', 'notify_ko_second'])

        returned_to = self.create_act('OTK_REVIEW')
        return_to_to(returned_to, self.otk, 'Уточнить анализ.')
        self.assert_recipients(Notification.EventType.ACT_RETURNED_TO_TO, ['notify_to', 'notify_to_second'])
        self.assertFalse(Notification.objects.filter(event_type=Notification.EventType.COMMENT_ADDED).exists())

    def test_assignment_notifies_only_each_active_assignee_and_deduplicates_users(self):
        act = self.create_act('TO_ANALYSIS')
        analysis_data = [{
            'root_cause': 'Причина',
            'actions': [{
                'comment': 'Мероприятие',
                'department': self.department,
                'assignees': [self.to, self.to_second],
                'due_date': timezone.localdate() + timedelta(days=1),
            }],
        }]

        apply_structured_to_analysis(act, self.to, analysis_data)

        action = ActCorrectiveAction.objects.get(root_analysis__act=act)
        notify_action_assigned(action, self.to, [self.to_second, self.to_second])

        self.assert_recipients(Notification.EventType.ACTION_ASSIGNED, ['notify_to', 'notify_to_second'])

    def test_approval_and_normal_comment_are_in_app_only_and_exclude_actor(self):
        act = self.create_act(
            'OTK_REVIEW',
            ko_decision_by=self.ko,
            to_analysis_by=self.to,
        )
        self.create_action(act, [self.to_second])

        approve_act(act, self.otk)

        self.assert_recipients(
            Notification.EventType.ACT_APPROVED,
            ['notify_ko', 'notify_to', 'notify_to_second'],
        )
        self.assertFalse(
            NotificationDelivery.objects.filter(
                notification__event_type=Notification.EventType.ACT_APPROVED,
            ).exists()
        )

        comment_act = self.create_act('KO_REVIEW')
        add_act_comment(comment_act, self.ko, 'Обычная заметка.')
        self.assert_recipients(Notification.EventType.COMMENT_ADDED, ['notify_ko_second'])
        self.assertFalse(
            NotificationDelivery.objects.filter(
                notification__event_type=Notification.EventType.COMMENT_ADDED,
            ).exists()
        )

    def test_duplicate_event_call_does_not_duplicate_notifications_or_deliveries(self):
        act = self.create_act('KO_REVIEW')
        event = ActHistoryEvent.objects.create(
            act=act,
            user=self.otk,
            event_type=ActHistoryEvent.EventType.SENT_TO_KO,
            message='Передан в КО.',
        )

        notify_history_event(event)
        notify_history_event(event)

        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(NotificationDelivery.objects.count(), 2)

    def test_outer_transaction_rollback_removes_workflow_event_notifications_and_deliveries(self):
        act = self.create_act()

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                send_to_ko(act, self.otk)
                raise RuntimeError('rollback')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(NotificationDelivery.objects.exists())


class NotificationViewTests(NotificationTestMixin, TestCase):
    def create_notification(self, recipient, key, is_read=False):
        return Notification.objects.create(
            recipient=recipient,
            actor=self.ko,
            event_type=Notification.EventType.COMMENT_ADDED,
            title=f'Уведомление {key}',
            message='Сообщение',
            related_act=self.create_act(),
            deduplication_key=key,
            is_read=is_read,
            read_at=timezone.now() if is_read else None,
        )

    def test_page_requires_login_and_shows_only_own_notifications_with_bell_counter(self):
        own = self.create_notification(self.otk, 'own')
        other = self.create_notification(self.ko, 'other')
        response = self.client.get(reverse('notifications:list'))
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.otk)
        response = self.client.get(reverse('notifications:list'))

        self.assertContains(response, own.title)
        self.assertNotContains(response, other.title)
        self.assertContains(response, '1 непрочитанных')
        self.assertContains(response, own.related_url)

    def test_unread_filter_mark_one_and_mark_all_are_owner_only_posts(self):
        unread = self.create_notification(self.otk, 'unread')
        read = self.create_notification(self.otk, 'read', is_read=True)
        other = self.create_notification(self.ko, 'other')
        self.client.force_login(self.otk)

        response = self.client.get(reverse('notifications:list'), {'filter': 'unread'})
        page_titles = [notification.title for notification in response.context['page_obj']]
        self.assertIn(unread.title, page_titles)
        self.assertNotIn(read.title, page_titles)

        mark_url = reverse('notifications:mark_read', args=[unread.pk])
        self.assertEqual(self.client.get(mark_url).status_code, 405)
        self.assertEqual(self.client.post(reverse('notifications:mark_read', args=[other.pk])).status_code, 404)
        self.client.post(mark_url)
        unread.refresh_from_db()
        self.assertTrue(unread.is_read)
        self.assertIsNotNone(unread.read_at)

        second = self.create_notification(self.otk, 'second')
        self.client.post(reverse('notifications:mark_all_read'))
        second.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(second.is_read)
        self.assertFalse(other.is_read)

    def test_notifications_are_paginated(self):
        for index in range(22):
            self.create_notification(self.otk, f'page-{index}')
        self.client.force_login(self.otk)

        response = self.client.get(reverse('notifications:list'))

        self.assertEqual(len(response.context['page_obj']), 20)
        self.assertEqual(response.context['page_obj'].paginator.num_pages, 2)


@override_settings(
    EMAIL_NOTIFICATIONS_ENABLED=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    APP_BASE_URL='https://quality.example.test',
    DEFAULT_FROM_EMAIL='quality@example.test',
    EMAIL_NOTIFICATION_RETRY_DELAY_SECONDS=0,
    EMAIL_NOTIFICATION_MAX_ATTEMPTS=2,
)
class NotificationEmailTests(NotificationTestMixin, TestCase):
    def queue_delivery(self, recipient=None):
        act = self.create_act('KO_REVIEW')
        notifications = create_notifications(
            event_type=Notification.EventType.ACT_SENT_TO_KO,
            act=act,
            actor=self.otk,
            recipients=[recipient or self.ko],
            source_key=f'test:{act.pk}',
        )
        return notifications[0].deliveries.get(), act

    def test_successful_delivery_uses_safe_text_and_html_templates_and_authenticated_link(self):
        delivery, act = self.queue_delivery()

        result = process_delivery(delivery.pk)

        delivery.refresh_from_db()
        self.assertEqual(result, NotificationDelivery.Status.SENT)
        self.assertEqual(delivery.status, NotificationDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn(act.number, message.subject)
        self.assertIn(f'https://quality.example.test/acts/{act.pk}/', message.body)
        self.assertIn('Требуемое действие', message.body)
        self.assertNotIn(act.description, message.body)
        self.assertEqual(message.alternatives[0].mimetype, 'text/html')

    def test_return_comment_is_not_in_email_content(self):
        act = self.create_act('KO_REVIEW')
        confidential_comment = 'Секретная причина возврата.'
        return_to_otk(act, self.ko, confidential_comment)
        delivery = NotificationDelivery.objects.get(
            notification__event_type=Notification.EventType.ACT_RETURNED_TO_OTK,
        )

        process_delivery(delivery.pk)

        message = mail.outbox[0]
        self.assertNotIn(confidential_comment, message.body)
        self.assertNotIn(confidential_comment, message.alternatives[0].content)

    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=False)
    def test_disabled_email_is_skipped_at_creation_without_old_backlog(self):
        act = self.create_act()
        notification = create_notifications(
            event_type=Notification.EventType.ACT_SENT_TO_KO,
            act=act,
            actor=self.otk,
            recipients=[self.ko],
            source_key='disabled',
        )[0]

        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, NotificationDelivery.Status.SKIPPED)
        self.assertIn('отключены', delivery.last_error)

    def test_missing_email_is_skipped(self):
        user = self.create_user('no_email', UserProfile.Role.KO)
        delivery, _act = self.queue_delivery(user)
        self.assertEqual(delivery.status, NotificationDelivery.Status.SKIPPED)
        self.assertIn('не указан', delivery.last_error)

    def test_retryable_and_permanent_failures_and_attempt_limit(self):
        retryable, _act = self.queue_delivery()
        with mock.patch('notifications.email_delivery._send_email', side_effect=OSError('temporary')):
            self.assertEqual(process_delivery(retryable.pk), NotificationDelivery.Status.PENDING)
        retryable.refresh_from_db()
        self.assertEqual(retryable.attempts, 1)

        with mock.patch('notifications.email_delivery._send_email', side_effect=OSError('temporary')):
            self.assertEqual(process_delivery(retryable.pk), NotificationDelivery.Status.FAILED)
        retryable.refresh_from_db()
        self.assertEqual(retryable.attempts, 2)

        permanent, _act = self.queue_delivery(self.ko_second)
        with mock.patch('notifications.email_delivery._send_email', side_effect=BadHeaderError('invalid header')):
            self.assertEqual(process_delivery(permanent.pk), NotificationDelivery.Status.FAILED)
        permanent.refresh_from_db()
        self.assertEqual(permanent.attempts, 1)
        self.assertIn('invalid header', permanent.last_error)

    @override_settings(EMAIL_HOST_PASSWORD='smtp-secret')
    def test_delivery_errors_are_sanitized(self):
        delivery, _act = self.queue_delivery()
        with mock.patch(
            'notifications.email_delivery._send_email',
            side_effect=OSError('smtp-secret\nconnection lost'),
        ):
            process_delivery(delivery.pk)

        delivery.refresh_from_db()
        self.assertNotIn('smtp-secret', delivery.last_error)
        self.assertNotIn('\n', delivery.last_error)
        self.assertIn('[скрыто]', delivery.last_error)

    def test_management_command_processes_batch(self):
        delivery, _act = self.queue_delivery()
        output = StringIO()

        call_command('process_notification_deliveries', batch_size=1, stdout=output)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, NotificationDelivery.Status.SENT)
        self.assertIn('отправлено — 1', output.getvalue())
