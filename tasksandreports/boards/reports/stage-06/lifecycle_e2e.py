"""Browser check of stage 6: cancel, archive, filters, the card version, §0.

Needs Redis and two servers on the data of `seed_demo.py` — one with
real-time, one without — exactly as in stage 5 (the ASGI wrapper that adds
static files is stage 5's):

    redis-server --port 6379
    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
        python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8766
    python tasksandreports/boards/reports/stage-06/lifecycle_e2e.py [screenshot-dir]

Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import sys
from datetime import date, timedelta

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
BASE_OFF = os.environ.get('BASE_OFF', 'http://127.0.0.1:8766')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/'
OLD_BOARD = '/work/boards/2/'
LIVE_WAIT = 10_000

results = []
console_errors = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def login(context, base, username):
    page = context.new_page()
    page.on('console', lambda m: console_errors.append(f'{username}: {m.text}') if m.type == 'error' else None)
    page.on('pageerror', lambda e: console_errors.append(f'{username}: {e}'))
    page.goto(base + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def open_board(page, base=BASE, query='', board=BOARD):
    page.goto(base + board + query)
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


def all_titles(page):
    return page.locator('[data-live-board-columns] li.board-column__item .board-tile__title').all_inner_texts()


def centre(locator):
    box = locator.bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def column_point(page, code):
    box = page.locator(f'[data-column="{code}"] [data-column-list]').bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] - 20


def mouse_drag(page, source, target_x, target_y):
    x, y = centre(source)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 8, y + 8, steps=2)
    page.mouse.move(target_x, target_y, steps=12)
    page.mouse.up()


def wait_idle(page):
    page.wait_for_function('!document.querySelector("[data-board-busy]")', timeout=5000)


def panel_fact(page, label):
    return page.locator(f'.board-panel__facts div:has(dt:text-is("{label}")) dd').inner_text()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    one = browser.new_context(viewport={'width': 1920, 'height': 1080})
    two = browser.new_context(viewport={'width': 1920, 'height': 1080})
    three = browser.new_context(viewport={'width': 1920, 'height': 1080})
    ivanov = login(one, BASE, 'ivanov')
    sidorova = login(two, BASE, 'sidorova')
    petrova = login(three, BASE, 'petrova')

    # ------------------------------------------------------------------ 1
    # §0: with the live client running, moving the open card navigates
    # nowhere and says nothing; the panel follows by itself.
    moved = None
    open_board(ivanov)
    moved = card_id(ivanov, 'Перенос открытой')
    open_board(ivanov, query=f'?card={moved}')
    wait_live(ivanov)
    x, y = column_point(ivanov, 'IN_PROGRESS')
    mouse_drag(ivanov, item(ivanov, 'Перенос открытой'), x, y)
    wait_idle(ivanov)
    ivanov.wait_for_function(
        """() => Array.from(document.querySelectorAll('.board-panel__facts div'))
               .some((row) => row.textContent.includes('Колонка') && row.textContent.includes('В работе'))""",
        timeout=LIVE_WAIT,
    )
    check('1 §0 real-time: страница не перезагружена, панель обновилась сама',
          not_reloaded(ivanov) and ivanov.url.endswith(f'?card={moved}'))
    check('1 §0 real-time: сообщения «обновите страницу» нет',
          ivanov.locator('[data-board-message]').is_hidden())

    # ------------------------------------------------------------------ 2
    # Filters: «Мои» applies on change, the search after a pause.
    open_board(ivanov)
    ivanov.check('.board-filters input[name=mine]')
    ivanov.wait_for_url('**mine=1**')
    mine = all_titles(ivanov)
    check('2 фильтр «Мои»: только карточки Иванова',
          'Бета' not in mine and 'Эпсилон' not in mine and 'Альфа' in mine, str(mine))
    count = ivanov.locator('[data-column="TODO"] [data-column-count]').inner_text().strip()
    check('2 счётчик колонки — отфильтрованный', count == '3', count)
    check('2 ссылка плитки сохраняет фильтр',
          'mine=1' in item(ivanov, 'Альфа').locator('a.board-tile').get_attribute('href'))
    ivanov.screenshot(path=os.path.join(SHOTS, 'filter-mine-1920.png'))
    ivanov.fill('.board-filters input[name=q]', 'Альф')
    # The GET form already sent an empty `q=` with «Мои»: wait for the term.
    ivanov.wait_for_url(lambda url: 'q=%D0%90' in url, timeout=5000)
    check('2 поиск по названию', all_titles(ivanov) == ['Альфа'], str(all_titles(ivanov)))
    ivanov.click('.board-filters__reset')
    ivanov.wait_for_load_state()
    check('2 «Сбросить» — доска без фильтра',
          'mine=' not in ivanov.url and 'Бета' in all_titles(ivanov), ivanov.url)
    ivanov.goto(f'{BASE}{BOARD}?overdue=1')
    check('2 «Просроченные»', all_titles(ivanov) == ['Дельта просрочена'], str(all_titles(ivanov)))

    # ------------------------------------------------------------------ 3
    # Dragging under «Мои»: «Зета» above «Гамма», with «Бета» (not mine)
    # hidden in between. The service places it before «Гамма» in the full
    # column, so the hidden card keeps its place.
    open_board(ivanov, query='?mine=1')
    gamma_box = item(ivanov, 'Гамма').bounding_box()
    mouse_drag(ivanov, item(ivanov, 'Зета'), gamma_box['x'] + gamma_box['width'] / 2, gamma_box['y'] + 5)
    wait_idle(ivanov)
    check('3 перетаскивание с фильтром: видимый порядок',
          column_titles(ivanov, 'TODO') == ['Альфа', 'Зета', 'Гамма'], str(column_titles(ivanov, 'TODO')))
    check('3 счётчик после броска остаётся отфильтрованным',
          ivanov.locator('[data-column="TODO"] [data-column-count]').inner_text().strip() == '3')
    ivanov.goto(BASE + BOARD)
    check('3 полный порядок колонки не сломан',
          column_titles(ivanov, 'TODO') == ['Альфа', 'Бета', 'Зета', 'Гамма'], str(column_titles(ivanov, 'TODO')))

    # ------------------------------------------------------------------ 4
    # Cancel: Сидорова puts a card on Иванов by mistake and withdraws it.
    sidorova.goto(f'{BASE}{BOARD}?new=TODO')
    sidorova.fill('input[name=title]', 'Ошибочная')
    sidorova.fill('input[name=due_date]', (date.today() + timedelta(days=5)).isoformat())
    sidorova.locator('.board-card-form__assignees label:has-text("Иванов") input').check()
    with sidorova.expect_navigation():
        sidorova.click('.board-card-form button[type=submit]')
    wrong = sidorova.url.split('card=')[1].split('&')[0]
    open_board(ivanov, query=f'?card={wrong}')
    wait_live(ivanov)
    check('4 исполнитель (не автор) не видит «Отменить карточку»',
          ivanov.locator('button:has-text("Отменить карточку")').count() == 0)
    open_board(ivanov)
    wait_live(ivanov)
    sidorova.click('button:has-text("Отменить карточку")')
    dialog = sidorova.locator('[data-confirm-modal]')
    check('4 окно отмены с обязательной причиной',
          dialog.is_visible() and sidorova.locator('[data-confirm-modal-comment-input]').is_visible())
    sidorova.fill('[data-confirm-modal-comment-input]', 'Поставила не на того исполнителя')
    sidorova.screenshot(path=os.path.join(SHOTS, 'cancel-modal-1920.png'))
    with sidorova.expect_navigation():
        sidorova.click('[data-confirm-modal-accept]')
    panel = sidorova.locator('.board-panel').inner_text()
    check('4 панель отменённой карточки — причина, кто отменил',
          'Поставила не на того исполнителя' in panel and 'Отменил' in panel, panel[:200])
    check('4 карточки нет в колонках', 'Ошибочная' not in all_titles(sidorova))
    ivanov.wait_for_function(
        """() => !Array.from(document.querySelectorAll('[data-live-board-columns] .board-tile__title'))
               .some((node) => node.textContent.trim() === 'Ошибочная')""",
        timeout=LIVE_WAIT,
    )
    check('4 у другого участника карточка исчезла без перезагрузки', not_reloaded(ivanov))

    # ------------------------------------------------------------------ 5
    # Two tabs edit one card: the second save is refused, its text stays.
    edit_id = card_id(ivanov, 'Правка вдвоём')
    tab_a = ivanov
    tab_b = one.new_page()
    tab_b.on('pageerror', lambda e: console_errors.append(f'ivanov/b: {e}'))
    tab_a.goto(f'{BASE}{BOARD}?card={edit_id}&edit=1')
    tab_b.goto(f'{BASE}{BOARD}?card={edit_id}&edit=1')
    tab_b.fill('textarea[name=description]', 'Текст второй вкладки, который нельзя потерять')
    tab_a.fill('input[name=title]', 'Правка вдвоём (первая)')
    with tab_a.expect_navigation():
        tab_a.click('.board-card-form button[type=submit]')
    with tab_b.expect_navigation():
        tab_b.click('.board-card-form button[type=submit]')
    content = tab_b.content()
    check('5 вторая вкладка получает отказ', 'Карточку изменили, пока вы её редактировали' in content)
    check('5 введённое на месте',
          tab_b.input_value('textarea[name=description]') == 'Текст второй вкладки, который нельзя потерять')
    link = tab_b.locator('a:has-text("Открыть текущую версию")')
    check('5 ссылка «Открыть текущую версию» в новой вкладке',
          link.get_attribute('target') == '_blank' and link.get_attribute('href').endswith(f'?card={edit_id}'))
    tab_b.screenshot(path=os.path.join(SHOTS, 'version-conflict-1920.png'))
    tab_a.goto(f'{BASE}{BOARD}?card={edit_id}')
    check('5 в базе правка первой вкладки',
          tab_a.locator('.board-panel__title').inner_text() == 'Правка вдвоём (первая)')
    tab_b.evaluate('window.qualityUnsavedGuard && window.qualityUnsavedGuard.markClean()')
    tab_b.close()

    # ------------------------------------------------------------------ 6
    # The shelf: a board with open cards is refused; a finished one goes and
    # comes back.
    petrova.goto(BASE + BOARD)
    petrova.click('button:has-text("В архив")')
    with petrova.expect_navigation():
        petrova.click('[data-confirm-modal-accept]')
    check('6 доску с открытыми карточками в архив не убрать',
          'Сначала завершите или отмените открытые карточки' in petrova.content())
    petrova.goto(BASE + OLD_BOARD)
    petrova.click('button:has-text("В архив")')
    with petrova.expect_navigation():
        petrova.click('[data-confirm-modal-accept]')
    heading = petrova.locator('.board-heading').inner_text()
    check('6 архивная доска по тому же адресу, с пометкой',
          petrova.url.endswith(OLD_BOARD) and 'В архиве' in heading)
    check('6 на архивной доске нет действий',
          petrova.locator('text=+ Карточка').count() == 0 and petrova.locator('[data-card-movable]').count() == 0
          and petrova.locator('button:has-text("Вернуть из архива")').count() == 1)
    petrova.screenshot(path=os.path.join(SHOTS, 'archived-board-1920.png'))
    petrova.goto(BASE + '/work/boards/?tab=archive')
    check('6 вкладка «Архив» реестра', 'Старая доска' in petrova.locator('.board-registry-table').inner_text())
    petrova.goto(BASE + '/work/boards/?tab=all')
    check('6 «Все» — только активные', 'Старая доска' not in petrova.content())
    petrova.goto(BASE + OLD_BOARD)
    petrova.click('button:has-text("Вернуть из архива")')
    with petrova.expect_navigation():
        petrova.click('[data-confirm-modal-accept]')
    check('6 возврат из архива', 'В архиве' not in petrova.locator('.board-heading').inner_text()
          and petrova.locator('button:has-text("В архив")').count() == 1)

    one.close()
    two.close()
    three.close()

    # ------------------------------------------------------------------ 7
    # §0 without real-time: the board's own address is loaded.
    off = browser.new_context(viewport={'width': 1920, 'height': 1080})
    page = login(off, BASE_OFF, 'ivanov')
    open_board(page, BASE_OFF, query=f'?card={moved}')
    x, y = column_point(page, 'REVIEW')
    with page.expect_navigation():
        mouse_drag(page, item(page, 'Перенос открытой'), x, y)
    check('7 §0 без real-time: переход на адрес доски с ?card=',
          page.url == f'{BASE_OFF}{BOARD}?card={moved}' and panel_fact(page, 'Колонка') == 'На проверке',
          page.url)
    off.close()
    browser.close()

relevant = [line for line in console_errors if 'favicon' not in line]
check('ошибок в консоли нет', not relevant, '; '.join(relevant))

failures = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failures} из {len(results)} проверок прошли')
sys.exit(failures)
