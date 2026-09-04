"""Let an СМК мероприятие fan out into one task per исполнитель.

Exactly the change `0009_protocol_action_task_fanout` and `0010_act_task_split`
already made for the other two split-capable sources: the source shape stops
forbidding `individual_assignee` for `SMK`, and the single
`unique_smk_action_task` index becomes the usual pair — one shared task per
measure, one task per исполнитель within the split.

Nothing is rewritten. Every existing СМК task has a NULL `individual_assignee`,
so it is a shared task under the new rules and stays covered by the shared
constraint.
"""

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0016_task_cancellation'),
        ('smk', '0008_corrective_action_split_for_assignees'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='task',
            name='task_source_relations_match_source_type',
        ),
        migrations.RemoveConstraint(
            model_name='task',
            name='unique_smk_action_task',
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        source_type='ACT',
                        smk_source__isnull=True,
                        smk_action__isnull=True,
                        act__isnull=False,
                        root_analysis__isnull=False,
                        source_action__isnull=False,
                        protocol__isnull=True,
                        protocol_action__isnull=True,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    | Q(
                        source_type='PROTOCOL_APPROVAL',
                        smk_source__isnull=True,
                        smk_action__isnull=True,
                        act__isnull=True,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=False,
                        protocol_action__isnull=True,
                        individual_assignee__isnull=True,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    | Q(
                        source_type='PROTOCOL_ACTION',
                        smk_source__isnull=True,
                        smk_action__isnull=True,
                        act__isnull=True,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=False,
                        protocol_action__isnull=False,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    | Q(
                        source_type='ACT_REJECTION',
                        smk_source__isnull=True,
                        smk_action__isnull=True,
                        act__isnull=False,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=True,
                        protocol_action__isnull=True,
                        individual_assignee__isnull=True,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    | (
                        Q(
                            source_type='ACT_WORKFLOW',
                            smk_source__isnull=True,
                            smk_action__isnull=True,
                            act__isnull=False,
                            root_analysis__isnull=True,
                            source_action__isnull=True,
                            protocol__isnull=True,
                            protocol_action__isnull=True,
                            individual_assignee__isnull=True,
                        )
                        & ~Q(workflow_stage='')
                    )
                    | Q(
                        source_type='SMK',
                        act__isnull=True,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=True,
                        protocol_action__isnull=True,
                        smk_source__isnull=False,
                        smk_action__isnull=False,
                        department__isnull=False,
                        workflow_stage='',
                    )
                ),
                name='task_source_relations_match_source_type',
            ),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(
                condition=Q(individual_assignee__isnull=True),
                fields=('smk_action',),
                name='unique_shared_smk_action_task',
            ),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(
                fields=('smk_action', 'individual_assignee'),
                name='unique_individual_smk_action_task',
            ),
        ),
    ]
