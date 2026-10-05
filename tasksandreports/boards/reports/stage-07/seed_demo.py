"""Demo data for discussion_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-07/seed_demo.py

Every account's password is its login. `petrova` owns the board; `ivanov`
and `sidorova` work on it.
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
today = timezone.localdate()
board = create_board(
    name='Планирование производства', department=pdo, owner=owner, actor=owner,
    member_ids=[ivanov.pk, sidorova.pk],
)
for title, stage, assignees, days in (
    ('Согласовать график', 'IN_PROGRESS', [ivanov], 3),
    ('Закупка оснастки', 'TODO', [sidorova], 5),
):
    create_card(
        board, actor=owner, title=title, due_date=today + timedelta(days=days),
        assignee_ids=[user.pk for user in assignees], stage=stage,
    )
# Сидорова's own card on Иванов — she may cancel it.
create_card(
    board, actor=sidorova, title='Лишняя карточка', due_date=today + timedelta(days=4),
    assignee_ids=[ivanov.pk], stage='TODO',
)
print('board', board.pk)
