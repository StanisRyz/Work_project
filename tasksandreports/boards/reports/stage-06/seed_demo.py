"""Demo data for lifecycle_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-06/seed_demo.py

Every account's password is its login. Board 1 is in use; board 2 has only
finished work, so it can go to the archive.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile
from boards.services import complete_card, create_board, create_card

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
today = timezone.localdate()

board = create_board(
    name='Планирование производства', department=pdo, owner=owner, actor=owner,
    member_ids=[ivanov.pk, sidorova.pk],
)
for title, stage, assignees, days in (
    ('Альфа', 'TODO', [ivanov], 3),
    ('Бета', 'TODO', [sidorova], 4),
    ('Гамма', 'TODO', [ivanov], 5),
    ('Зета', 'TODO', [ivanov], 6),
    ('Дельта просрочена', 'IN_PROGRESS', [ivanov], -2),
    ('Правка вдвоём', 'IN_PROGRESS', [ivanov], 2),
    ('Эпсилон', 'REVIEW', [sidorova], 1),
    ('Перенос открытой', 'TODO', [ivanov], 7),
):
    create_card(
        board, actor=owner, title=title, due_date=today + timedelta(days=days),
        assignee_ids=[user.pk for user in assignees], stage=stage,
    )

old = create_board(
    name='Старая доска', department=pdo, owner=owner, actor=owner, member_ids=[ivanov.pk],
)
done = create_card(old, actor=owner, title='Сделано давно', due_date=today, assignee_ids=[ivanov.pk])
complete_card(done, actor=ivanov, execution_comment='Готово')
print('boards', board.pk, old.pk)
