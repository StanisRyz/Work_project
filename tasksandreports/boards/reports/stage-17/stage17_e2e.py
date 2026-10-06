"""Browser check of stage 17: time in the column, «Застой», «Таблица» and its Excel.

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-17/stage17_e2e.py [screenshot-dir]

Two rounds, 1920×1080 and 1536×864 (125 % Windows scaling of the same
monitor), each:
1. the tiles say «в колонке N дн.»; «Застой» of «Сделать» set to 3 days in
   the column's «⋯» menu; the header says «⏱ 3 дн.» and the tiles that have
   stood there 3 days or more are highlighted; «Застрявшие» keeps exactly them;
2. «Таблица» of «Основная» with the board's fields; sorted by «Приоритет» and
   by «Срок» both ways; «Поля» → «Приоритет: Высокий»;
3. «Все поддоски» (with «Отменённые»): the «Поддоска» column and the card of
   «Цех ПиР»;
4. «Excel» downloaded, opened with openpyxl and compared with the table on
   the screen, cell by cell for the code, the title, the column and the days.
Each check prints PASS/FAIL; the exit code is the number of failures. The
first rows of the downloaded workbook are printed for the report.
"""

import os
import re
import sys
import tempfile

from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/1/'
COLUMNS = ('Сделать', 'В работе', 'На проверке', 'Готово')
SIZES = ((1920, 1080), (1536, 864))
# The «Срок» header's link, its arrow included once it sorts.
DUE = re.compile(r'^Срок(\s*[↑↓])?$')

results = []
problems = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + str(detail)) if detail else ''}")


def login(browser, username, size):
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]}, accept_downloads=True)
    page = context.new_page()
    page.on('pageerror', lambda e: problems.append(f'{username}: {e}'))
    page.on('dialog', lambda d: (problems.append(f'{username}: dialog {d.type}'), d.dismiss()))
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def shot(page, name, size):
    page.screenshot(path=os.path.join(SHOTS, f'{name}-{size[0]}.png'))


def tiles(page):
    """`{code: (age line, stale?)}` of every tile on the board."""
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.board-tile')].map((tile) => [
        tile.querySelector('.board-tile__number').textContent.trim(),
        [(tile.querySelector('.board-tile__age') || {textContent: ''}).textContent.trim(),
         tile.classList.contains('board-tile--stale')],
    ]))""")


def board_state(page):
    """`{column: (the count in its header, [the codes of its tiles])}`."""
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.board-column')].map((column) => [
        column.querySelector('h2').textContent.trim(),
        [Number(column.querySelector('[data-column-count]').textContent.trim()),
         [...column.querySelectorAll('.board-tile__number')].map((n) => n.textContent.trim())],
    ]))""")


def table(page):
    """The table on the screen: the headers and, per row, the cells' text."""
    return page.evaluate("""() => ({
        headers: [...document.querySelectorAll('.board-table thead th')].map((th) => th.textContent.replace(/[↑↓]/g, '').trim()),
        rows: [...document.querySelectorAll('.board-table tbody tr')].map((tr) =>
            [...tr.children].map((td) => td.textContent.replace(/\\s+/g, ' ').trim())),
    })""")


def codes(page):
    return [row[0] for row in table(page)['rows']]


def column_of(page, name):
    return page.locator('.board-column', has=page.locator('h2', has_text=re.compile(f'^{name}$')))


def single_line(page):
    box = page.locator('.board-filters').bounding_box()
    wide = page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
    return box['height'] < 48 and not wide, f"height {box['height']:.0f}, sideways scroll {wide}"


