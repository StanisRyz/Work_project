from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from acts.models import Act
from references.models import ActStatus, DefectType, Operation, Priority


class Command(BaseCommand):
    help = 'Create or update local demo acts.'

    def handle(self, *args, **options):
        try:
            otk_user = User.objects.get(username='otk_user')
            ko_user = User.objects.get(username='ko_user')
            to_user = User.objects.get(username='to_user')
            operation = Operation.objects.get(code='NAVIVKA')
            defect_type = DefectType.objects.get(code='SIZE_DEVIATION')
            priority = Priority.objects.get(code='HIGH')
            statuses = {
                status.code: status
                for status in ActStatus.objects.filter(
                    code__in=[
                        'CREATED_OTK',
                        'KO_REVIEW',
                        'TO_ANALYSIS',
                        'ACTIONS_ASSIGNED',
                        'CLOSED',
                    ]
                )
            }
            for code in ['CREATED_OTK', 'KO_REVIEW', 'TO_ANALYSIS', 'ACTIONS_ASSIGNED', 'CLOSED']:
                if code not in statuses:
                    raise ActStatus.DoesNotExist
        except (User.DoesNotExist, Operation.DoesNotExist, DefectType.DoesNotExist, Priority.DoesNotExist, ActStatus.DoesNotExist):
            self.stdout.write(self.style.ERROR('Run seed_demo_accounts and seed_references first.'))
            return

        today = timezone.localdate()
        demo_acts = [
            ('АОК-DEMO-001', statuses['CREATED_OTK'], otk_user, 'П-1001', 'Катушка А', ''),
            ('АОК-DEMO-002', statuses['KO_REVIEW'], otk_user, 'П-1002', 'Катушка Б', ''),
            ('АОК-DEMO-003', statuses['TO_ANALYSIS'], otk_user, 'П-1003', 'Катушка В', 'ALLOW'),
            ('АОК-DEMO-004', statuses['ACTIONS_ASSIGNED'], otk_user, 'П-1004', 'Катушка Г', 'ALLOW'),
            ('АОК-DEMO-005', statuses['CLOSED'], otk_user, 'П-1005', 'Катушка Д', 'REJECT'),
        ]

        for index, (number, status, created_by, party, nomenclature, decision) in enumerate(demo_acts, start=1):
            act, _ = Act.objects.update_or_create(
                number=number,
                defaults={
                    'created_by': created_by,
                    'party_number': party,
                    'nomenclature': nomenclature,
                    'operation': operation,
                    'defect_type': defect_type,
                    'priority': priority,
                    'status': status,
                    'description': 'Демонстрационный акт для проверки маршрута ОТК - КО - ТО.',
                    'due_date': today + timedelta(days=index + 2),
                },
            )
            if status.code in {'TO_ANALYSIS', 'ACTIONS_ASSIGNED', 'CLOSED'}:
                act.ko_decision = decision
                act.ko_comment = 'Демонстрационное решение КО.'
                act.ko_decision_by = ko_user
                act.ko_decision_at = timezone.now()
            if status.code in {'ACTIONS_ASSIGNED', 'CLOSED'}:
                act.to_root_cause = 'Демонстрационная корневая причина.'
                act.to_action_summary = 'Демонстрационное описание мероприятий.'
                act.to_analysis_by = to_user
                act.to_analysis_at = timezone.now()
            act.save()

        self.stdout.write(self.style.SUCCESS('Demo acts ready: 5 acts across MVP statuses.'))
