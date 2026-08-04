"""RT-3 server side: the shared bell state, its fragment endpoint and the
template wiring the browser client depends on."""

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from notifications.services import (
    HEADER_NOTIFICATION_LIMIT,
    get_notification_header_state,
    mark_notifications_read,
)

from .tests import NotificationTestMixin


class HeaderStateMixin(NotificationTestMixin):
    def make_notification(self, recipient, key, *, act=None, is_read=False, title=None):
        notification = Notification.objects.create(
            recipient=recipient,
            actor=self.ko,
            event_type=Notification.EventType.COMMENT_ADDED,
            title=title or f'Заголовок {key}',
            message=f'Сообщение {key}',
            related_act=act or self.act,
            deduplication_key=key,
        )
        if is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=['is_read', 'read_at'])
        return notification


class NotificationHeaderStateTests(HeaderStateMixin, TestCase):
    def setUp(self):
        self.act = self.create_act()

    def test_unread_count_reflects_only_unread_rows(self):
        self.make_notification(self.otk, 'a')
        self.make_notification(self.otk, 'b')
        self.make_notification(self.otk, 'c', is_read=True)

        state = get_notification_header_state(self.otk)

        self.assertEqual(state['unread_count'], 2)

    def test_at_most_five_recent_unread_items_are_returned(self):
        for index in range(8):
            self.make_notification(self.otk, f'many-{index}')

        state = get_notification_header_state(self.otk)

        self.assertEqual(state['unread_count'], 8)
        self.assertEqual(len(state['items']), HEADER_NOTIFICATION_LIMIT)
        self.assertEqual(HEADER_NOTIFICATION_LIMIT, 5)

    def test_items_are_ordered_newest_first(self):
        created = [self.make_notification(self.otk, f'order-{index}') for index in range(3)]

        state = get_notification_header_state(self.otk)

        self.assertEqual(
            [item.pk for item in state['items']],
            [item.pk for item in reversed(created)],
        )
        self.assertEqual(state['latest_notification_id'], created[-1].pk)

    def test_read_notifications_are_excluded(self):
        unread = self.make_notification(self.otk, 'unread')
        self.make_notification(self.otk, 'read', is_read=True)

        state = get_notification_header_state(self.otk)

        self.assertEqual([item.pk for item in state['items']], [unread.pk])

    def test_another_users_notifications_are_excluded(self):
        mine = self.make_notification(self.otk, 'mine')
        theirs = self.make_notification(self.ko, 'theirs')

        state = get_notification_header_state(self.otk)

        self.assertEqual([item.pk for item in state['items']], [mine.pk])
        self.assertNotIn(theirs.pk, [item.pk for item in state['items']])

    def test_an_anonymous_user_costs_no_query(self):
        self.make_notification(self.otk, 'anon')

        with self.assertNumQueries(0):
            state = get_notification_header_state(AnonymousUser())

        self.assertEqual(state, {'unread_count': 0, 'items': (), 'latest_notification_id': None})

    def test_an_empty_state_reports_no_latest_id(self):
        state = get_notification_header_state(self.otk)

        self.assertEqual(state['unread_count'], 0)
        self.assertEqual(list(state['items']), [])
        self.assertIsNone(state['latest_notification_id'])


