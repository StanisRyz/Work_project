"""Demo data for stage14_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-14/seed_demo.py

Every account's password is its login. `admin1` (администратор) owns
«Запуск заказов» (code ZAP) with a second sub-board «Цех ПиР»; `ivanov` (ОТК)
and `petrova` (ПДО) are members; `loner` (ТО) owns nothing and is on no board.
A second board «Отгрузки» (OTG) is the admin's alone, for «чужая доска».
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile
from boards.services import create_board, create_card, create_sub_board


def person(username, first, last, role, department_code):
    user = User.objects.create_user(username=username, password=username, first_name=first, last_name=last)
    profile = user.userprofile
    profile.role = role
    profile.department = Department.objects.get(code=department_code)
    profile.save()
    return user


admin = person('admin1', 'Олег', 'Админов', UserProfile.Role.ADMIN, 'MANAGEMENT')
ivanov = person('ivanov', 'Иван', 'Иванов', UserProfile.Role.OTK, 'PROD_MP_RL')
petrova = person('petrova', 'Мария', 'Петрова', UserProfile.Role.PDO, 'PDO')
extra = [
    person(f'worker{index}', name, 'Сотрудников', UserProfile.Role.OTK, 'PROD_MP_RL')
    for index, name in enumerate(['Анна', 'Борис', 'Вера', 'Глеб'], start=1)
]
person('loner', 'Пётр', 'Одиночкин', UserProfile.Role.TO, 'PROD_PIR')

launch = create_board(
    name='Запуск заказов', code='zap', owner=admin, actor=admin,
    member_ids=[ivanov.pk, petrova.pk] + [u.pk for u in extra],
)
other = create_board(name='Отгрузки', code='OTG', owner=admin, actor=admin)
main = launch.sub_boards.get()
create_sub_board(launch, actor=admin, name='Цех ПиР')
columns = list(main.columns.order_by('position'))
due = timezone.localdate() + timedelta(days=4)
for title, column in (
    ('Согласовать спецификацию', 0),
    ('Утвердить технологическую карту на изделие 9АВ.616.001-365 и передать в ПДО', 0),
    ('Запустить партию в цех МП', 1),
    ('Проверить комплектацию', 2),
):
    create_card(
        main, actor=admin, title=title, due_date=due, assignee_ids=[ivanov.pk], column=columns[column],
        description='Подробности: согласовать с конструктором, приложить извещение и сверить с заказом покупателя.',
    )
create_card(
    other.sub_boards.get(), actor=admin, title='Отгрузка по заказу 77', due_date=due, assignee_ids=[admin.pk],
)
print('seeded')
