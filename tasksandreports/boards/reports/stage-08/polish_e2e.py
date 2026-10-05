"""Browser check of stage 8: a message keeps the unsaved «Выполнение», and a
long «Обсуждение» shows its newest 100 messages.

Needs Redis and a real-time server on the data of `seed_demo.py` (the ASGI
wrapper that adds static files is stage 5's):

    redis-server --port 6379
    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
        python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-08/polish_e2e.py [screenshot-dir]

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
    page.on('dialog', lambda d: (console_errors.append(f'{username}: browser dialog {d.type}'), d.dismiss()))
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


def card_id(page, title):
    return page.locator(
        f'li.board-column__item:has(.board-tile__title:text-is("{title}"))'
    ).get_attribute('data-card-id')


def shown(page):
    return page.locator('[data-live-board-comments] .board-discussion__message').count()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    one = browser.new_context(viewport={'width': 1920, 'height': 1080})
    two = browser.new_context(viewport={'width': 1920, 'height': 1080})
    ivanov = login(one, 'ivanov')
    sidorova = login(two, 'sidorova')

    ivanov.goto(BASE + BOARD)
    card = card_id(ivanov, 'Согласовать график')
    long = card_id(ivanov, 'Долгое обсуждение')

    # ------------------------------------------------------------------ 1
    ivanov.goto(f'{BASE}{BOARD}?card={card}&mine=1')
    execution = 'Согласовал с цехом, жду подписи'
    ivanov.click('#task-execution-comment')
    ivanov.keyboard.type(execution)
    ivanov.click('#board-comment-text')
    ivanov.keyboard.type('Вопрос: подпись нужна до пятницы?')
    with ivanov.expect_navigation():
        ivanov.click('.board-discussion__form button[type=submit]')
    check('1 сообщение отправлено кнопкой',
          'подпись нужна до пятницы' in ivanov.locator('[data-live-board-comments]').inner_text())
    check('1 «Выполнение» вернулось в поле', ivanov.input_value('#task-execution-comment') == execution,
          ivanov.input_value('#task-execution-comment'))
    check('1 фильтр доски сохранён', 'mine=1' in ivanov.url, ivanov.url)

    # ------------------------------------------------------------------ 2
    ivanov.fill('#task-execution-comment', execution + ' — подписано')
    ivanov.click('#board-comment-text')
    ivanov.keyboard.type('Подписали, закрываю')
    with ivanov.expect_navigation():
        ivanov.keyboard.press('Control+Enter')
    check('2 Ctrl+Enter: «Выполнение» на месте',
          ivanov.input_value('#task-execution-comment') == execution + ' — подписано')
    ivanov.reload()
    check('2 черновик показан один раз', ivanov.input_value('#task-execution-comment') == '')

    # ------------------------------------------------------------------ 3
    ivanov.goto(f'{BASE}{BOARD}?card={long}')
    wait_live(ivanov)
    texts = ivanov.locator('[data-live-board-comments] .board-discussion__text').all_inner_texts()
    check('3 показаны последние 100', len(texts) == 100 and texts[0] == 'Сообщение 006'
          and texts[-1] == 'Сообщение 105', f'{len(texts)}: {texts[0]}…{texts[-1]}')
    link = ivanov.locator('.board-discussion__earlier a')
    check('3 ссылка «Показать ранние (5)»', link.inner_text() == 'Показать ранние (5)', link.inner_text())
    ivanov.evaluate("document.querySelectorAll('.toast').forEach((toast) => toast.remove())")
    ivanov.evaluate("document.querySelector('[data-live-board-comments]').scrollTop = 0")
    ivanov.locator('.board-discussion').screenshot(path=os.path.join(SHOTS, 'discussion-earlier.png'))
    with ivanov.expect_navigation():
        link.click()
    check('3 по ссылке — все 105', shown(ivanov) == 105, str(shown(ivanov)))
    check('3 адрес несёт comments=all', 'comments=all' in ivanov.url, ivanov.url)

    # ------------------------------------------------------------------ 4
    wait_live(ivanov)
    ivanov.evaluate('window.__sameDocument = true')
    sidorova.goto(f'{BASE}{BOARD}?card={long}')
    sidorova.fill('#board-comment-text', 'Сообщение 106')
    with sidorova.expect_navigation():
        sidorova.click('.board-discussion__form button[type=submit]')
    ivanov.wait_for_function(
        """() => document.querySelectorAll('[data-live-board-comments] .board-discussion__message').length === 106""",
        timeout=LIVE_WAIT,
    )
    check('4 живое обновление при comments=all — все 106',
          shown(ivanov) == 106 and ivanov.evaluate('window.__sameDocument === true'))
    check('4 у Сидоровой — последние 100 и «ранние (6)»',
          shown(sidorova) == 100
          and sidorova.locator('.board-discussion__earlier a').inner_text() == 'Показать ранние (6)')

    one.close()
    two.close()
    browser.close()

relevant = [line for line in console_errors if 'favicon' not in line]
check('ошибок в консоли и браузерных диалогов нет', not relevant, '; '.join(relevant))

failures = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failures} из {len(results)} проверок прошли')
sys.exit(failures)
