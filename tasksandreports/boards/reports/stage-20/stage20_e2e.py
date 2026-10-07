"""Browser check of stage 20: «Подзадачи» — cards inside a card.

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-20/stage20_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it (no subtask anywhere).

Two rounds, 1920×1080 and 1536×864, each:
1. `admin1` opens ZAP-1 (the order) on «Подзадачи», adds three positions with
   «Добавить списком», then turns the checklist step «Передать заказ в цех
   МП» into a fourth with «В подзадачу»; the tab lists four, «0 из 4
   выполнено», «Описание» says «Подзадачи: 0 из 4», the tile «⧉ 0/4»;
2. a subtask opened from the list shows «Подзадача карточки ZAP-1 · …» and
   three tabs; the link to ZAP-1 brings the order back, without a reload;
3. `ivanov`, the исполнитель, finds the subtask in «Мои задачи» with its
   source «… · подзадача ZAP-1», opens it — the board's drawer — and
   completes it; `admin1`'s page shows «⧉ 1/4» and «1 из 4» without a
   reload and without the conflict banner;
5. «Завершить» of ZAP-1 with three subtasks open: «Открыто подзадач: 3
   (…)» above «Результат», and the same sentence in the drop dialog of the
   tile and in «Отменить карточку»;
4. `ivanov` closes the other three; `admin1`'s bell: «Все подзадачи
   карточки ZAP-1 выполнены»; ZAP-1 itself is still in work;
6. «Таблица» with «Подзадачи»: the four right under ZAP-1, indented, with
   «Родитель»; the Excel holds the same rows;
7. (`reviews/stage-19.md`) a picture sent to «Чат» with a long name: under
   its thumbnail the name has the whole width, the size and the links below.
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import io
import os
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from xml.etree import ElementTree

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
RESET = os.environ.get('RESET', '')
BOARD = '/work/boards/1/1/'
CARD = BOARD + '?card=1'
SIZES = ((1920, 1080), (1536, 864))
LIST = 'Корпус 1200×800\nКрышка с уплотнителем\n\nКомплект крепежа М8'

results = []
problems = []


def png(width, height, rgb=(96, 140, 200)):
    """A plain PNG of that size — a photo of a part, as far as «Чат» cares."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b'\x00' + bytes(rgb) * width
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(row * height)) + chunk(b'IEND', b''))


WORK = tempfile.mkdtemp(prefix='stage20-files-')
PICTURE = os.path.join(WORK, 'фото-заготовки-корпуса-крупным-планом.png')
with open(PICTURE, 'wb') as handle:
    handle.write(png(400, 260))


def on_dialog(username, dialog):
    problems.append(f'{username}: dialog {dialog.type}')
    dialog.dismiss()


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + str(detail)) if detail else ''}")


def login(browser, username, size):
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]}, accept_downloads=True)
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
    # The live client is up once it has synced.
    page.wait_for_timeout(1200)


def wait_for(page, predicate, timeout=10000):
    waited = 0
    while waited < timeout:
        if predicate(page):
            return True
        page.wait_for_timeout(250)
        waited += 250
    return predicate(page)


def text(page, selector):
    locator = page.locator(selector)
    return ' '.join(locator.first.inner_text().split()) if locator.count() else ''


def subtask_rows(page):
    return page.locator('[data-live-board-subtasks] .board-subtask')


def tile_subtasks(page):
    return text(page, '[data-card-id="1"] [data-tile-subtasks]')


def no_sideways_scroll(page):
    return page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth + 1')


def complete_open_drawer(page, result):
    page.locator('details[data-board-complete] > summary').click()
    page.fill('#board-complete-result', result)
    with page.expect_navigation():
        page.locator('form.board-complete__form button[type=submit]').click()
    page.wait_for_load_state('networkidle')