class NotificationHeaderFragmentTests(HeaderStateMixin, TestCase):
    def setUp(self):
        self.act = self.create_act()
        self.url = reverse('notifications:header_fragment')

    def test_authentication_is_required(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response['Location'])

    def test_only_get_is_allowed(self):
        self.client.force_login(self.otk)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 405)

    def test_the_payload_carries_count_html_and_timestamp(self):
        notification = self.make_notification(self.otk, 'payload')
        self.client.force_login(self.otk)

        payload = self.client.get(self.url).json()

        self.assertEqual(payload['unread_count'], 1)
        self.assertEqual(payload['latest_notification_id'], notification.pk)
        self.assertIn(f'data-notification-id="{notification.pk}"', payload['items_html'])
        self.assertIn(notification.title, payload['items_html'])
        self.assertTrue(payload['generated_at'])

    def test_only_the_current_users_notifications_are_returned(self):
        mine = self.make_notification(self.otk, 'mine', title='Мой заголовок')
        theirs = self.make_notification(self.ko, 'theirs', title='Чужой заголовок')
        self.client.force_login(self.otk)

        payload = self.client.get(self.url).json()

        self.assertEqual(payload['unread_count'], 1)
        self.assertIn(f'data-notification-id="{mine.pk}"', payload['items_html'])
        self.assertNotIn(f'data-notification-id="{theirs.pk}"', payload['items_html'])
        self.assertNotIn('Чужой заголовок', payload['items_html'])
        self.assertNotIn(theirs.message, payload['items_html'])

    def test_the_endpoint_accepts_no_user_parameter(self):
        self.make_notification(self.otk, 'mine')
        theirs = self.make_notification(self.ko, 'theirs', title='Чужой заголовок')
        self.client.force_login(self.otk)

        payload = self.client.get(
            self.url, {'user_id': self.ko.pk, 'recipient': self.ko.pk, 'user': self.ko.pk}
        ).json()

        self.assertEqual(payload['unread_count'], 1)
        self.assertNotIn(f'data-notification-id="{theirs.pk}"', payload['items_html'])
        self.assertNotIn('Чужой заголовок', payload['items_html'])

    def test_a_get_never_marks_anything_read(self):
        notification = self.make_notification(self.otk, 'stays-unread')
        self.client.force_login(self.otk)

        payload = self.client.get(self.url).json()

        notification.refresh_from_db()
        self.assertFalse(notification.is_read)
        self.assertIsNone(notification.read_at)
        self.assertEqual(payload['unread_count'], 1)

    def test_the_related_url_is_produced_by_the_server(self):
        notification = self.make_notification(self.otk, 'link')
        self.client.force_login(self.otk)

        payload = self.client.get(self.url).json()

        self.assertIn(
            f'href="{reverse("acts:detail", kwargs={"pk": self.act.pk})}"', payload['items_html']
        )
        self.assertEqual(notification.related_url, reverse('acts:detail', kwargs={'pk': self.act.pk}))

    def test_the_response_is_not_cacheable(self):
        self.client.force_login(self.otk)

        response = self.client.get(self.url)

        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('no-cache', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])
        self.assertEqual(response['Vary'], 'Cookie')

    def test_an_empty_state_renders_the_empty_placeholder(self):
        self.client.force_login(self.otk)

        payload = self.client.get(self.url).json()

        self.assertEqual(payload['unread_count'], 0)
        self.assertIn('data-notification-empty', payload['items_html'])
        self.assertIsNone(payload['latest_notification_id'])

    def test_the_fragment_matches_the_database_after_marking_read(self):
        first = self.make_notification(self.otk, 'one')
        self.make_notification(self.otk, 'two')
        self.client.force_login(self.otk)

        mark_notifications_read(self.otk, scope='bell', notification_ids=[first.pk])
        payload = self.client.get(self.url).json()

        self.assertEqual(payload['unread_count'], 1)
        self.assertNotIn(f'data-notification-id="{first.pk}"', payload['items_html'])


