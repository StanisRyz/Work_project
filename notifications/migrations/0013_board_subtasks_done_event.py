"""One board notification: `BOARD_SUBTASKS_DONE` — the last open subtask of a
card closed while the card is still in work.

Choices only — the column, its length and every existing row are unchanged.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0012_board_mention_and_completed_events"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("ACT_SENT_TO_KO", "Акт передан в КО"),
                    ("ACT_SENT_TO_TO", "Акт передан в ТО"),
                    ("ACT_SENT_TO_OTK", "Акт передан на проверку ОТК"),
                    ("ACT_RETURNED_TO_OTK", "Акт возвращён в ОТК"),
                    ("ACT_RETURNED_TO_KO", "Акт возвращён в КО"),
                    ("ACT_RETURNED_TO_TO", "Акт возвращён в ТО"),
                    ("ACTION_ASSIGNED", "Назначено мероприятие"),
                    ("ACT_APPROVED", "Акт утверждён"),
                    ("COMMENT_ADDED", "Добавлен комментарий"),
                    ("PROTOCOL_APPROVAL_REQUIRED", "Требуется согласование протокола"),
                    (
                        "PROTOCOL_RETURNED_FOR_REVISION",
                        "Протокол возвращён на доработку",
                    ),
                    ("PROTOCOL_APPROVED", "Протокол согласован"),
                    ("PROTOCOL_TASK_ASSIGNED", "Назначена задача по протоколу"),
                    ("ACT_REJECTION_ASSIGNED", "Назначена задача ПДО по браку"),
                    ("SMK_TASK_ASSIGNED", "Назначена задача СМК"),
                    ("BOARD_TASK_ASSIGNED", "Назначена задача на доске"),
                    ("BOARD_TASK_CANCELLED", "Карточка отменена"),
                    ("BOARD_CARD_COMMENT", "Новое сообщение в карточке"),
                    ("BOARD_CARD_MENTION", "Упоминание в карточке"),
                    ("BOARD_CARD_COMPLETED", "Карточка выполнена"),
                    ("BOARD_SUBTASKS_DONE", "Все подзадачи выполнены"),
                    ("BUG_REPORTED", "Сообщение об ошибке в системе"),
                    ("DOCUMENT_ACK_REQUIRED", "Требуется ознакомление с документом"),
                    ("DOCUMENT_APPROVAL_REQUIRED", "Требуется согласование документа"),
                    ("DOCUMENT_REVIEW_DUE", "Подходит срок пересмотра документа"),
                    ("DOCUMENT_VERSION_RETURNED", "Версия документа возвращена"),
                    ("DOCUMENT_UPDATED", "Новая версия документа"),
                ],
                max_length=40,
                verbose_name="Тип события",
            ),
        ),
    ]
