"""Browser check of stage 7: «Обсуждение» in the card panel, live, and the
cancellation notice.

Needs Redis and a real-time server on the data of `seed_demo.py` (the ASGI
wrapper that adds static files is stage 5's):

    redis-server --port 6379
    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
        python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-07/discussion_e2e.py [screenshot-dir]

Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/'
LIVE_WAIT = 10_000

results = []
console_errors = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def login(context, username):
    page = context.new_page()
    page.on('console', lambda m: console_errors.append(f'{username}: {m.text}') if m.type == 'error' else None)
    page.on('pageerror', lambda e: console_errors.append(f'{username}: {e}'))
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def wait_live(page):
    page.wait_for_function(
        "window.QualityRealtime && window.QualityRealtime.state === 'live'", timeout=LIVE_WAIT,
    )


def item(page, title):
    return page.locator(f'li.board-column__item:has(.board-tile__title:text-is("{title}"))')


def card_id(page, title):
    return item(page, title).get_attribute('data-card-id')


def bell(page):
    counter = page.locator('[data-notification-counter]')
    return 0 if counter.is_hidden() else int(counter.inner_text().strip() or 0)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    one = browser.new_context(viewport={'width': 1920, 'height': 1080})
    two = browser.new_context(viewport={'width': 1920, 'height': 1080})
    ivanov = login(one, 'ivanov')
    sidorova = login(two, 'sidorova')

    ivanov.goto(BASE + BOARD)
    card = card_id(ivanov, 'Согласовать график')
    ivanov.goto(f'{BASE}{BOARD}?card={card}')
    ivanov.evaluate('window.__sameDocument = true')
    wait_live(ivanov)
    bell_before = bell(ivanov)

    # Иванов is in the middle of two texts when Сидорова writes.
    execution = 'Согласовал с цехом, жду подписи'
    own = 'Моё сообщение, ещё не отправлено'
    ivanov.click('#task-execution-comment')
    ivanov.keyboard.type(execution)
    ivanov.click('#board-comment-text')
    ivanov.keyboard.type(own)

    sidorova.goto(f'{BASE}{BOARD}?card={card}')
    sidorova.fill('#board-comment-text', 'Иван, график нужен до пятницы')
    with sidorova.expect_navigation():
        sidorova.click('.board-discussion__form button[type=submit]')
    check('0 у Сидоровой сообщение в обсуждении после отправки',
          'Иван, график нужен до пятницы' in sidorova.locator('[data-live-board-comments]').inner_text())

    # ------------------------------------------------------------------ 1
    ivanov.wait_for_function(
        """() => (document.querySelector('[data-live-board-comments]') || {}).textContent
                 .includes('график нужен до пятницы')""",
        timeout=LIVE_WAIT,
    )
    check('1 сообщение появилось в открытой панели без перезагрузки',
          ivanov.evaluate('window.__sameDocument === true'))
    ivanov.wait_for_selector(f'li[data-card-id="{card}"] .board-tile__comments', timeout=LIVE_WAIT)
    counter = item(ivanov, 'Согласовать график').locator('.board-tile__comments').inner_text().strip()
    check('1 счётчик на плитке вырос', counter == '1', counter)

    # ------------------------------------------------------------------ 2
    check('2 текст «Выполнения» на месте', ivanov.input_value('#task-execution-comment') == execution)
    check('2 своё сообщение на месте', ivanov.input_value('#board-comment-text') == own)
    check('2 баннера конфликта нет', ivanov.locator('[data-board-conflict-banner]').is_hidden())

    # ------------------------------------------------------------------ 3
    ivanov.wait_for_function(
        f"""() => {{
            const counter = document.querySelector('[data-notification-counter]');
            return counter && !counter.hidden && Number(counter.textContent) > {bell_before};
        }}""",
        timeout=LIVE_WAIT,
    )
    check('3 колокольчик у Иванова', bell(ivanov) == bell_before + 1, f'{bell_before} → {bell(ivanov)}')
    # The bell's toast would cover the panel in the picture.
    ivanov.evaluate("document.querySelectorAll('.toast').forEach((toast) => toast.remove())")
    ivanov.locator('.board-discussion').screenshot(path=os.path.join(SHOTS, 'panel-discussion.png'))
    item(ivanov, 'Согласовать график').screenshot(path=os.path.join(SHOTS, 'tile-counter.png'))

    # Иванов sends his own message: it lands, and his «Выполнение» is kept by
    # the browser's own leave-page prompt — dismissed here by clearing it.
    ivanov.evaluate("document.querySelector('#task-execution-comment').value = ''")
    ivanov.evaluate('window.qualityUnsavedGuard.markClean()')
    with ivanov.expect_navigation():
        ivanov.click('.board-discussion__form button[type=submit]')
    messages = ivanov.locator('[data-live-board-comments] .board-discussion__message').count()
    check('3 своё сообщение отправлено формой', messages == 2, str(messages))

    # ------------------------------------------------------------------ 4
    wait_live(ivanov)
    bell_before = bell(ivanov)
    extra = card_id(sidorova, 'Лишняя карточка')
    sidorova.goto(f'{BASE}{BOARD}?card={extra}')
    sidorova.click('button:has-text("Отменить карточку")')
    sidorova.fill('[data-confirm-modal-comment-input]', 'Дубль')
    with sidorova.expect_navigation():
        sidorova.click('[data-confirm-modal-accept]')
    ivanov.wait_for_function(
        f"""() => {{
            const counter = document.querySelector('[data-notification-counter]');
            return counter && !counter.hidden && Number(counter.textContent) > {bell_before};
        }}""",
        timeout=LIVE_WAIT,
    )
    ivanov.goto(BASE + '/notifications/')
    content = ivanov.content()
    check('4 исполнитель получил уведомление об отмене',
          'Карточка отменена на доске' in content and 'Дубль' not in content)
    sidorova.goto(BASE + '/notifications/')
    check('4 отменившая уведомления не получила', 'Карточка отменена' not in sidorova.content())

    one.close()
    two.close()
    browser.close()

relevant = [line for line in console_errors if 'favicon' not in line]
check('ошибок в консоли нет', not relevant, '; '.join(relevant))

failures = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failures} из {len(results)} проверок прошли')
sys.exit(failures)
