"""Demo data for members_e2e.py. Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-10/seed_demo.py

Every account's password is its login. `admin1` is an administrator;
`ivanov` (ОТК, «Производство МП и РЛ») and `sidorova` (Отдел продаж) become
members of the board the browser creates; `petrova` (ПДО) is on «Чужая
доска» only; `loner` (ТО) is on no board. A draft protocol of `admin1` is
there for the participant picker.
"""
from django.contrib.auth.models import User

from accounts.models import Department, UserProfile
from boards.services import create_board, create_card
from django.utils import timezone
from datetime import timedelta
from protocols.models import QUALITY_PROTOCOL_TYPE_CODE, ProtocolType
from protocols.services import create_protocol


def person(username, first, last, role, department_code):
    user = User.objects.create_user(username=username, password=username, first_name=first, last_name=last)
    profile = user.userprofile
    profile.role = role
    profile.department = Department.objects.get(code=department_code)
    profile.save()
    return user


admin = person('admin1', 'Олег', 'Админов', UserProfile.Role.ADMIN, 'MANAGEMENT')
person('ivanov', 'Иван', 'Иванов', UserProfile.Role.OTK, 'PROD_MP_RL')
person('sidorova', 'Мария', 'Сидорова', UserProfile.Role.OPR, 'OPR')
petrova = person('petrova', 'Анна', 'Петрова', UserProfile.Role.PDO, 'PDO')
person('loner', 'Пётр', 'Одиночкин', UserProfile.Role.TO, 'PROD_PIR')
foreign = create_board(name='Чужая доска', owner=admin, actor=admin, member_ids=[petrova.pk])
create_card(
    foreign, actor=admin, title='Задача Петровой', due_date=timezone.localdate() + timedelta(days=3),
    assignee_ids=[petrova.pk], stage='TODO',
)
protocol = create_protocol(ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE), admin)
print('protocol', protocol.pk)
