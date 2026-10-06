"""Stage 18 on PostgreSQL, after `seed_demo.py`: the checklist, the audience,
the mentions and their constraints through the services and the selectors.

    python manage.py shell < tasksandreports/boards/reports/stage-18/pg_checks.py
"""
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.template import Context, Template

from boards.models import (
    BoardCard, BoardCardChecklistItem, BoardCardComment, BoardCardCommentMention, BoardCardEvent,
    BoardCardSubscription,
)
from boards.selectors import build_board_table, card_audience, checklist_counts, describe_card_event
from boards.services import (
    BoardError, add_checklist_item, complete_card, move_checklist_item, post_card_comment, reopen_card,
    toggle_card_subscription, toggle_checklist_item,
)
from notifications.models import Notification, NotificationDelivery

admin, ivanov, petrova = (User.objects.get(username=name) for name in ('admin1', 'ivanov', 'petrova'))
card = BoardCard.objects.get(board__code='ZAP', number=1)
task = card.tasks.get()
print('чек-лист ZAP-1 (сделано, всего):', checklist_counts(card))
items = list(BoardCardChecklistItem.objects.filter(card=card).order_by('position'))
toggle_checklist_item(items[2], actor=petrova)
move_checklist_item(items[4], actor=petrova, direction='up')
print('после отметки и переноса:', checklist_counts(card),
      [item.text for item in BoardCardChecklistItem.objects.filter(card=card).order_by('position')])
print('журнал:', [describe_card_event(event) for event in BoardCardEvent.objects.filter(card=card, kind='CHECKLIST')])
try:
    add_checklist_item(card, actor=ivanov, text='я' * 201)
except BoardError as exc:
    print('201 символ:', exc)
table = build_board_table(card.board, card.sub_board, admin)
print('таблица, «Чек-лист»:', {row['card'].code: row['checklist_label'] for row in table['rows']})

print('подписка admin1:', toggle_card_subscription(card, actor=admin, subscribe=True))
comment = post_card_comment(card, actor=ivanov, text='@Мария Петрова, сроки?', mentions=[petrova.pk, petrova.pk, 'x', ivanov.pk])
print('упомянуты:', list(BoardCardCommentMention.objects.filter(comment=comment).values_list('user__username', flat=True)))
print('подписчики:', sorted(BoardCardSubscription.objects.filter(card=card).values_list('user__username', flat=True)))
print('аудитория (один запрос, DISTINCT):', [user.username for user in card_audience(card, task)])
for event_type in ('BOARD_CARD_MENTION', 'BOARD_CARD_COMMENT'):
    notes = Notification.objects.filter(event_type=event_type, related_task=task)
    print(event_type, sorted(notes.values_list('recipient__username', flat=True)),
          'писем:', NotificationDelivery.objects.filter(notification__in=notes).count())
html = Template('{% load board_text %}{{ comment|with_mentions }}').render(Context({
    'comment': BoardCardComment.objects.prefetch_related('mentions__user').get(pk=comment.pk),
}))
print('выделение:', html)
for model, values in (
    (BoardCardSubscription, {'card': card, 'user': admin}),
    (BoardCardCommentMention, {'comment': comment, 'user': petrova}),
):
    try:
        with transaction.atomic():
            model.objects.create(**values)
    except IntegrityError as exc:
        print(f'{model.__name__}: IntegrityError ({type(exc.__cause__).__name__})')

complete_card(card, actor=ivanov, execution_comment='Готово')
reopen_card(card, actor=admin)
complete_card(card, actor=ivanov, execution_comment='Готово снова')
print('BOARD_CARD_COMPLETED:', sorted(Notification.objects.filter(
    event_type='BOARD_CARD_COMPLETED', related_task=task).values_list('recipient__username', flat=True)))
