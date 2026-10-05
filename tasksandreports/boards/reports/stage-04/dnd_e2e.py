"""Browser check of dragging cards on a board (stage 4).

Needs a running server on BASE with the data of `seed_demo.py`:

    python manage.py runserver 127.0.0.1:8765
    python tasksandreports/boards/reports/stage-04/dnd_e2e.py [screenshot-dir]

Chromium is launched from /opt/pw-browsers (CHROMIUM env var overrides it).
Each scenario prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/'

results = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def login(context, username):
    page = context.new_page()
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def item(page, title):
    return page.locator(f'li.board-column__item:has(a:has-text("{title}"))')


def column_titles(page, code):
    return page.locator(f'[data-column="{code}"] li.board-column__item .board-tile__title').all_inner_texts()


def centre(locator):
    box = locator.bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def mouse_drag(page, source, target_x, target_y, *, release=True, steps=12):
    x, y = centre(source)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 8, y + 8, steps=2)
    page.mouse.move(target_x, target_y, steps=steps)
    if release:
        page.mouse.up()


def wait_idle(page):
    page.wait_for_function('!document.querySelector(".board-column__item--pending")', timeout=5000)


def column_point(page, code, where='bottom'):
    box = page.locator(f'[data-column="{code}"] [data-column-list]').bounding_box()
    y = box['y'] + (box['height'] - 20 if where == 'bottom' else 10)
    return box['x'] + box['width'] / 2, y


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)

    # ------------------------------------------------------------------ 1–4
    desktop = browser.new_context(viewport={'width': 1920, 'height': 1080})
    page = login(desktop, 'ivanov')
    page.goto(BASE + BOARD)

    # 1a. Between columns: «Альфа» from «Сделать» to the end of «В работе».
    x, y = column_point(page, 'IN_PROGRESS')
    mouse_drag(page, item(page, 'Альфа'), x, y, release=False)
    page.screenshot(path=os.path.join(SHOTS, 'dragging-1920.png'))
    page.mouse.up()
    wait_idle(page)
    check('1a мышь: перенос между колонками виден сразу', 'Альфа' in column_titles(page, 'IN_PROGRESS'))
    counts = page.locator('[data-column="IN_PROGRESS"] [data-column-count]').inner_text()
    check('1a счётчик колонки обновлён из ответа', counts.strip() == '3', counts)

    # 1b. Inside a column: «Гамма» above «Бета» in «Сделать».
    bx, by = centre(item(page, 'Бета'))
    box = item(page, 'Бета').bounding_box()
    mouse_drag(page, item(page, 'Гамма'), bx, box['y'] + 5)
    wait_idle(page)
    check('1b мышь: порядок внутри колонки', column_titles(page, 'TODO') == ['Гамма', 'Бета'], str(column_titles(page, 'TODO')))

    page.reload()
    check('1 после перезагрузки порядок сохранён',
          column_titles(page, 'TODO') == ['Гамма', 'Бета']
          and column_titles(page, 'IN_PROGRESS')[-1] == 'Альфа',
          f"{column_titles(page, 'TODO')} / {column_titles(page, 'IN_PROGRESS')}")

    # 2. A click without movement opens the panel.
    item(page, 'Дельта').locator('a').click()
    page.wait_for_load_state()
    check('2 клик без движения открывает панель', re.search(r'\?card=\d+', page.url) and page.locator('.board-panel').is_visible(), page.url)
    page.goto(BASE + BOARD)

    # 3. «Готово»: the modal, «Отмена» puts it back, «Завершить» completes.
    x, y = column_point(page, 'DONE', 'top')
    mouse_drag(page, item(page, 'Эпсилон'), x, y)
    dialog = page.locator('[data-confirm-modal]')
    check('3 бросок в «Готово» открывает окно', dialog.is_visible())
    page.screenshot(path=os.path.join(SHOTS, 'complete-modal-1920.png'))
    page.click('[data-confirm-modal-cancel]')
    # `close` reaches the dialog's listeners in a task of its own.
    page.wait_for_function(
        "!document.querySelector('[data-column=\"DONE\"] [data-column-list]').textContent.includes('Эпсилон')",
        timeout=3000,
    )
    check('3 подсветка колонки снята', page.locator('.board-column--drop').count() == 0)
    check('3 «Отмена» возвращает плитку', 'Эпсилон' in column_titles(page, 'REVIEW') and 'Эпсилон' not in column_titles(page, 'DONE'))
    mouse_drag(page, item(page, 'Эпсилон'), x, y)
    page.fill('[data-confirm-modal-comment-input]', 'Проверено, замечаний нет')
    with page.expect_navigation():
        page.click('[data-confirm-modal-accept]')
    check('3 «Завершить» завершает задачу', 'Эпсилон' in column_titles(page, 'DONE'), str(column_titles(page, 'DONE')))

    # 4. The server refuses: the task was closed in another tab.
    page.goto(BASE + BOARD)
    other = desktop.new_page()
    other.goto(BASE + BOARD)
    complete_trigger = item(other, 'Закрыть в другой вкладке').locator('[data-card-complete-trigger]')
    url = complete_trigger.get_attribute('data-confirm-url')
    csrf = next(c['value'] for c in desktop.cookies() if c['name'] == 'csrftoken')
    other.request.post(BASE + url, form={'execution_comment': 'Закрыто в другой вкладке'},
                       headers={'X-CSRFToken': csrf, 'Referer': BASE + BOARD})
    other.close()
    before = column_titles(page, 'IN_PROGRESS')
    x, y = column_point(page, 'TODO')
    mouse_drag(page, item(page, 'Закрыть в другой вкладке'), x, y)
    page.wait_for_selector('[data-board-message]:not([hidden])', timeout=5000)
    message = page.locator('[data-board-message]').inner_text()
    check('4 отказ сервера: плитка вернулась', column_titles(page, 'IN_PROGRESS') == before, str(column_titles(page, 'IN_PROGRESS')))
    check('4 отказ сервера: сообщение видно', 'закрыта' in message, message)
    desktop.close()

    # ------------------------------------------------------------------ 5
    phone = browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True, is_mobile=True)
    page = login(phone, 'ivanov')
    page.goto(BASE + BOARD)
    cdp = phone.new_cdp_session(page)

    def touch(kind, x, y):
        points = [] if kind == 'touchEnd' else [{'x': x, 'y': y}]
        cdp.send('Input.dispatchTouchEvent', {'type': kind, 'touchPoints': points})

    # Short swipe on a tile: the row of columns scrolls, nothing is dragged.
    beta = item(page, 'Бета')
    beta.scroll_into_view_if_needed()
    x, y = centre(beta)
    row_before = page.evaluate('document.querySelector("[data-board-columns]").scrollLeft')
    touch('touchStart', x, y)
    for step in range(1, 9):
        touch('touchMove', x - step * 25, y)
        time.sleep(0.01)
    touch('touchEnd', x - 200, y)
    time.sleep(0.4)
    row_after = page.evaluate('document.querySelector("[data-board-columns]").scrollLeft')
    check('5 короткий свайп прокручивает ряд колонок', row_after > row_before, f'{row_before} → {row_after}')
    check('5 короткий свайп ничего не тащит', column_titles(page, 'TODO') == ['Гамма', 'Бета'], str(column_titles(page, 'TODO')))
    page.evaluate('document.querySelector("[data-board-columns]").scrollLeft = 0')

    # Long press, then move within «Сделать»: «Бета» above «Гамма».
    beta = item(page, 'Бета')
    gamma_box = item(page, 'Гамма').bounding_box()
    x, y = centre(beta)
    touch('touchStart', x, y)
    time.sleep(0.45)
    check('5 долгое нажатие берёт плитку', page.locator('.board-ghost').count() == 1)
    target_y = gamma_box['y'] + 4
    for step in range(1, 11):
        touch('touchMove', x, y + (target_y - y) * step / 10)
        time.sleep(0.02)
    touch('touchEnd', x, target_y)
    wait_idle(page)
    page.reload()
    check('5 касание: перенос сохранён', column_titles(page, 'TODO') == ['Бета', 'Гамма'], str(column_titles(page, 'TODO')))
    phone.close()

    # ------------------------------------------------------------------ 6
    reader = browser.new_context(viewport={'width': 1920, 'height': 1080})
    page = login(reader, 'reader')
    page.goto(BASE + BOARD)
    check('6 читатель: перетаскиваемых плиток нет', page.locator('[data-card-movable]').count() == 0)
    before = column_titles(page, 'TODO')
    x, y = column_point(page, 'REVIEW')
    mouse_drag(page, item(page, 'Бета'), x, y)
    time.sleep(0.3)
    check('6 читатель: плитка не тащится', page.locator('.board-ghost').count() == 0 and column_titles(page, 'TODO') == before)
    reader.close()

    browser.close()

failures = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failures} из {len(results)} проверок прошли')
sys.exit(failures)
