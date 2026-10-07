"""Browser check of stage 22: links, column subscriptions, column sums.

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-22/stage22_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it (and restarts the server).

Two rounds, 1920×1080 and 1536×864, each:
1. `admin1` opens ZAP-1 and links it with «+ Связь»: «Ждёт» СНБ-1 (the board
   «Снабжение»); «Связи» on «Описание» lists it under «Ждёт» with its board,
   the tile says «⛔ ждёт СНБ-1», «Заблокированные» keeps only it;
2. `ivanov` (ZAP-1's исполнитель) has the board open; `worker1` completes
   СНБ-1 on his board — on `ivanov`'s page the badge disappears without a
   reload, and his bell says «Карточку ZAP-1 можно начинать: СНБ-1
   выполнена»;
3. `worker2` (a master, no manager) opens «⋯» of «В работе» — only «🔔
   Сообщать о новых карточках» — and subscribes; `admin1` moves ZAP-2 there
   with «Переместить в…»; `worker2`'s bell: «Карточка ZAP-2 вошла в колонку
   «В работе»…»;
4. `admin1` ticks «Сумма в колонке» for «Сумма» on «Поля карточек»: the
   column headers read «Σ Сумма: …» (В работе: 3 350 000, the closing column
   its completed cards), a filter narrows them, and «Таблица»
   ends with «Итого», the sum of its «Сумма» column.
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import subprocess
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
RESET = os.environ.get('RESET', '')
BOARD = '/work/boards/1/1/'
SNB_BOARD = '/work/boards/3/4/'
SIZES = ((1920, 1080), (1536, 864))
NBSP = ' '

results = []
problems = []


def on_dialog(username, dialog):
    problems.append(f'{username}: dialog {dialog.type}')
    dialog.dismiss()


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + str(detail)) if detail else ''}")


def login(browser, username, size):
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]})
    page = context.new_page()
    page.on('pageerror', lambda e: problems.append(f'{username}: {e}'))
    page.on('dialog', lambda d: on_dialog(username, d))
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def shot(page, name, size):
    page.screenshot(path=os.path.join(SHOTS, f'{name}-{size[0]}.png'))


def settle(page):
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)


def text(page, selector):
    locator = page.locator(selector)
    return ' '.join(locator.first.inner_text().split()) if locator.count() else ''


def no_sideways_scroll(page):
    return page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth + 1')


def bell(page, needle):
    page.reload()
    settle(page)
    page.locator('[data-notification-summary]').click()
    page.wait_for_timeout(300)
    return page.locator('.notification-menu__item', has_text=needle).all_inner_texts()


def number(text_value):
    cleaned = text_value.replace(NBSP, '').replace(' ', '').replace(',', '.').strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True, capture_output=True)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)
    worker1 = login(browser, 'worker1', size)
    worker2 = login(browser, 'worker2', size)

    # 1. ZAP-1 «ждёт» СНБ-1.
    admin.goto(BASE + BOARD + '?card=1')
    settle(admin)
    add = admin.locator('details.board-links__add')
    check('«+ Связь» on «Описание»', add.count() == 1)
    add.locator('summary').click()
    kinds = admin.locator('.board-links__form select[name=kind] option').all_inner_texts()
    check('the kinds: Ждёт, Блокирует, Связана с, Дублирует', kinds == ['Ждёт', 'Блокирует', 'Связана с', 'Дублирует'], kinds)
    admin.fill('.board-links__form input[name=code]', 'снб-1')
    with admin.expect_navigation():
        admin.locator('.board-links__form button[type=submit]').click()
    settle(admin)
    links = text(admin, '[data-live-board-links]')
    check('«Связи»: «Ждёт» СНБ-1, its title, status and board', 'Ждёт' in links and 'СНБ-1' in links
          and 'Закупить лист 2 мм' in links and 'В работе' in links and 'Снабжение' in links, links)
    badge = text(admin, '[data-card-id="1"] [data-tile-blocked]')
    check('the tile: «⛔ ждёт СНБ-1»', badge == '⛔ ждёт СНБ-1', badge)
    log = admin.locator('.board-drawer__tabs a', has_text='Лог')
    if log.count():
        log.first.click()
        admin.wait_for_timeout(300)
        check('«Лог»: «Связь: ждёт СНБ-1»', 'Связь: ждёт СНБ-1' in text(admin, '[data-live-board-log]'))
        admin.locator('.board-drawer__tabs a', has_text='Описание').first.click()
        admin.wait_for_timeout(300)
    check('no sideways scroll', no_sideways_scroll(admin))
    shot(admin, '1-link', size)
    admin.goto(BASE + BOARD + '?blocked=1')
    settle(admin)
    working = admin.locator('section.board-column:not(.board-column--done) [data-card-id]')
    check('«Заблокированные»: only ZAP-1 in the working columns', working.count() == 1
          and working.first.get_attribute('data-card-id') == '1')
    check('…the box ticked', admin.locator('input[name=blocked]').is_checked())
    shot(admin, '1-blocked-filter', size)

    # 2. СНБ-1 done on another board: the badge goes without a reload.
    ivanov.goto(BASE + BOARD)
    settle(ivanov)
    ivanov.evaluate('() => { window.__stage22 = 1; }')
    check('ivanov sees «⛔ ждёт СНБ-1» (he reads «Снабжение»)',
          text(ivanov, '[data-card-id="1"] [data-tile-blocked]') == '⛔ ждёт СНБ-1')
    worker1.goto(BASE + SNB_BOARD + '?card=12')
    settle(worker1)
    worker1.locator('[data-live-board-panel] details summary', has_text='Завершить').click()
    worker1.fill('#board-complete-result', 'Лист на складе.')
    with worker1.expect_navigation():
        worker1.locator('[data-live-board-panel] form button[type=submit]', has_text='Завершить').click()
    settle(worker1)
    try:
        ivanov.wait_for_selector('[data-card-id="1"] [data-tile-blocked]', state='detached', timeout=10000)
        gone = True
    except Exception:  # noqa: BLE001 - reported below
        gone = False
    check('the badge disappears on ivanov\'s page…', gone)
    check('…without a reload', ivanov.evaluate('() => window.__stage22') == 1)
    shot(ivanov, '2-unblocked-tile', size)
    entries = bell(ivanov, 'можно начинать')
    check('ivanov\'s bell: «Карточку ZAP-1 можно начинать: СНБ-1 выполнена»',
          any('Карточку ZAP-1 можно начинать: СНБ-1 выполнена' in entry for entry in entries), entries)
    shot(ivanov, '2-unblocked-bell', size)
    ivanov.keyboard.press('Escape')

    # 3. A master follows «В работе».
    worker2.goto(BASE + BOARD)
    settle(worker2)
    column = worker2.locator('section.board-column[data-column-id="2"]')
    column.locator('.board-menu__toggle').click()
    menu = column.locator('.board-menu__body')
    check('a member who manages nothing: «⋯» holds only the bell',
          menu.locator('form').count() == 1 and 'Сообщать о новых карточках' in menu.inner_text()
          and 'Переименовать' not in menu.inner_text())
    shot(worker2, '3-follow-menu', size)
    with worker2.expect_navigation():
        menu.locator('button', has_text='Сообщать о новых карточках').click()
    settle(worker2)
    check('the header shows «🔔»', column.locator('.board-column__followed').count() == 1)
    admin.goto(BASE + BOARD + '?card=2')
    settle(admin)
    admin.select_option('[data-live-board-facts] select[name=column_id]', '2')
    with admin.expect_navigation():
        admin.locator('[data-live-board-facts] button', has_text='Переместить').click()
    settle(admin)
    entries = bell(worker2, 'вошла в колонку')
    check('worker2\'s bell: «Карточка ZAP-2 вошла в колонку «В работе»…»',
          any('Карточка ZAP-2 вошла в колонку «В работе» на доске «Запуск заказов»' in entry for entry in entries), entries)
    check('…and nothing for admin1, who moved it', not bell(admin, 'вошла в колонку'))
    admin.keyboard.press('Escape')
    shot(worker2, '3-master-bell', size)
    worker2.keyboard.press('Escape')

    # 4. «Сумма в колонке».
    admin.goto(BASE + '/work/boards/1/fields/')
    settle(admin)
    field = admin.locator('#field-6')
    field.locator('details.board-field__edit summary').click()
    box = field.locator('input[name=sum_in_column]')
    check('«Сумма в колонке» offered for a number', box.count() == 1)
    box.check()
    with admin.expect_navigation():
        field.locator('form.board-field__form button[type=submit]').click()
    settle(admin)
    admin.goto(BASE + BOARD)
    settle(admin)
    sums = {
        column_id: text(admin, f'section.board-column[data-column-id="{column_id}"] [data-column-sums]')
        for column_id in ('1', '2', '3', '4')
    }
    # The seed's values: Сделать — ZAP-1 1 250 000, ZAP-7 75 000, ZAP-8 15 000;
    # В работе — ZAP-2 (just moved) 800 000, ZAP-3 2 150 000, ZAP-5 400 000;
    # На проверке — ZAP-4 75 000; Готово — the completed ZAP-6 990 000.
    check('every header sums the cards it shows', sums == {
        '1': 'Σ Сумма: 1 340 000', '2': 'Σ Сумма: 3 350 000', '3': 'Σ Сумма: 75 000', '4': 'Σ Сумма: 990 000',
    }, sums)
    check('no sideways scroll', no_sideways_scroll(admin))
    shot(admin, '4-sums', size)
    admin.goto(BASE + BOARD + '?q=заказ')
    settle(admin)
    filtered = text(admin, 'section.board-column[data-column-id="1"] [data-column-sums]')
    check('the filter narrows the sum', filtered != sums['1'], filtered)
    admin.goto(BASE + BOARD + '?view=table')
    settle(admin)
    headers = [' '.join(th.inner_text().replace('↓', '').replace('↑', '').split()) for th in admin.locator('.board-table thead th').all()]
    at = headers.index('Сумма') if 'Сумма' in headers else -1
    check('«Таблица»: «Ждёт» before the fields', 'Ждёт' in headers and headers.index('Ждёт') < at, headers)
    cells = [row.locator('td').nth(at).inner_text() for row in admin.locator('.board-table tbody tr').all()]
    total = admin.locator('.board-table tfoot tr td, .board-table tfoot tr th').nth(at).inner_text()
    check('«Итого» under «Сумма» is the sum of the column', admin.locator('.board-table tfoot').count() == 1
          and abs(number(total) - sum(number(cell) for cell in cells if cell.strip() != '—')) < 0.01, (total, cells))
    admin.locator('.board-table tfoot').scroll_into_view_if_needed()
    shot(admin, '4-table-total', size)
    for page in (admin, ivanov, worker1, worker2):
        page.context.close()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    for size in SIZES:
        run_round(browser, size)
    browser.close()

check('no JavaScript errors and no browser dialogs', not problems, problems)
failed = [name for name, ok in results if not ok]
print(f'\n{len(results) - len(failed)}/{len(results)} PASS')
sys.exit(len(failed))