@override_settings(REALTIME_ENABLED=True)
class RealtimeTemplateWiringTests(HeaderStateMixin, TestCase):
    def setUp(self):
        self.act = self.create_act()

    def test_the_header_uses_the_shared_partial(self):
        notification = self.make_notification(self.otk, 'partial')
        self.client.force_login(self.otk)

        response = self.client.get(reverse('dashboard:home'))

        self.assertTemplateUsed(response, 'notifications/includes/header_items.html')
        content = response.content.decode()
        self.assertIn(f'data-notification-id="{notification.pk}"', content)
        self.assertIn('data-notification-title', content)
        self.assertIn('data-notification-message', content)
        self.assertIn('data-notification-unread="true"', content)

    def test_the_realtime_config_is_rendered_for_an_authenticated_user(self):
        self.client.force_login(self.otk)

        content = self.client.get(reverse('dashboard:home')).content.decode()

        self.assertIn('data-realtime-config', content)
        self.assertIn('data-realtime-enabled="true"', content)
        self.assertIn(f'data-events-url="{reverse("realtime:events")}"', content)
        self.assertIn(
            f'data-notification-fragment-url="{reverse("notifications:header_fragment")}"', content
        )
        self.assertIn(f'data-notifications-url="{reverse("notifications:list")}"', content)
        self.assertIn('js/realtime.js', content)

    def test_the_config_never_leaks_transport_details(self):
        self.client.force_login(self.otk)

        content = self.client.get(reverse('dashboard:home')).content.decode()

        self.assertNotIn('redis://', content)
        self.assertNotIn('quality-ecosystem:realtime', content)
        self.assertNotIn('data-user-id', content)
        self.assertNotIn(f'user:{self.otk.pk}', content)

    @override_settings(REALTIME_ENABLED=False)
    def test_a_disabled_configuration_renders_no_client(self):
        self.client.force_login(self.otk)

        content = self.client.get(reverse('dashboard:home')).content.decode()

        self.assertNotIn('data-realtime-config', content)
        self.assertNotIn('js/realtime.js', content)

    def test_an_anonymous_page_renders_no_client(self):
        content = self.client.get(reverse('accounts:login')).content.decode()

        self.assertNotIn('data-realtime-config', content)
        self.assertNotIn('js/realtime.js', content)

    def test_the_toast_region_is_accessible(self):
        self.client.force_login(self.otk)

        content = self.client.get(reverse('dashboard:home')).content.decode()

        self.assertIn('data-toast-region', content)
        self.assertIn('aria-live="polite"', content)
        self.assertIn('aria-relevant="additions"', content)

    def test_the_toast_region_exists_even_without_realtime(self):
        # The region is inert markup: it must not depend on the feature flag.
        with override_settings(REALTIME_ENABLED=False):
            self.client.force_login(self.otk)
            content = self.client.get(reverse('dashboard:home')).content.decode()

        self.assertIn('data-toast-region', content)


class BellMarkReadFlowTests(HeaderStateMixin, TestCase):
    """The existing bell behaviour must survive fragment replacement."""

    def setUp(self):
        self.act = self.create_act()
        self.bulk_url = reverse('notifications:mark_read_bulk')
        self.fragment_url = reverse('notifications:header_fragment')
        self.client.force_login(self.otk)

    def test_only_the_shown_notifications_are_marked_read(self):
        shown = [self.make_notification(self.otk, f'shown-{index}') for index in range(3)]
        hidden = self.make_notification(self.otk, 'hidden')

        response = self.client.post(self.bulk_url, {'ids': [item.pk for item in shown]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['unread_count'], 1)
        for item in shown:
            item.refresh_from_db()
            self.assertTrue(item.is_read)
        hidden.refresh_from_db()
        self.assertFalse(hidden.is_read)

    def test_the_fragment_after_the_post_matches_the_database(self):
        shown = [self.make_notification(self.otk, f'sync-{index}') for index in range(2)]
        remaining = self.make_notification(self.otk, 'remaining')

        self.client.post(self.bulk_url, {'ids': [item.pk for item in shown]})
        payload = self.client.get(self.fragment_url).json()

        self.assertEqual(payload['unread_count'], 1)
        self.assertEqual(
            payload['unread_count'],
            Notification.objects.filter(recipient=self.otk, is_read=False).count(),
        )
        self.assertIn(f'data-notification-id="{remaining.pk}"', payload['items_html'])
        for item in shown:
            self.assertNotIn(f'data-notification-id="{item.pk}"', payload['items_html'])

    def test_opening_the_bell_again_changes_nothing_further(self):
        notification = self.make_notification(self.otk, 'once')

        first = self.client.post(self.bulk_url, {'ids': [notification.pk]})
        notification.refresh_from_db()
        read_at = notification.read_at
        second = self.client.post(self.bulk_url, {'ids': [notification.pk]})

        self.assertEqual(first.json()['unread_count'], 0)
        self.assertEqual(second.json()['unread_count'], 0)
        notification.refresh_from_db()
        self.assertEqual(notification.read_at, read_at)

    def test_a_foreign_notification_is_never_touched(self):
        theirs = self.make_notification(self.ko, 'foreign')

        response = self.client.post(self.bulk_url, {'ids': [theirs.pk]})

        self.assertEqual(response.json()['unread_count'], 0)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_a_repeated_fragment_fetch_reports_a_stable_count(self):
        self.make_notification(self.otk, 'stable')

        first = self.client.get(self.fragment_url).json()
        second = self.client.get(self.fragment_url).json()

        self.assertEqual(first['unread_count'], second['unread_count'])
        self.assertEqual(first['items_html'], second['items_html'])
