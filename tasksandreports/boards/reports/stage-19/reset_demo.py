"""Puts stage 19's demo back as `seed_demo.py` left it, between the rounds of
`stage19_e2e.py`: no message (and so no file of «Чат»), nobody following, no
message notification. The older task attachment of ZAP-1 stays.

    python manage.py shell < tasksandreports/boards/reports/stage-19/reset_demo.py
"""
from boards.models import (
    BoardCard, BoardCardComment, BoardCardCommentMention, BoardCardFile, BoardCardSubscription,
)
from notifications.models import Notification

card = BoardCard.objects.get(board__code='ZAP', number=1)
BoardCardFile.objects.filter(card__board=card.board).delete()
BoardCardSubscription.objects.filter(card__board=card.board).delete()
BoardCardCommentMention.objects.filter(comment__card__board=card.board).delete()
BoardCardComment.objects.filter(card__board=card.board).delete()
Notification.objects.filter(event_type__in=['BOARD_CARD_MENTION', 'BOARD_CARD_COMMENT']).delete()
print('reset')
