"""Demo data for polish_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-08/seed_demo.py

Every account's password is its login. `petrova` owns the board; `ivanov`
and `sidorova` work on it. «Долгое обсуждение» carries 105 messages.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile
from boards.services import create_board, create_card, post_card_comment

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
create_card(
    board, actor=owner, title='Согласовать график', due_date=today + timedelta(days=3),
    assignee_ids=[ivanov.pk], stage='IN_PROGRESS',
)
long = create_card(
    board, actor=owner, title='Долгое обсуждение', due_date=today + timedelta(days=5),
    assignee_ids=[ivanov.pk], stage='TODO',
)
for index in range(105):
    post_card_comment(long, actor=sidorova if index % 2 else owner, text=f'Сообщение {index + 1:03}')
print('board', board.pk)
