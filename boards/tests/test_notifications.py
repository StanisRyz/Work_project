"""«Назначена задача на доске»: who is told, by bell and by mail, and where it leads."""

from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from notifications.models import Notification, NotificationDelivery
from notifications.services import (
    EMAIL_ELIGIBLE_EVENTS,
    describe_notification_source,
    get_notification_url,
    notify_board_task_assigned,
)
from tasks.models import Task

from ..services import update_card
from .helpers import BoardFixtureMixin, due


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def board_notifications():
    return Notification.objects.filter(event_type=Notification.EventType.BOARD_TASK_ASSIGNED)


class BoardNotificationTests(BoardFixtureMixin, TestCase):
    def test_creation_notifies_every_assignee_but_the_creator(self):
        card = self.card(actor=self.member, assignees=[self.member, self.colleague])
        notes = board_notifications()
        self.assertEqual([note.recipient for note in notes], [self.colleague])
        note = notes.get()
        self.assertEqual(note.source_type, Notification.SourceType.TASK)
        self.assertEqual(note.related_task, task_of(card))
        self.assertEqual(note.actor, self.member)

    def test_update_notifies_only_the_added(self):
        card = self.card(actor=self.owner, assignees=[self.member])
        self.assertEqual(list(board_notifications().values_list('recipient', flat=True)), [self.member.pk])
        update_card(
            card, actor=self.owner, title=card.title, description='', due_date=due(),
            assignee_ids=[self.colleague.pk],
        )
        self.assertEqual(
            sorted(board_notifications().values_list('recipient', flat=True)),
            sorted([self.member.pk, self.colleague.pk]),
        )
        # Nothing new for the one who stayed or a pure text edit.
        update_card(
            card, actor=self.owner, title='Новое', description='', due_date=due(),
            assignee_ids=[self.colleague.pk],
        )
        self.assertEqual(board_notifications().count(), 2)

    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
    def test_email_is_queued_and_never_says_board_code(self):
        self.colleague.email = 'colleague@example.com'
        self.colleague.save()
        self.card('Подготовить КП', actor=self.owner, assignees=[self.colleague])
        note = board_notifications().get()
        self.assertIn(Notification.EventType.BOARD_TASK_ASSIGNED, EMAIL_ELIGIBLE_EVENTS)
        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification=note, status=NotificationDelivery.Status.PENDING,
            ).exists()
        )
        source = describe_notification_source(note)
        self.assertEqual(source['context'], 'Доска «Планирование»')
        self.assertIn('«Планирование»', note.title)
        for text in (note.title, note.message, source['label'], source['context']):
            self.assertNotIn('BOARD', text)

    def test_link_opens_the_card_on_the_board(self):
        card = self.card(actor=self.owner, assignees=[self.colleague])
        note = board_notifications().get()
        url = get_notification_url(note)
        self.assertEqual(url, reverse('tasks:detail', args=[task_of(card).pk]))
        self.client.force_login(self.colleague)
        self.assertRedirects(
            self.client.get(url),
            f"{reverse('boards:detail', args=[self.board.pk])}?card={card.pk}",
        )

    def test_rollback_leaves_no_notification(self):
        try:
            with transaction.atomic():
                self.card(actor=self.owner, assignees=[self.colleague])
                raise RuntimeError('после записи')
        except RuntimeError:
            pass
        self.assertFalse(board_notifications().exists())
        self.assertFalse(Task.objects.filter(source_type=Task.SourceType.BOARD).exists())

    def test_refused_for_another_task_source(self):
        task = task_of(self.card(actor=self.owner))
        task.source_type = Task.SourceType.BUG
        with self.assertRaises(ValueError):
            notify_board_task_assigned(task, self.owner, [self.member])

    def test_no_second_task_notification(self):
        card = self.card(actor=self.owner, assignees=[self.member])
        self.assertEqual(
            Notification.objects.filter(related_task=task_of(card)).count(), 1,
        )