def read_xlsx_rows(content):
    ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    rows = []
    for row in sheet.find('x:sheetData', ns):
        cells = []
        for cell in row:
            inline = cell.find('x:is/x:t', ns)
            value = cell.find('x:v', ns)
            cells.append(inline.text if inline is not None else (value.text if value is not None else None))
        rows.append(cells)
    return rows


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)

    # 1. «Добавить списком» and «В подзадачу».
    admin.goto(BASE + CARD + '&tab=subtasks')
    settle(admin)
    tabs = [link.get_attribute('data-board-tab-link') for link in admin.locator('.board-drawer__tabs [data-board-tab-link]').all()]
    check('a card has four tabs: Описание, Чат, Подзадачи, Лог', tabs == ['description', 'chat', 'subtasks', 'log'], tabs)
    check('«Подзадачи» opens on its tab', admin.locator('[data-board-drawer]').get_attribute('data-board-tab') == 'subtasks')
    check('no subtasks yet', 'Подзадач пока нет' in text(admin, '[data-live-board-subtasks]'))
    admin.locator('details.board-subtasks__list > summary').click()
    admin.fill('#board-subtasks-list', LIST)
    with admin.expect_navigation():
        admin.locator('form.board-subtasks__list-form button[type=submit]').click()
    settle(admin)
    titles = [' '.join(row.locator('.board-subtask__title').inner_text().split()) for row in subtask_rows(admin).all()]
    check('«Добавить списком»: one line — one subtask, the empty one skipped',
          titles == ['Корпус 1200×800', 'Крышка с уплотнителем', 'Комплект крепежа М8'], titles)
    check('the redirect lands on «Подзадачи»', admin.locator('[data-board-drawer]').get_attribute('data-board-tab') == 'subtasks')
    people = [row.locator('.board-subtask__people').inner_text().split() for row in subtask_rows(admin).all()]
    check('each with the order\'s исполнитель (ИИ)', all(names == ['ИИ'] for names in people), people)

    admin.locator('[data-board-tab-link="description"]').first.click()
    step = admin.locator('button[aria-label="Сделать подзадачей: Передать заказ в цех МП"]')
    check('«В подзадачу» beside a checklist step', step.count() == 1)
    # The tools of an item show on hover (always on a touchscreen).
    admin.locator('[data-checklist-item]', has_text='Передать заказ в цех МП').hover()
    shot(admin, '1-checklist-to-subtask', size)
    step.click()
    dialog = admin.locator('dialog[data-confirm-modal]')
    check('the shared confirmation asks first',
          dialog.is_visible() and 'станет подзадачей карточки ZAP-1' in dialog.inner_text(), dialog.inner_text())
    with admin.expect_navigation():
        dialog.locator('[data-confirm-modal-accept]').click()
    settle(admin)
    items = [' '.join(item.inner_text().split()) for item in admin.locator('[data-live-board-checklist] .board-checklist__text').all()]
    check('the step left the checklist', 'Передать заказ в цех МП' not in items and len(items) == 4, items)
    check('«Описание»: «Подзадачи: 0 из 4»', text(admin, '[data-live-board-subtask-summary]') == 'Подзадачи: 0 из 4',
          text(admin, '[data-live-board-subtask-summary]'))
    check('the tile: «⧉ 0/4»', tile_subtasks(admin) == '⧉ 0/4', tile_subtasks(admin))
    admin.locator('[data-live-board-subtask-summary] a').click()
    admin.wait_for_timeout(300)
    check('«Подзадачи: 0 из 4» switches to the tab in place',
          admin.locator('[data-board-drawer]').get_attribute('data-board-tab') == 'subtasks')
    codes = [row.locator('.board-subtask__code').inner_text().strip() for row in subtask_rows(admin).all()]
    check('four subtasks, the board\'s own numbers', len(codes) == 4 and all(code.startswith('ZAP-') for code in codes), codes)
    check('«0 из 4 выполнено»', text(admin, '.board-subtasks__progress-label') == '0 из 4 выполнено')
    check('«Подзадачи 0/4» in the tab strip', text(admin, '[data-board-tab-count="subtasks"]') == '0/4')
    tiles = [number.inner_text().strip() for number in admin.locator('[data-live-board-columns] .board-tile__number').all()]
    check('no subtask is a tile', 'ZAP-1' in tiles and not set(codes) & set(tiles), tiles)
    check('no sideways scroll of the page', no_sideways_scroll(admin))
    shot(admin, '1-subtasks', size)
    admin.locator('[data-card-id="1"]').screenshot(path=os.path.join(SHOTS, f'1-tile-{size[0]}.png'))
    first_id = subtask_rows(admin).first.get_attribute('data-subtask-id')
    ids = [row.get_attribute('data-subtask-id') for row in subtask_rows(admin).all()]

    # 2. A subtask opened from the list, and back to its card.
    url_before = admin.url
    subtask_rows(admin).first.locator('.board-subtask__code').click()
    opened = wait_for(admin, lambda p: f'card={first_id}' in p.url and 'Подзадача карточки' in text(p, '[data-live-board-panel]'))
    check('the code opens the subtask in the drawer, no reload', opened, admin.url)
    heading = text(admin, '.board-drawer__parent')
    check('«Подзадача карточки ZAP-1 · Согласовать спецификацию»',
          heading == 'Подзадача карточки ZAP-1 · Согласовать спецификацию', heading)
    tabs = [link.get_attribute('data-board-tab-link') for link in admin.locator('.board-drawer__tabs [data-board-tab-link]').all()]
    check('a subtask has three tabs, no «Подзадачи»', tabs == ['description', 'chat', 'log'], tabs)
    check('its facts name the card, not a column', 'Карточка ZAP-1' in text(admin, '[data-live-board-facts]'))
    check('the page did not reload', url_before != admin.url and admin.evaluate('() => performance.getEntriesByType("navigation").length') == 1)
    shot(admin, '2-subtask', size)
    admin.locator('.board-drawer__parent a').click()
    back = wait_for(admin, lambda p: 'card=1&' in p.url + '&' and text(p, '.board-drawer__code') == 'Карточка ZAP-1')
    check('the link to ZAP-1 opens the order again, on «Подзадачи»', back
          and admin.locator('[data-board-drawer]').get_attribute('data-board-tab') == 'subtasks', admin.url)

    # 3. ivanov: «Мои задачи», the subtask, «Завершить».
    ivanov.goto(BASE + '/quality/tasks/?tab=my')
    ivanov.wait_for_load_state('networkidle')
    row = ivanov.locator('tr', has_text='Корпус 1200×800')
    check('ivanov finds the subtask in «Мои задачи»', row.count() == 1)
    source = ' '.join(row.first.inner_text().split()) if row.count() else ''
    check('its source: «Доска «Запуск заказов» · ZAP-… · подзадача ZAP-1»', 'подзадача ZAP-1' in source, source)
    shot(ivanov, '3-my-tasks', size)
    with ivanov.expect_navigation():
        row.first.locator('a').first.click()
    settle(ivanov)
    check('it opens the board with the subtask\'s drawer', f'card={first_id}' in ivanov.url
          and 'Подзадача карточки ZAP-1' in text(ivanov, '[data-live-board-panel]'), ivanov.url)
    complete_open_drawer(ivanov, 'Корпус сварен, ОТК принят.')
    check('ivanov: «Выполнена»', 'Выполнена' in text(ivanov, '[data-live-board-panel]'))
    live = wait_for(admin, lambda p: tile_subtasks(p) == '⧉ 1/4'
                    and text(p, '.board-subtasks__progress-label') == '1 из 4 выполнено')
    check('admin1 sees «⧉ 1/4» and «1 из 4 выполнено» without a reload', live,
          (tile_subtasks(admin), text(admin, '.board-subtasks__progress-label')))
    check('the done one went to the bottom of the list',
          subtask_rows(admin).last.get_attribute('data-subtask-id') == first_id)
    check('no conflict banner', admin.locator('[data-board-conflict-banner]').is_hidden())
    shot(admin, '3-live', size)

    # 5. «Завершить» of ZAP-1 with three open.
    admin.locator('details[data-board-complete] > summary').click()
    warning = text(admin, '[data-live-board-subtask-warning]')
    check('«Завершить»: «Открыто подзадач: 3 (…)» above «Результат»', warning.startswith('Открыто подзадач: 3 (ZAP-'), warning)
    shot(admin, '5-complete-warning', size)
    admin.locator('details[data-board-complete] > summary').click()
    drop = admin.locator('[data-card-id="1"] [data-card-complete-trigger]').get_attribute('data-confirm-text') or ''
    check('the drop dialog of the tile says it too', 'Открыто подзадач: 3' in drop, drop)
    admin.locator('[data-board-tab-link="description"]').first.click()
    cancel = admin.locator('[data-board-cancel-trigger]').get_attribute('data-confirm-text') or ''
    check('and «Отменить карточку»', 'Открыто подзадач: 3' in cancel, cancel)

    # 4. The last one closed: the bell.
    for subtask_id in ids[1:]:
        ivanov.goto(BASE + f'{BOARD}?card={subtask_id}')
        ivanov.wait_for_load_state('networkidle')
        complete_open_drawer(ivanov, 'Сделано.')
    admin.goto(BASE + CARD + '&tab=subtasks')
    settle(admin)
    check('«4 из 4 выполнено», the tile green', text(admin, '.board-subtasks__progress-label') == '4 из 4 выполнено'
          and admin.locator('[data-card-id="1"] .board-tile__subtasks--complete').count() == 1)
    check('ZAP-1 itself is still in work', 'В работе' in text(admin, '[data-live-board-panel]'))
    admin.locator('[data-notification-summary]').click()
    item = admin.locator('.notification-menu__item', has_text='Все подзадачи')
    check('admin1\'s bell: «Все подзадачи карточки ZAP-1 выполнены»',
          item.count() == 1 and 'Все подзадачи карточки ZAP-1 выполнены' in item.first.inner_text(), item.all_inner_texts())
    shot(admin, '4-bell', size)
    admin.keyboard.press('Escape')

    # 6. «Таблица» with «Подзадачи».
    admin.goto(BASE + BOARD + '?view=table&subtasks=1')
    admin.wait_for_load_state('networkidle')
    headers = [' '.join(th.inner_text().split()) for th in admin.locator('.board-table thead th').all()]
    check('«Родитель» after «Код»', headers[:2] == ['Код', 'Родитель'], headers[:3])
    codes_in_table = [' '.join(td.inner_text().split()) for td in admin.locator('.board-table tbody tr td:first-child').all()]
    at = codes_in_table.index('ZAP-1') if 'ZAP-1' in codes_in_table else -1
    check('the four subtasks right under ZAP-1', at >= 0 and set(codes_in_table[at + 1:at + 5]) == set(codes),
          codes_in_table)
    check('indented', admin.locator('.board-table__row--subtask').count() == 4)
    check('the «Подзадачи» box is ticked', admin.locator('input[name=subtasks]').is_checked())
    shot(admin, '6-table', size)
    response = admin.request.get(BASE + BOARD + '?view=table&subtasks=1&export=xlsx')
    rows = read_xlsx_rows(response.body())
    check('the Excel: «Родитель» and the same rows', rows[0][:2] == ['Код', 'Родитель']
          and [row[0] for row in rows[1:]] == codes_in_table
          and [row[1] for row in rows[1:] if row[0] in codes] == ['ZAP-1'] * 4, rows[:3])
    admin.goto(BASE + BOARD + '?view=table')
    admin.wait_for_load_state('networkidle')
    plain = [' '.join(td.inner_text().split()) for td in admin.locator('.board-table tbody tr td:first-child').all()]
    check('without the box no subtask in the table', not set(codes) & set(plain), plain)

    # 7. The name under a thumbnail has the whole width of the row.
    admin.goto(BASE + CARD + '&tab=chat')
    settle(admin)
    admin.locator('input[data-chat-files-input]').set_input_files(PICTURE)
    with admin.expect_navigation():
        admin.locator('form.board-chat__form button[type=submit]').click()
    settle(admin)
    image = admin.locator('.board-chat__image').last
    box = image.bounding_box()
    name = image.locator('.board-file__name').bounding_box()
    size_box = image.locator('.board-file__size').bounding_box()
    check('the name under the thumbnail takes the whole width of the row',
          box and name and name['width'] >= box['width'] - 2, (box, name))
    check('the size and the links on the line below it', name and size_box and size_box['y'] > name['y'], (name, size_box))
    image.screenshot(path=os.path.join(SHOTS, f'7-thumbnail-name-{size[0]}.png'))
    for page in (admin, ivanov):
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
