"""Browser check of stage 11: sub-boards and configurable columns.

Needs Redis and a real-time server on the data of `seed_demo.py` (the ASGI
wrapper that adds static files is stage 5's):

    redis-server --port 6379
    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
        python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-11/structure_e2e.py [screenshot-dir]

`admin1` builds the structure in one window; `ivanov`, a member, watches the
same sub-board in another and must see every change without reloading.
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import re
import sys
from datetime import date, timedelta

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

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


def column_names(page):
    return page.eval_on_selector_all(
        '[data-live-board-columns] [data-column-id] .board-column__header h2',
        'nodes => nodes.map(n => n.textContent.trim())',
    )


def tab_names(page):
    return page.eval_on_selector_all(
        '[data-live-board-columns] .board-tabs__link', 'nodes => nodes.map(n => n.textContent.trim())',
    )


def column(page, name):
    return page.locator('[data-live-board-columns] [data-column-id]').filter(
        has=page.locator(f'.board-column__header h2:text-is("{name}")'),
    )


def open_menu(page, summary):
    summary.click()
    page.wait_for_timeout(150)


def submit_in(menu_body, value):
    field = menu_body.locator('input[name=name]')
    field.fill(value)
    with menu_body.page.expect_navigation():
        menu_body.locator('button[type=submit]').first.click()


def centre(locator):
    box = locator.bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def wait_idle(page):
    page.wait_for_function('!document.querySelector("[data-board-busy]")', timeout=5000)


def wait_for(page, expression, timeout=8000):
    try:
        page.wait_for_function(expression, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 — a timeout is a failed check, reported below
        return False


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)

    # ------------------------------------------------------------------ 1
    admin = login(browser, 'admin1')
    admin.goto(BASE + '/work/boards/create/')
    admin.fill('input[name=name]', 'Запуск заказов')
    row = admin.locator('[data-employee-picker-row]').first
    employee = row.locator('[data-employee-select]')
    department_id = employee.locator('option:has-text("Иванов")').first.get_attribute('data-department-id')
    row.locator('[data-department-select]').select_option(value=department_id)
    employee.select_option(label=employee.locator('option:has-text("Иванов")').first.inner_text().strip())
    with admin.expect_navigation():
        admin.click('form.board-form button[type=submit]:not([name])')
    board_path = re.sub(r'\?.*$', '', admin.url.replace(BASE, ''))
    check('1 новая доска открывается на своей поддоске', re.fullmatch(r'/work/boards/\d+/\d+/', board_path),
          board_path)
    check('1 вкладка «Основная», активная', tab_names(admin) == ['Основная']
          and admin.locator('.board-tabs__item--active .board-tabs__link').inner_text().strip() == 'Основная')
    check('1 четыре колонки по умолчанию', column_names(admin) == ['Сделать', 'В работе', 'На проверке', 'Готово'],
          str(column_names(admin)))
    check('1 «Готово» — завершающая', column(admin, 'Готово').get_attribute('data-column-complete') is not None)
    admin.screenshot(path=os.path.join(SHOTS, 'new-board.png'))

    # The member opens the same sub-board and keeps it open.
    ivanov = login(browser, 'ivanov')
    ivanov.goto(BASE + board_path)
    ivanov.evaluate('window.__stillTheSamePage = true')
    check('5 участник видит доску без меню управления',
          ivanov.locator('[data-board-menu]').count() == 0 and column_names(ivanov)[0] == 'Сделать')

    # ------------------------------------------------------------------ 2
    open_menu(admin, admin.locator('.board-tabs__item--new summary'))
    submit_in(admin.locator('.board-tabs__item--new .board-menu__body'), 'Цех ПиР')
    check('2 вторая поддоска создана и открыта',
          tab_names(admin) == ['Основная', 'Цех ПиР']
          and admin.locator('.board-tabs__item--active .board-tabs__link').inner_text().strip() == 'Цех ПиР')
    check('2 у новой поддоски свои колонки по умолчанию',
          column_names(admin) == ['Сделать', 'В работе', 'На проверке', 'Готово'])
    admin.goto(BASE + board_path)

    review = column(admin, 'На проверке')
    open_menu(admin, review.locator('.board-menu summary'))
    submit_in(review.locator('.board-menu__body'), 'Проверка ОТК')
    check('2 колонка переименована', column_names(admin) == ['Сделать', 'В работе', 'Проверка ОТК', 'Готово'],
          str(column_names(admin)))

    open_menu(admin, admin.locator('.board-column-new summary'))
    submit_in(admin.locator('.board-column-new .board-menu__body'), 'Упаковка')
    check('2 колонка добавлена перед завершающей',
          column_names(admin) == ['Сделать', 'В работе', 'Проверка ОТК', 'Упаковка', 'Готово'],
          str(column_names(admin)))
    done_menu = column(admin, 'Готово').locator('.board-menu')
    open_menu(admin, done_menu.locator('summary'))
    check('2 у завершающей колонки — только «Переименовать»',
          done_menu.locator('input[name=name]').count() == 1
          and done_menu.locator('input[name=direction], [data-confirm]').count() == 0)
    admin.keyboard.press('Escape')
    open_menu(admin, column(admin, 'Упаковка').locator('.board-menu summary'))
    admin.screenshot(path=os.path.join(SHOTS, 'column-menu.png'))
    admin.keyboard.press('Escape')

    # ------------------------------------------------------------------ 5
    live = wait_for(
        ivanov,
        'window.__stillTheSamePage && [...document.querySelectorAll(".board-column__header h2")]'
        '.map(n => n.textContent.trim()).join("|") === "Сделать|В работе|Проверка ОТК|Упаковка|Готово"'
        ' && [...document.querySelectorAll(".board-tabs__link")].length === 2',
    )
    check('5 участник видит новую вкладку, переименование и колонку без перезагрузки', live,
          f'{tab_names(ivanov)} {column_names(ivanov)}')

    # ------------------------------------------------------------------ 3
    first_id = column(admin, 'Сделать').get_attribute('data-column-id')
    admin.goto(f'{BASE}{board_path}?new={first_id}')
    admin.fill('input[name=title]', 'Согласовать спецификацию')
    admin.fill('input[name=due_date]', (date.today() + timedelta(days=5)).isoformat())
    admin.locator('.board-card-form__assignees label:has-text("Иванов") input').check()
    with admin.expect_navigation():
        admin.click('.board-card-form button[type=submit]')
    admin.goto(BASE + board_path)
    tile = admin.locator('[data-card-movable]').first
    target = column(admin, 'Упаковка').locator('[data-column-list]')
    box = target.bounding_box()
    x, y = centre(tile)
    with admin.expect_response(lambda r: '/move/' in r.url) as moved:
        admin.mouse.move(x, y)
        admin.mouse.down()
        admin.mouse.move(x + 8, y + 8, steps=2)
        admin.mouse.move(box['x'] + box['width'] / 2, box['y'] + 40, steps=14)
        admin.mouse.up()
    answer = moved.value.json()
    wait_idle(admin)
    packing_id = column(admin, 'Упаковка').get_attribute('data-column-id')
    check('3 перенос в новую колонку принят сервером',
          answer.get('ok') and str(answer.get('column_id')) == packing_id
          and answer['counts'].get(packing_id) == 1, str(answer))
    check('3 плитка стоит в «Упаковке»',
          column(admin, 'Упаковка').locator('[data-card-movable]').count() == 1
          and column(admin, 'Сделать').locator('[data-card-movable]').count() == 0)
    admin.reload()
    check('3 после перезагрузки — там же', column(admin, 'Упаковка').locator('[data-card-movable]').count() == 1)
    check('5 участник видит перенос вживую', wait_for(
        ivanov,
        'window.__stillTheSamePage && [...document.querySelectorAll("[data-column-id]")]'
        '.some(c => c.querySelector("h2").textContent.trim() === "Упаковка"'
        ' && c.querySelector("[data-card-id]"))',
    ))
    admin.screenshot(path=os.path.join(SHOTS, 'card-in-new-column.png'))

    # ------------------------------------------------------------------ 4
    packing = column(admin, 'Упаковка')
    open_menu(admin, packing.locator('.board-menu summary'))
    packing.locator('.board-menu [data-confirm]').click()
    admin.wait_for_selector('[data-confirm-modal][open]')
    with admin.expect_navigation():
        admin.click('[data-confirm-modal-accept]')
    check('4 колонку с открытой карточкой удалить нельзя — причина на странице',
          'Упаковка' in column_names(admin) and 'открытые карточки: 1' in admin.content())

    in_progress = column(admin, 'В работе')
    open_menu(admin, in_progress.locator('.board-menu summary'))
    in_progress.locator('.board-menu [data-confirm]').click()
    admin.wait_for_selector('[data-confirm-modal][open]')
    check('4 удаление спрашивает подтверждение в общем окне',
          'Удалить колонку «В работе»?' in admin.locator('[data-confirm-modal-title]').inner_text())
    with admin.expect_navigation():
        admin.click('[data-confirm-modal-accept]')
    check('4 пустая колонка удалена', column_names(admin) == ['Сделать', 'Проверка ОТК', 'Упаковка', 'Готово'],
          str(column_names(admin)))
    check('5 участник видит удаление вживую', wait_for(
        ivanov,
        'window.__stillTheSamePage && [...document.querySelectorAll(".board-column__header h2")]'
        '.map(n => n.textContent.trim()).join("|") === "Сделать|Проверка ОТК|Упаковка|Готово"',
    ), str(column_names(ivanov)))
    ivanov.screenshot(path=os.path.join(SHOTS, 'member-live.png'))

    # The member drags into a column the owner has just deleted: his page
    # still draws it (its refresh is held back, as under a gesture), the
    # server refuses the move and the tile goes back where it was.
    ivanov.evaluate("document.querySelector('[data-board]').setAttribute('data-board-busy', '')")
    doomed = column(admin, 'Проверка ОТК')
    open_menu(admin, doomed.locator('.board-menu summary'))
    doomed.locator('.board-menu [data-confirm]').click()
    admin.wait_for_selector('[data-confirm-modal][open]')
    with admin.expect_navigation():
        admin.click('[data-confirm-modal-accept]')
    ivanov.wait_for_timeout(1500)
    ivanov.evaluate("document.querySelector('[data-board]').removeAttribute('data-board-busy')")
    check('4 у участника удалённая колонка ещё нарисована (обновление отложено)',
          'Проверка ОТК' in column_names(ivanov))
    tile = ivanov.locator('[data-card-movable]').first
    target = column(ivanov, 'Проверка ОТК').locator('[data-column-list]').bounding_box()
    x, y = centre(tile)
    with ivanov.expect_response(lambda r: '/move/' in r.url) as refused:
        ivanov.mouse.move(x, y)
        ivanov.mouse.down()
        ivanov.mouse.move(x + 8, y + 8, steps=2)
        ivanov.mouse.move(target['x'] + target['width'] / 2, target['y'] + 40, steps=14)
        ivanov.mouse.up()
    answer = refused.value
    wait_idle(ivanov)
    ivanov.wait_for_timeout(300)
    message = ivanov.locator('[data-board-message]')
    check('4 перенос в удалённую колонку отклонён сервером',
          answer.status == 400 and 'возможно, её удалили' in answer.json().get('error', ''),
          f"{answer.status} {answer.json().get('error', '')}")
    check('4 плитка вернулась, причина показана',
          not message.is_hidden() and 'возможно, её удалили' in message.inner_text())
    check('4 после отказа колонка исчезла и у участника', wait_for(
        ivanov,
        '[...document.querySelectorAll(".board-column__header h2")]'
        '.map(n => n.textContent.trim()).join("|") === "Сделать|Упаковка|Готово"'
        ' && document.querySelectorAll("[data-card-movable]").length === 1',
    ), str(column_names(ivanov)))

    check('нет ошибок JavaScript и браузерных диалогов', not console_errors, '; '.join(console_errors))
    browser.close()

failed = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failed} of {len(results)} checks passed')
sys.exit(failed)
