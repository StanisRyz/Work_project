"""Browser check of board access: administrators only, for now.

Run a server on the data of `seed_demo.py` (the ASGI wrapper that adds static
files is stage 5's; real-time is not needed):

    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-09/access_e2e.py [screenshot-dir]

Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

results = []
console_errors = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def login(context, username):
    page = context.new_page()
    page.on('pageerror', lambda e: console_errors.append(f'{username}: {e}'))
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def tile(page, title):
    return page.locator(f'li.board-column__item:has(.board-tile__title:text-is("{title}"))')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    one = browser.new_context(viewport={'width': 1920, 'height': 1080})
    two = browser.new_context(viewport={'width': 1920, 'height': 1080})

    # ------------------------------------------------------------------ 1
    admin = login(one, 'admin1')
    admin.goto(BASE + '/')
    check('1 у администратора «Доски» в меню', admin.locator('.sidebar a:has-text("Доски")').count() == 1)
    check('1 и карточка на главной', admin.locator('main a[href="/work/boards/"]').count() == 1)
    admin.click('[data-sidebar-toggle]')
    admin.wait_for_selector('.sidebar a:has-text("Доски")', state='visible')
    admin.wait_for_timeout(600)
    admin.screenshot(path=os.path.join(SHOTS, 'admin-menu.png'))
    with admin.expect_navigation():
        admin.click('.sidebar a:has-text("Доски")')
    check('1 реестр досок открывается', 'Пилот администраторов' in admin.content())
    admin.goto(BASE + '/work/boards/1/')
    source = tile(admin, 'Настроить доступы')
    target = admin.locator('[data-column="IN_PROGRESS"] [data-column-list]')
    box_from, box_to = source.bounding_box(), target.bounding_box()
    with admin.expect_response(lambda r: '/move/' in r.url) as moved:
        admin.mouse.move(box_from['x'] + 40, box_from['y'] + 20)
        admin.mouse.down()
        admin.mouse.move(box_from['x'] + 60, box_from['y'] + 40, steps=5)
        admin.mouse.move(box_to['x'] + 60, box_to['y'] + box_to['height'] - 30, steps=15)
        admin.mouse.up()
    check('1 перетаскивание работает (200)', moved.value.status == 200, str(moved.value.status))
    admin.reload()
    in_progress = admin.locator('[data-column="IN_PROGRESS"]').inner_text()
    check('1 карточка в «В работе» после перезагрузки', 'Настроить доступы' in in_progress)
    admin.screenshot(path=os.path.join(SHOTS, 'admin-board.png'))

    # ------------------------------------------------------------------ 2
    pdo = login(two, 'petrova')
    pdo.goto(BASE + '/')
    check('2 у ПДО нет «Доски» в меню', pdo.locator('.sidebar a:has-text("Доски")').count() == 0)
    check('2 и нет карточки на главной', pdo.locator('a[href="/work/boards/"]').count() == 0)
    check('2 на главной нет задачи с доски', 'Задача ПДО с доски' not in pdo.content())
    pdo.click('[data-sidebar-toggle]')
    pdo.wait_for_selector('.sidebar a:has-text("Документация")', state='visible')
    pdo.wait_for_timeout(600)
    pdo.screenshot(path=os.path.join(SHOTS, 'pdo-menu.png'))
    for path in ('/work/boards/', '/work/boards/2/', '/work/boards/1/'):
        response = pdo.goto(BASE + path)
        check(f'2 {path} — 403', response.status == 403, str(response.status))
    for tab in ('my', 'all'):
        pdo.goto(f'{BASE}/quality/tasks/?tab={tab}')
        check(f'2 в «Задачах» ({tab}) нет задачи с доски', 'Задача ПДО с доски' not in pdo.content())
    check('2 в фильтре «Тип задачи» нет «Доска»', pdo.locator('select[name=source_type] option[value=BOARD]').count() == 0)
    pdo.screenshot(path=os.path.join(SHOTS, 'pdo-tasks.png'))
    pdo.goto(BASE + '/search/?q=' + 'Задача ПДО')
    check('2 поиск не находит задачу с доски', 'Задача ПДО с доски' not in pdo.content())

    one.close()
    two.close()
    browser.close()

check('ошибок JavaScript нет', not console_errors, '; '.join(console_errors))
failures = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failures} из {len(results)} проверок прошли')
sys.exit(failures)
