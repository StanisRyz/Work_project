"""Browser check of stage 10: board members by «подразделение → сотрудник».

Run a server on the data of `seed_demo.py` (the ASGI wrapper that adds static
files is stage 5's; real-time is not needed):

    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-10/members_e2e.py [screenshot-dir]

Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import sys
from datetime import date, timedelta

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
PROTOCOL = os.environ.get('PROTOCOL', '1')

results = []
console_errors = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def login(browser, username):
    context = browser.new_context(viewport={'width': 1920, 'height': 1080})
    page = context.new_page()
    page.on('pageerror', lambda e: console_errors.append(f'{username}: {e}'))
    page.on('dialog', lambda d: (console_errors.append(f'{username}: dialog {d.type}'), d.accept()))
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def option_value(select, text):
    return select.locator(f'option:has-text("{text}")').first.get_attribute('value')


def visible_options(select):
    return select.evaluate("s => [...s.options].filter(o => o.value && !o.hidden).map(o => o.textContent.trim())")


def pick(row, department, employee):
    department_select = row.locator('[data-department-select]')
    department_select.select_option(label=department)
    employee_select = row.locator('[data-employee-select]')
    names = visible_options(employee_select)
    employee_select.select_option(value=option_value(employee_select, employee))
    return names


def menu_has_boards(page):
    page.click('[data-sidebar-toggle]')
    page.wait_for_selector('.sidebar a:has-text("Документация")', state='visible')
    page.wait_for_timeout(500)
    found = page.locator('.sidebar a:has-text("Доски")').count() == 1
    return found


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)

    # ------------------------------------------------------------------ 1
    admin = login(browser, 'admin1')
    admin.goto(BASE + '/work/boards/create/')
    form = admin.locator('form.board-form')
    check('1 в форме только «Название» и «Участники»',
          form.locator('input[name=name]').count() == 1
          and form.locator('[name=description], [name=department]').count() == 0)
    admin.fill('input[name=name]', 'Пилот: ОТК и продажи')
    rows = admin.locator('[data-employee-picker-row]')
    names = pick(rows.nth(0), 'Производство МП и РЛ', 'Иванов')
    check('1 подразделение сужает список сотрудников', names == ['Иван Иванов'], str(names))
    admin.click('[data-employee-picker-add]')
    check('1 «+ Добавить участника» добавил строку без перезагрузки', rows.count() == 2)
    names = pick(rows.nth(1), 'Отдел продаж', 'Сидорова')
    check('1 вторая строка — другой отдел', names == ['Мария Сидорова'], str(names))
    own = rows.nth(0).locator('[data-employee-select] option:has-text("Админов")')
    admin.screenshot(path=os.path.join(SHOTS, 'new-board-form.png'))
    with admin.expect_navigation():
        admin.click('form.board-form button[type=submit]:has-text("Создать доску")')
    check('1 доска создана', '/work/boards/' in admin.url and 'create' not in admin.url, admin.url)
    board_url = admin.url.split('?')[0]
    admin.goto(board_url + 'members/')
    members = admin.locator('.board-members-table tbody').inner_text()
    check('1 участники: админ (владелец), Иванов, Сидорова',
          all(name in members for name in ('Админов', 'Иванов', 'Сидорова')), members.replace('\n', ' | '))
    admin.goto(board_url + '?new=TODO')
    admin.fill('input[name=title]', 'Проверить партию 17')
    admin.fill('input[name=due_date]', (date.today() + timedelta(days=4)).isoformat())
    admin.locator('label:has(input[name=assignees]):has-text("Иванов") input').check()
    with admin.expect_navigation():
        admin.locator('form:has(input[name=title]) button[type=submit]').first.click()
    check('1 карточка на Иванова создана', 'Проверить партию 17' in admin.content())
    admin.screenshot(path=os.path.join(SHOTS, 'admin-board.png'))

    # ------------------------------------------------------------------ 2
    ivanov = login(browser, 'ivanov')
    ivanov.goto(BASE + '/')
    check('2 у участника-ОТК «Доски» в меню', menu_has_boards(ivanov))
    ivanov.screenshot(path=os.path.join(SHOTS, 'member-menu.png'))
    ivanov.goto(BASE + '/work/boards/?tab=all')
    content = ivanov.locator('main').inner_text()
    check('2 видна только его доска', 'Пилот: ОТК и продажи' in content and 'Чужая доска' not in content)
    response = ivanov.goto(BASE + '/work/boards/1/')
    check('2 чужая доска — 403', response.status == 403, str(response.status))
    ivanov.goto(BASE + '/quality/tasks/?tab=my')
    content = ivanov.locator('main').inner_text()
    check('2 задача карточки в «Моих задачах»', 'Проверить партию 17' in content)
    check('2 задачи чужой доски нет', 'Задача Петровой' not in content)
    ivanov.screenshot(path=os.path.join(SHOTS, 'member-my-tasks.png'))

    # ------------------------------------------------------------------ 3
    loner = login(browser, 'loner')
    loner.goto(BASE + '/')
    check('3 у сотрудника без досок пункта «Доски» нет', not menu_has_boards(loner))
    loner.screenshot(path=os.path.join(SHOTS, 'loner-menu.png'))
    response = loner.goto(BASE + '/work/boards/')
    check('3 /work/boards/ — 403', response.status == 403, str(response.status))

    # ------------------------------------------------------------------ 4
    admin.goto(f'{BASE}/quality/protocols/{PROTOCOL}/')
    participants = admin.locator('[data-block="participants"]')
    participants.locator('[data-add-row]').click()
    row = participants.locator('[data-row]').last
    names = pick(row, 'Отдел продаж', 'Сидорова')
    check('4 протокол: подразделение сужает список', names == ['Мария Сидорова'], str(names))
    check('4 протокол: выбор сделан', row.locator('[data-employee-select]').input_value() != '')
    # The protocol's own rule: the participant chosen is hidden in a new row.
    participants.locator('[data-add-row]').click()
    second = participants.locator('[data-row]').last
    second.locator('[data-department-select]').select_option(label='Отдел продаж')
    hidden = visible_options(second.locator('[data-employee-select]'))
    check('4 протокол: уже выбранный участник скрыт в новой строке', 'Мария Сидорова' not in hidden, str(hidden))
    second.locator('[data-remove-row]').click()
    admin.locator('[data-protocol-editor] [data-department-select]').first.scroll_into_view_if_needed()
    admin.screenshot(path=os.path.join(SHOTS, 'protocol-participant.png'))
    # The draft's own required fields (the empty «Слушали» row needs a
    # speaker and a text) — the editor's rules, untouched by this stage.
    admin.evaluate("""() => {
        const form = document.querySelector('[data-protocol-editor]');
        [...form.elements].filter((e) => e.willValidate && !e.checkValidity()).forEach((e) => {
            if (e.tagName === 'SELECT') {
                const option = [...e.options].find((o) => o.value && !o.disabled);
                if (option) e.value = option.value;
            } else {
                e.value = 'Обсудили состав участников';
            }
        });
    }""")
    with admin.expect_navigation():
        admin.click('button:has-text("Сохранить черновик")')
    saved = admin.evaluate("""() => [...document.querySelectorAll('[data-participant-user]')]
        .filter((s) => s.value).map((s) => s.options[s.selectedIndex].textContent.trim())""")
    check('4 протокол: участник сохранён', saved == ['Мария Сидорова'], str(saved))

    browser.close()

check('ошибок JavaScript и браузерных диалогов нет', not console_errors, '; '.join(console_errors))
failures = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failures} из {len(results)} проверок прошли')
sys.exit(failures)
