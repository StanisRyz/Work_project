from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from accounts.models import Department, UserProfile


DEMO_PASSWORD = 'demo12345'


class Command(BaseCommand):
    help = 'Create or update local demo departments and users.'

    def handle(self, *args, **options):
        departments = {
            'OTK': 'ОТК',
            'KO': 'КО',
            'TO': 'ТО',
            'MANAGEMENT': 'Руководство',
        }
        department_objects = {}

        for code, name in departments.items():
            department, _ = Department.objects.update_or_create(
                code=code,
                defaults={'name': name, 'is_active': True},
            )
            department_objects[code] = department

        demo_users = [
            ('otk_user', UserProfile.Role.OTK, 'OTK', False, False),
            ('ko_user', UserProfile.Role.KO, 'KO', False, False),
            ('to_user', UserProfile.Role.TO, 'TO', False, False),
            ('manager_user', UserProfile.Role.MANAGER, 'MANAGEMENT', False, False),
            ('admin_user', UserProfile.Role.ADMIN, 'MANAGEMENT', True, True),
        ]

        for username, role, department_code, is_staff, is_superuser in demo_users:
            user, _ = User.objects.update_or_create(
                username=username,
                defaults={
                    'is_active': True,
                    'is_staff': is_staff,
                    'is_superuser': is_superuser,
                },
            )
            user.set_password(DEMO_PASSWORD)
            user.save()

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = role
            profile.department = department_objects[department_code]
            profile.is_active = True
            profile.save()

        self.stdout.write(
            self.style.SUCCESS(
                'Demo data ready: 4 departments, 5 users, password demo12345. '
                'admin_user is an ADMIN, staff, and superuser in Management.'
            )
        )
