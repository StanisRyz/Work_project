"""Demo data for drawer_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-13/seed_demo.py

Every account's password is its login. `admin1` (администратор) owns two
boards; `ivanov` (ОТК) is a member of «Запуск заказов» only; `loner` (ТО) is on
no board. «Запуск заказов» has a second sub-board and a few cards, one of them
with a long title and a description, and a short «Чат» on the first card, for
the card drawer.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile
from boards.services import create_board, create_card, create_sub_board, post_card_comment


def person(username, first, last, role, department_code):
    user = User.objects.create_user(username=username, password=username, first_name=first, last_name=last)
    profile = user.userprofile
    profile.role = role
    profile.department = Department.objects.get(code=department_code)
    profile.save()
    return user


admin = person('admin1', 'Олег', 'Админов', UserProfile.Role.ADMIN, 'MANAGEMENT')
ivanov = person('ivanov', 'Иван', 'Иванов', UserProfile.Role.OTK, 'PROD_MP_RL')
extra = [
    person(f'worker{index}', name, 'Сотрудников', UserProfile.Role.OTK, 'PROD_MP_RL')
    for index, name in enumerate(['Анна', 'Борис', 'Вера', 'Глеб', 'Дина', 'Егор'], start=1)
]
person('loner', 'Пётр', 'Одиночкин', UserProfile.Role.TO, 'PROD_PIR')

launch = create_board(
    name='Запуск заказов', owner=admin, actor=admin, member_ids=[ivanov.pk] + [u.pk for u in extra],
)
create_board(name='Отгрузки', owner=admin, actor=admin)
main = launch.sub_boards.get()
create_sub_board(launch, actor=admin, name='Цех ПиР')
columns = list(main.columns.order_by('position'))
due = timezone.localdate() + timedelta(days=4)
cards = []
for title, column in (
    ('Согласовать спецификацию', 0),
    ('Утвердить технологическую карту на изделие 9АВ.616.001-365 и передать в ПДО', 0),
    ('Запустить партию в цех МП', 1),
    ('Проверить комплектацию', 2),
):
    cards.append(create_card(
        main, actor=admin, title=title, due_date=due, assignee_ids=[ivanov.pk], column=columns[column],
        description='Подробности: согласовать с конструктором, приложить извещение и сверить с заказом покупателя.',
    ))
post_card_comment(cards[0], actor=ivanov, text='Нет папки с проработкой, хотя продавали в июне.')
post_card_comment(cards[0], actor=admin, text='Закинул последнюю проработку М2519.')
print('seeded')