def click_and_wait(page, locator, url_part=None):
    with page.expect_navigation():
        locator.click()
    page.wait_for_load_state('networkidle')
    if url_part:
        check(f'address has {url_part}', url_part in page.url, page.url)


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    page = login(browser, 'admin1', size)
    page.goto(BASE + BOARD)
    page.wait_for_load_state('networkidle')

    # 1. Time in the column, «Застой», «Застрявшие».
    state = tiles(page)
    check('ZAP-1 «в колонке 5 дн.»', state['ZAP-1'][0] == 'в колонке 5 дн.', state['ZAP-1'])
    check('ZAP-7 «в колонке сегодня»', state['ZAP-7'][0] == 'в колонке сегодня', state['ZAP-7'])
    check('ZAP-4 «в колонке 12 дн.»', state['ZAP-4'][0] == 'в колонке 12 дн.', state['ZAP-4'])
    check('completed ZAP-6 says nothing', state['ZAP-6'][0] == '', state['ZAP-6'])
    todo = column_of(page, 'Сделать')
    todo.locator('summary.board-menu__toggle').click()
    days = todo.locator('input[name=days]')
    days.fill('3')
    shot(page, '1-stale-menu', size)
    click_and_wait(page, todo.locator('form.board-stale button[type=submit]'))
    todo = column_of(page, 'Сделать')
    header = todo.locator('.board-column__stale')
    check('«Сделать» header «⏱ 3 дн.»', header.count() == 1 and header.inner_text().strip() == '⏱ 3 дн.')
    check('other headers have no mark', page.locator('.board-column__stale').count() == 1)
    state = tiles(page)
    stale = sorted(code for code, (_, flag) in state.items() if flag)
    check('stale tiles: ZAP-1 (5 дн.), ZAP-2 (9 дн.)', stale == ['ZAP-1', 'ZAP-2'], stale)
    check('ZAP-8 (1 дн.) and ZAP-7 (0) not highlighted', not state['ZAP-8'][1] and not state['ZAP-7'][1])
    check('ZAP-4 (12 дн., no threshold) not highlighted', not state['ZAP-4'][1])
    shot(page, '1-stale-tiles', size)
    before_done = board_state(page)['Готово']
    click_and_wait(page, page.locator('label.board-filters__check', has_text='Застрявшие'), 'stale=1')
    after = board_state(page)
    check(
        '«Застрявшие»: Сделать = ZAP-1, ZAP-2; В работе and На проверке empty',
        sorted(after['Сделать'][1]) == ['ZAP-1', 'ZAP-2'] and after['Сделать'][0] == 2
        and after['В работе'] == [0, []] and after['На проверке'] == [0, []],
        after,
    )
    check('«Готово» untouched by the filter', after['Готово'] == before_done, after['Готово'])
    line, detail = single_line(page)
    check('filter row one line, no sideways scroll (board)', line, detail)
    shot(page, '1-stale-filter', size)

    # 2. «Таблица».
    page.goto(BASE + BOARD)
    page.wait_for_load_state('networkidle')
    click_and_wait(page, page.locator('.board-view-switch__link', has_text='Таблица'), 'view=table')
    data = table(page)
    check(
        'table headers: code, title, column, people, due, days, the five fields and two dates',
        data['headers'] == [
            'Код', 'Название', 'Колонка', 'Исполнители', 'Срок', 'В колонке', 'Номер заявки',
            'Заказ покупателя', 'Срок изготовления', 'Приоритет', 'Стоп', 'Сумма', 'Создана', 'Завершена',
        ],
        data['headers'],
    )
    check(
        'rows in the board\'s order',
        codes(page) == ['ZAP-1', 'ZAP-2', 'ZAP-7', 'ZAP-8', 'ZAP-3', 'ZAP-5', 'ZAP-4', 'ZAP-6'],
        codes(page),
    )
    stale_rows = page.locator('.board-table__days--stale').count()
    check('days of the stuck rows highlighted (2)', stale_rows == 2, stale_rows)
    line, detail = single_line(page)
    check('filter row one line, no sideways scroll (table)', line, detail)
    page_wide = page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
    card_scrolls = page.evaluate(
        "(() => { const c = document.querySelector('.board-table-card'); return c.scrollWidth > c.clientWidth; })()"
    )
    check('the table scrolls sideways inside its card, not the page', not page_wide and card_scrolls, (page_wide, card_scrolls))
    shot(page, '2-table', size)

    click_and_wait(page, page.locator('.board-table thead a.sort-link', has_text='Приоритет'), 'sort=field_4')
    check(
        'sorted by «Приоритет»: Высокий, Средний, Низкий, then the empty',
        codes(page) == ['ZAP-1', 'ZAP-3', 'ZAP-6', 'ZAP-2', 'ZAP-4', 'ZAP-7', 'ZAP-8', 'ZAP-5'],
        codes(page),
    )
    shot(page, '2-table-priority', size)
    click_and_wait(page, page.locator('.board-table thead a.sort-link', has_text='Приоритет'), 'sort=-field_4')
    check(
        '«Приоритет» descending, the empty still last',
        codes(page) == ['ZAP-4', 'ZAP-2', 'ZAP-1', 'ZAP-3', 'ZAP-6', 'ZAP-7', 'ZAP-8', 'ZAP-5'],
        codes(page),
    )
    click_and_wait(page, page.locator('.board-table thead a.sort-link', has_text=DUE), 'sort=due')
    check(
        'sorted by «Срок»',
        codes(page) == ['ZAP-3', 'ZAP-8', 'ZAP-1', 'ZAP-7', 'ZAP-5', 'ZAP-6', 'ZAP-4', 'ZAP-2'],
        codes(page),
    )
    shot(page, '2-table-due', size)
    click_and_wait(page, page.locator('.board-table thead a.sort-link', has_text=DUE), 'sort=-due')
    check(
        '«Срок» descending',
        codes(page) == ['ZAP-2', 'ZAP-4', 'ZAP-7', 'ZAP-5', 'ZAP-6', 'ZAP-1', 'ZAP-8', 'ZAP-3'],
        codes(page),
    )
    click_and_wait(page, page.locator('.board-table thead a.sort-link', has_text=DUE), 'sort=due')
    page.locator('.board-filters__fields-toggle').click()
    high = page.locator('.board-field-filter', has=page.locator('legend', has_text='Приоритет')).locator(
        'label', has=page.locator('.board-chip', has_text=re.compile('^Высокий$')),
    ).locator('input')
    with page.expect_navigation():
        high.check()
    page.wait_for_load_state('networkidle')
    check('«Высокий» in the table: ZAP-3, ZAP-1, ZAP-6 by «Срок»', codes(page) == ['ZAP-3', 'ZAP-1', 'ZAP-6'], codes(page))
    check('the order survived the filter', 'sort=due' in page.url, page.url)
    chips = page.eval_on_selector_all('.board-filter-chip > span', 'n => n.map(e => e.textContent.trim())')
    check('chip «Приоритет: Высокий»', chips == ['Приоритет: Высокий'], chips)
    page.mouse.click(size[0] - 20, size[1] - 20)
    shot(page, '2-table-high', size)

    # 3. «Все поддоски».
    page.goto(BASE + BOARD + '?view=table')
    page.wait_for_load_state('networkidle')
    with page.expect_navigation():
        page.locator('label.board-filters__check', has_text='Все поддоски').click()
    page.wait_for_load_state('networkidle')
    with page.expect_navigation():
        page.locator('label.board-filters__check', has_text='Отменённые').click()
    page.wait_for_load_state('networkidle')
    data = table(page)
    check('«Поддоска» right after «Код»', data['headers'][:3] == ['Код', 'Поддоска', 'Название'], data['headers'][:3])
    pir = [row for row in data['rows'] if row[0] == 'ZAP-9']
    check('ZAP-9 of «Цех ПиР» is there', pir and pir[0][1] == 'Цех ПиР', pir)
    check('address: scope=board, cancelled=1', 'scope=board' in page.url and 'cancelled=1' in page.url, page.url)
    shot(page, '3-all-sub-boards', size)

    # 4. Excel: the same table.
    page.goto(BASE + BOARD + '?view=table&sort=-field_6')
    page.wait_for_load_state('networkidle')
    screen = table(page)
    with page.expect_download() as info:
        page.locator('.board-filters a', has_text='Excel').click()
    download = info.value
    name = download.suggested_filename
    check('file name ZAP-Osnovnaya-<date>.xlsx', re.fullmatch(r'ZAP-Osnovnaya-\d{4}-\d{2}-\d{2}\.xlsx', name), name)
    path = os.path.join(tempfile.mkdtemp(), name)
    download.save_as(path)
    sheet = load_workbook(path).active
    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
    header = rows[0]
    check(
        'Excel headers: the screen\'s plus «Статус»',
        header == ['Код', 'Название', 'Колонка', 'Статус', 'Исполнители', 'Срок', 'В колонке, дн.', 'Номер заявки',
                   'Заказ покупателя', 'Срок изготовления', 'Приоритет', 'Стоп', 'Сумма', 'Создана', 'Завершена'],
        header,
    )
    check('Excel rows = screen rows, same order', [row[0] for row in rows[1:]] == [row[0] for row in screen['rows']])
    same = all(
        excel[1] == shown[1] and excel[2] == shown[2]
        and (excel[6] is None) == (shown[5] == '—')
        for excel, shown in zip(rows[1:], screen['rows'])
    )
    check('title, column and days agree cell by cell', same)
    zap1 = next(row for row in rows[1:] if row[0] == 'ZAP-1')
    types = [type(value).__name__ for value in zap1]
    check(
        'ZAP-1 cell types: dates are dates, numbers are numbers, empty is empty',
        types == ['str', 'str', 'str', 'str', 'str', 'datetime', 'int', 'str', 'NoneType', 'datetime', 'str',
                  'str', 'int', 'datetime', 'NoneType'],
        types,
    )
    check('the date format is ДД.ММ.ГГГГ', sheet['F2'].number_format == 'dd.mm.yyyy', sheet['F2'].number_format)
    if size == SIZES[0]:
        print('\nПервые строки скачанного файла (openpyxl):')
        for row in rows[:5]:
            print(' | '.join('' if value is None else (value.strftime('%d.%m.%Y') if hasattr(value, 'strftime') else str(value)) for value in row))
        print()
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
