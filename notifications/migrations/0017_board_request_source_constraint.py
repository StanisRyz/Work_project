"""The source-shape constraint gains its `REQUEST` branch: exactly
`related_board_request` for a request notification, and that relation NULL
on every other branch. No row changes: `0016` added the column empty."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0016_board_request_source"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="notification",
            name="notification_source_relations_match_source_type",
        ),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("related_act__isnull", False),
                        ("related_board_request__isnull", True),
                        ("related_bug_report__isnull", True),
                        ("related_document__isnull", True),
                        ("related_protocol__isnull", True),
                        ("related_task__isnull", True),
                        ("source_type", "ACT"),
                    ),
                    models.Q(
                        ("related_act__isnull", True),
                        ("related_board_request__isnull", True),
                        ("related_bug_report__isnull", True),
                        ("related_document__isnull", True),
                        ("related_protocol__isnull", False),
                        ("related_task__isnull", True),
                        ("source_type", "PROTOCOL"),
                    ),
                    models.Q(
                        ("related_act__isnull", True),
                        ("related_board_request__isnull", True),
                        ("related_bug_report__isnull", True),
                        ("related_document__isnull", True),
                        ("related_protocol__isnull", True),
                        ("related_task__isnull", False),
                        ("source_type", "TASK"),
                    ),
                    models.Q(
                        ("related_act__isnull", True),
                        ("related_board_request__isnull", True),
                        ("related_bug_report__isnull", False),
                        ("related_document__isnull", True),
                        ("related_protocol__isnull", True),
                        ("related_task__isnull", True),
                        ("source_type", "BUG"),
                    ),
                    models.Q(
                        ("related_act__isnull", True),
                        ("related_board_request__isnull", True),
                        ("related_bug_report__isnull", True),
                        ("related_document__isnull", False),
                        ("related_protocol__isnull", True),
                        ("related_task__isnull", True),
                        ("source_type", "DOCUMENT"),
                    ),
                    models.Q(
                        ("related_act__isnull", True),
                        ("related_board_request__isnull", False),
                        ("related_bug_report__isnull", True),
                        ("related_document__isnull", True),
                        ("related_protocol__isnull", True),
                        ("related_task__isnull", True),
                        ("source_type", "REQUEST"),
                    ),
                    _connector="OR",
                ),
                name="notification_source_relations_match_source_type",
            ),
        ),
    ]
