"""Demo data for dnd_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-04/seed_demo.py

Every account's password is its login.
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
person('reader', 'Олег', 'Читатель', UserProfile.Role.TO)
board = create_board(
    name='Планирование производства', department=pdo, owner=owner, actor=owner,
    member_ids=[ivanov.pk, sidorova.pk],
)
today = timezone.localdate()
for title, stage, assignees, days in (
    ('Альфа', 'TODO', [ivanov], 3),
    ('Бета', 'TODO', [ivanov, sidorova], 4),
    ('Гамма', 'TODO', [sidorova], 5),
    ('Дельта', 'IN_PROGRESS', [ivanov], 2),
    ('Эпсилон', 'REVIEW', [ivanov], 1),
    ('Закрыть в другой вкладке', 'IN_PROGRESS', [ivanov], 6),
):
    create_card(
        board, actor=owner, title=title, due_date=today + timedelta(days=days),
        assignee_ids=[user.pk for user in assignees], stage=stage,
    )
# A long column, so a short vertical swipe has something to scroll.
for index in range(12):
    create_card(
        board, actor=owner, title=f'Заявка {index + 1}', due_date=today + timedelta(days=7),
        assignee_ids=[sidorova.pk], stage='REVIEW',
    )
print('board', board.pk)
