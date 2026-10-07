"""Demo data for stage20_e2e.py: stage 19's demo, unchanged.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-20/seed_demo.py

Stage 19's seed (`../stage-19/seed_demo.py`, which runs 18's, 17's, 16's and
15's first) is the whole of it: «Запуск заказов» ZAP, `admin1` — Олег Админов
(the owner and the author of ZAP-1), `ivanov` — Иван Иванов (the исполнитель
of ZAP-1), `petrova` — Мария Петрова, …, passwords = logins; ZAP-1
«Согласовать спецификацию» with its checklist «Запуск заказа» (five steps,
two ticked). No card has subtasks yet: the browser check adds every one
through the page — three «списком» and one «В подзадачу» from the checklist.
"""
from pathlib import Path

exec(Path('tasksandreports/boards/reports/stage-19/seed_demo.py').read_text())  # noqa: S102
print('seeded stage 20')
