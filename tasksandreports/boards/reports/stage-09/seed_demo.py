"""Demo data for access_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-09/seed_demo.py

Every account's password is its login. `admin1` and `admin2` are
administrators; `petrova` is ПДО. Board 1 is the administrators'. Board 2 was
written by ПДО under a widened admission — the way a board from before the
admission narrowed exists — and carries a task on ПДО.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile
from boards.services import create_board, create_card

pdo_department = Department.objects.get(code='PDO')


def person(username, first, last, role):
    user = User.objects.create_user(username=username, password=username, first_name=first, last_name=last)
    profile = user.userprofile
    profile.role = role
    profile.department = pdo_department
    profile.save()
    return user


admin1 = person('admin1', 'Олег', 'Админов', UserProfile.Role.ADMIN)
admin2 = person('admin2', 'Нина', 'Смирнова', UserProfile.Role.ADMIN)
petrova = person('petrova', 'Анна', 'Петрова', UserProfile.Role.PDO)
today = timezone.localdate()
board = create_board(
    name='Пилот администраторов', department=pdo_department, owner=admin1, actor=admin1,
    member_ids=[admin2.pk],
)
for title, stage, user in (('Настроить доступы', 'TODO', admin2), ('Проверить отчёты', 'IN_PROGRESS', admin1)):
    create_card(board, actor=admin1, title=title, due_date=today + timedelta(days=3),
                assignee_ids=[user.pk], stage=stage)
with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset(UserProfile.Role.values)):
    old = create_board(name='Старая доска ПДО', department=pdo_department, owner=petrova, actor=petrova)
    create_card(old, actor=petrova, title='Задача ПДО с доски', due_date=today + timedelta(days=2),
                assignee_ids=[petrova.pk], stage='TODO')
print('boards', board.pk, old.pk)
