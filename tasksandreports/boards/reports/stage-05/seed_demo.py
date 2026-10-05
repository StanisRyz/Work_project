"""Demo data for live_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-05/seed_demo.py

Every account's password is its login. Two members work on one board side by
side (`ivanov`, `sidorova`); `petrova` owns it.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile
from boards.services import create_board, create_card

pdo = Department.objects.get(code='PDO')


def person(username, first, last, role):
    user = User.objects.create_user(username=username, password=username, first_name=first, last_name=last)
    profile = user.userprofile
    profile.role = role
    profile.department = pdo
    profile.save()
    return user


owner = person('petrova', 'Анна', 'Петрова', UserProfile.Role.PDO)
ivanov = person('ivanov', 'Иван', 'Иванов', UserProfile.Role.OTK)
sidorova = person('sidorova', 'Мария', 'Сидорова', UserProfile.Role.OPR)
board = create_board(
    name='Планирование производства', department=pdo, owner=owner, actor=owner,
    member_ids=[ivanov.pk, sidorova.pk],
)
today = timezone.localdate()
for title, stage, assignees, days in (
    ('Альфа', 'TODO', [ivanov], 3),
    ('Бета', 'TODO', [sidorova], 4),
    ('Гамма', 'IN_PROGRESS', [sidorova], 5),
    ('Дельта', 'IN_PROGRESS', [ivanov], 2),
    ('Эпсилон', 'REVIEW', [ivanov], 1),
    ('Перенос открытой', 'TODO', [ivanov], 6),
):
    create_card(
        board, actor=owner, title=title, due_date=today + timedelta(days=days),
        assignee_ids=[user.pk for user in assignees], stage=stage,
    )
print('board', board.pk)
