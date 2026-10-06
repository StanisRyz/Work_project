"""Puts stage 18's demo back as `seed_demo.py` left it, between the rounds of
`stage18_e2e.py`: steps 3–5 of ZAP-1 unticked, nobody following, no message,
no mention or message notification.

    python manage.py shell < tasksandreports/boards/reports/stage-18/reset_demo.py
"""
from boards.models import (
    BoardCard, BoardCardChecklistItem, BoardCardComment, BoardCardCommentMention, BoardCardSubscription,
)
from notifications.models import Notification

card = BoardCard.objects.get(board__code='ZAP', number=1)
items = list(BoardCardChecklistItem.objects.filter(card=card).order_by('position'))
for item in items[2:]:
    item.is_done, item.done_by, item.done_at = False, None, None
    item.save(update_fields=['is_done', 'done_by', 'done_at'])
BoardCardSubscription.objects.filter(card__board=card.board).delete()
BoardCardCommentMention.objects.filter(comment__card__board=card.board).delete()
BoardCardComment.objects.filter(card__board=card.board).delete()
Notification.objects.filter(event_type__in=['BOARD_CARD_MENTION', 'BOARD_CARD_COMMENT']).delete()
print('reset')
