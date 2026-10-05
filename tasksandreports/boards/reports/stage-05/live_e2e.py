"""Browser check of the live board (stage 5): two users on one board.

Needs Redis and two servers on the data of `seed_demo.py` — one with
real-time, one without (the board must work exactly as before there):

    redis-server --port 6379
    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
        python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8766
    python tasksandreports/boards/reports/stage-05/live_e2e.py [screenshot-dir]

Chromium is launched from /opt/pw-browsers (CHROMIUM env var overrides it).
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import sys
import time
from datetime import timedelta, date

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
BASE_OFF = os.environ.get('BASE_OFF', 'http://127.0.0.1:8766')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/'
LIVE_WAIT = 10_000   # ms: an SSE event and one fragment request, with room to spare

results = []
console_errors = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def watch_console(page, who):
    def on_console(message):
        if message.type == 'error':
            console_errors.append(f'{who}: {message.text}')
    page.on('console', on_console)
    page.on('pageerror', lambda error: console_errors.append(f'{who}: {error}'))


def login(context, base, username):
    page = context.new_page()
    watch_console(page, username)
    page.goto(base + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def open_board(page, base=BASE, query=''):
    page.goto(base + BOARD + query)
    page.evaluate('window.__sameDocument = true')


def wait_live(page):
    page.wait_for_function(
        "window.QualityRealtime && window.QualityRealtime.state === 'live'", timeout=LIVE_WAIT,
    )


def not_reloaded(page):
    return page.evaluate('window.__sameDocument === true')


def item(page, title):
    return page.locator(f'li.board-column__item:has(.board-tile__title:text-is("{title}"))')


def card_id(page, title):
    return item(page, title).get_attribute('data-card-id')


def column_titles(page, code):
    return page.locator(f'[data-column="{code}"] li.board-column__item .board-tile__title').all_inner_texts()


def wait_in_column(page, code, title, timeout=LIVE_WAIT):
    page.wait_for_function(
        """([code, title]) => Array.from(document.querySelectorAll(
               `[data-column="${code}"] li.board-column__item .board-tile__title`))
               .some((node) => node.textContent.trim() === title)""",
        arg=[code, title],
        timeout=timeout,
    )


def centre(locator):
    box = locator.bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def column_point(page, code):
    box = page.locator(f'[data-column="{code}"] [data-column-list]').bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] - 20


def mouse_drag(page, source, target_x, target_y, *, release=True):
    x, y = centre(source)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 8, y + 8, steps=2)
    page.mouse.move(target_x, target_y, steps=12)
    if release:
        page.mouse.up()


def wait_idle(page):
    page.wait_for_function('!document.querySelector("[data-board-busy]")', timeout=5000)


def create_card(page, title, assignee_last_name, stage='TODO'):
    """Through the panel's own form, as a person would."""
    page.goto(f'{BASE}{BOARD}?new={stage}')
    page.fill('input[name=title]', title)
    page.fill('input[name=due_date]', (date.today() + timedelta(days=5)).isoformat())
    page.locator(f'.board-card-form__assignees label:has-text("{assignee_last_name}") input').check()
    with page.expect_navigation():
        page.click('.board-card-form button[type=submit]')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    one = browser.new_context(viewport={'width': 1920, 'height': 1080})
    two = browser.new_context(viewport={'width': 1920, 'height': 1080})
    ivanov = login(one, BASE, 'ivanov')
    sidorova = login(two, BASE, 'sidorova')
    open_board(ivanov)
    open_board(sidorova)
    wait_live(ivanov)
    wait_live(sidorova)

    # ------------------------------------------------------------------ 1–3
    create_card(sidorova, 'Новая от Сидоровой', 'Иванов')
    wait_in_column(ivanov, 'TODO', 'Новая от Сидоровой')
    check('1 создание видно другому без перезагрузки',
          'Новая от Сидоровой' in column_titles(ivanov, 'TODO') and not_reloaded(ivanov))
    count = ivanov.locator('[data-column="TODO"] [data-column-count]').inner_text()
    check('1 счётчик колонки обновлён', count.strip() == '4', count)

    open_board(sidorova)
    wait_live(sidorova)
    x, y = column_point(sidorova, 'REVIEW')
    mouse_drag(sidorova, item(sidorova, 'Бета'), x, y)
    wait_idle(sidorova)
    wait_in_column(ivanov, 'REVIEW', 'Бета')
    check('2 перенос виден другому без перезагрузки',
          'Бета' in column_titles(ivanov, 'REVIEW') and 'Бета' not in column_titles(ivanov, 'TODO')
          and not_reloaded(ivanov))

    gamma = card_id(sidorova, 'Гамма')
    sidorova.goto(f'{BASE}{BOARD}?card={gamma}')
    sidorova.fill('#task-execution-comment', 'График согласован')
    with sidorova.expect_navigation():
        sidorova.click('.board-execution-form button[type=submit]')
    wait_in_column(ivanov, 'DONE', 'Гамма')
    check('3 завершение видно другому без перезагрузки',
          'Гамма' in column_titles(ivanov, 'DONE') and not_reloaded(ivanov))

    # ------------------------------------------------------------------ 4
    # Mid-drag: the other user's new card waits until the drop.
    open_board(sidorova)
    x, y = column_point(ivanov, 'IN_PROGRESS')
    mouse_drag(ivanov, item(ivanov, 'Альфа'), x, y, release=False)
    check('4 во время перетаскивания доска помечена data-board-busy',
          ivanov.locator('[data-board][data-board-busy]').count() == 1)
    create_card(sidorova, 'Во время перетаскивания', 'Сидорова')
    time.sleep(2.0)
    ivanov.screenshot(path=os.path.join(SHOTS, 'mid-drag-1920.png'))
    check('4 пока карточка в руке, колонки не заменены',
          'Во время перетаскивания' not in column_titles(ivanov, 'TODO')
          and ivanov.locator('.board-ghost').count() == 1
          and ivanov.evaluate('window.QualityRealtime.boardLive.isDeferred') is True)
    ivanov.mouse.up()
    wait_idle(ivanov)
    wait_in_column(ivanov, 'TODO', 'Во время перетаскивания')
    check('4 после броска обновление пришло',
          'Во время перетаскивания' in column_titles(ivanov, 'TODO'))
    check('4 и перенесённая карточка там, куда её бросили',
          'Альфа' in column_titles(ivanov, 'IN_PROGRESS') and not_reloaded(ivanov),
          str(column_titles(ivanov, 'IN_PROGRESS')))

    # ------------------------------------------------------------------ 5
    # Typing in «Выполнение»: a change of the card raises the banner and the
    # text stays; the columns are replaced all the same.
    delta = card_id(ivanov, 'Дельта')
    open_board(ivanov, query=f'?card={delta}')
    wait_live(ivanov)
    typed = 'Наполовину написанный результат'
    ivanov.click('#task-execution-comment')
    ivanov.keyboard.type(typed)
    sidorova.goto(f'{BASE}{BOARD}?card={delta}&edit=1')
    sidorova.fill('input[name=title]', 'Дельта (уточнено)')
    with sidorova.expect_navigation():
        sidorova.click('.board-card-form button[type=submit]')
    ivanov.wait_for_selector('[data-board-conflict-banner]:not([hidden])', timeout=LIVE_WAIT)
    wait_in_column(ivanov, 'IN_PROGRESS', 'Дельта (уточнено)')
    ivanov.screenshot(path=os.path.join(SHOTS, 'conflict-banner-1920.png'))
    check('5 баннер конфликта показан', ivanov.locator('[data-board-conflict-banner]').is_visible())
    check('5 введённый текст сохранён',
          ivanov.input_value('#task-execution-comment') == typed)
    check('5 панель не заменена, колонки заменены',
          ivanov.locator('.board-panel__title').inner_text() == 'Дельта'
          and 'Дельта (уточнено)' in column_titles(ivanov, 'IN_PROGRESS') and not_reloaded(ivanov))

    # ------------------------------------------------------------------ 6
    # Dragging still works on columns the live client replaced.
    x, y = column_point(ivanov, 'TODO')
    mouse_drag(ivanov, item(ivanov, 'Эпсилон'), x, y)
    wait_idle(ivanov)
    check('6 перетаскивание после замены колонок работает',
          'Эпсилон' in column_titles(ivanov, 'TODO') and ivanov.locator('[data-board-message]').is_hidden())
    open_board(sidorova)
    check('6 перенос сохранён на сервере', 'Эпсилон' in column_titles(sidorova, 'TODO'))
    ivanov.evaluate('window.qualityUnsavedGuard.markClean()')

    # ------------------------------------------------------------------ 7
    # A clean panel is replaced in place when its card changes.
    epsilon = card_id(ivanov, 'Эпсилон')
    open_board(ivanov, query=f'?card={epsilon}')
    wait_live(ivanov)
    wait_live(sidorova)
    x, y = column_point(sidorova, 'IN_PROGRESS')
    mouse_drag(sidorova, item(sidorova, 'Эпсилон'), x, y)
    wait_idle(sidorova)
    ivanov.wait_for_function(
        """() => Array.from(document.querySelectorAll('.board-panel__facts div'))
               .some((row) => row.textContent.includes('Колонка') && row.textContent.includes('В работе'))""",
        timeout=LIVE_WAIT,
    )
    check('7 чистая панель заменена без перезагрузки',
          not_reloaded(ivanov) and ivanov.locator('[data-board-conflict-banner]').is_hidden())

    # ------------------------------------------------------------------ 8
    # §0: the open card moved on a page drawn in answer to a refused POST —
    # the board's own address is loaded, the POST is never repeated.
    moved = card_id(ivanov, 'Перенос открытой')
    ivanov.goto(f'{BASE}{BOARD}?card={moved}')
    ivanov.evaluate(
        """() => {
            const select = document.querySelector('.board-panel__move select[name=stage]');
            select.add(new Option('Нет такой', 'BOGUS'));
            select.value = 'BOGUS';
        }"""
    )
    with ivanov.expect_navigation():
        ivanov.click('.board-panel__move button[type=submit]')
    refused_url = ivanov.url
    check('8 отказанный POST отрисовал доску по адресу POST',
          refused_url.endswith(f'/cards/{moved}/move/') and 'Выберите колонку' in ivanov.content(), refused_url)
    posts = []
    ivanov.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
    x, y = column_point(ivanov, 'REVIEW')
    with ivanov.expect_navigation():
        mouse_drag(ivanov, item(ivanov, 'Перенос открытой'), x, y)
    check('8 после переноса открытой карточки — адрес доски с ?card=',
          ivanov.url == f'{BASE}{BOARD}?card={moved}', ivanov.url)
    check('8 POST не повторён: только сам перенос', posts == [f'{BASE}{BOARD}cards/{moved}/move/'], str(posts))
    check('8 карточка в новой колонке', 'Перенос открытой' in column_titles(ivanov, 'REVIEW'))

    one.close()
    two.close()

    # ------------------------------------------------------------------ 9
    # Real-time off: the board is the page the server drew, and works.
    off = browser.new_context(viewport={'width': 1920, 'height': 1080})
    page = login(off, BASE_OFF, 'ivanov')
    open_board(page, BASE_OFF)
    check('9 без real-time клиент не загружен',
          page.evaluate('typeof window.QualityRealtime === "undefined"')
          and page.locator('script[src*="realtime/boards.js"]').count() == 0)
    x, y = column_point(page, 'TODO')
    mouse_drag(page, item(page, 'Альфа'), x, y)
    wait_idle(page)
    page.reload()
    check('9 без real-time перетаскивание работает', 'Альфа' in column_titles(page, 'TODO'))
    off.close()
    browser.close()

relevant = [line for line in console_errors if 'favicon' not in line]
check('ошибок в консоли нет', not relevant, '; '.join(relevant))

failures = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failures} из {len(results)} проверок прошли')
sys.exit(failures)
