from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from realtime.context_processors import COORDINATION_EPOCH_SESSION_KEY


@override_settings(REALTIME_ENABLED=True)
class RealtimeCoordinationEpochTests(TestCase):
    def test_same_user_gets_distinct_opaque_epochs_for_distinct_sessions(self):
        user = User.objects.create_user(username='epoch_user', password='demo12345')
        clients = (Client(), Client())
        epochs = []

        for client in clients:
            client.force_login(user)
            response = client.get(reverse('dashboard:home'))
            epoch = response.context['realtime_client']['coordination_epoch']
            session_key = client.session.session_key

            self.assertEqual(client.session[COORDINATION_EPOCH_SESSION_KEY], epoch)
            self.assertNotEqual(epoch, session_key)
            self.assertNotIn(session_key, response.content.decode('utf-8'))
            epochs.append(epoch)

        self.assertNotEqual(epochs[0], epochs[1])
