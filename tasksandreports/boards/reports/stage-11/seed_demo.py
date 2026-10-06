"""Demo data for structure_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-11/seed_demo.py

Every account's password is its login. `admin1` is an administrator and
creates the board in the browser; `ivanov` (ОТК) is put on it as a member and
watches the second window.
"""
from django.contrib.auth.models import User

from accounts.models import Department, UserProfile


def person(username, first, last, role, department_code):
    user = User.objects.create_user(username=username, password=username, first_name=first, last_name=last)
    profile = user.userprofile
    profile.role = role
    profile.department = Department.objects.get(code=department_code)
    profile.save()
    return user


person('admin1', 'Олег', 'Админов', UserProfile.Role.ADMIN, 'MANAGEMENT')
person('ivanov', 'Иван', 'Иванов', UserProfile.Role.OTK, 'PROD_MP_RL')
print('seeded')
