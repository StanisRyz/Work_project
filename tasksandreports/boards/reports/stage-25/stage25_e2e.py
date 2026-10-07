"""Browser check of stage 25: «Правила при входе», «Передать дальше», «📌», «Списком».

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-25/stage25_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it (and restarts the server).
The browser runs in Russian (`--lang=ru-RU`, `LANG`, and the context's
`locale='ru-RU'`), so date fields read ДД.ММ.ГГГГ as the plant's browsers
show them.

Two rounds, 1920×1080 and 1536×864, each:
1. `admin1`, the owner of «Запуск заказов» (ZAP), opens «⋯» of the column
   «Запуск в работу» → «⚙ Правила при входе», adds a three-line checklist
   template and «Приоритет: Высокий»; `ivanov` moves ZAP-7 (no priority yet)
   there with «Переместить в…» — its «Чек-лист» has the three lines,
   «Приоритет» reads «Высокий», «Лог» says «Правила колонки …», the header
   shows «⚙»;
2. `admin1` adds the action «Передать в ПДО» (that column, «Заменить
   исполнителей» with `petrova`, a message with «{код}»); `ivanov` presses it
   on ZAP-2: the confirmation «Передать ZAP-2 в «Запуск в работу»?», then the
   card stands in that column, `petrova` is its исполнитель, «Чат» holds the
   message, the column's template lines are on its «Чек-лист» while its
   «Приоритет: Средний» stays (a rule sets, never resets), and `petrova`'s
   bell says «Назначена карточка ZAP-2»;
3. `ivanov` pins ZAP-8 — it stands first in «Сделать», with «📌»;
4. `ivanov` creates three cards at once with «Списком».
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import re
import subprocess
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
RESET = os.environ.get('RESET', '')
BOARD = '/work/boards/1/1/'
LAUNCH = 3          # «Запуск в работу» (the seed renamed «На проверке»)
TODO = 1            # «Сделать»
PRIORITY_FIELD = 4  # «Приоритет», a list
SIZES = ((1920, 1080), (1536, 864))

results = []
problems = []


def on_dialog(username, dialog):
    problems.append(f'{username}: dialog {dialog.type}')
    dialog.dismiss()


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + str(detail)) if detail else ''}")


def login(browser, username, size):
    # The plant's browsers are Russian: dates read ДД.ММ.ГГГГ (review 24).
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]}, locale='ru-RU')
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


def column_titles(page, column_id):
    return [
        ' '.join(title.split())
        for title in page.locator(f'[data-column-id="{column_id}"] .board-tile__title').all_inner_texts()
    ]


def open_card(page, card_id, tab=''):
    page.goto(f'{BASE}{BOARD}?card={card_id}' + (f'&tab={tab}' if tab else ''))
    settle(page)


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True, capture_output=True)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)
    petrova = login(browser, 'petrova', size)

    # 1. «Правила при входе» of «Запуск в работу».
    admin.goto(BASE + BOARD)
    settle(admin)
    check('no «⚙» on the column before any rule',
          admin.locator(f'[data-column-id="{LAUNCH}"] .board-column__rules').count() == 0)
    admin.locator(f'[data-column-id="{LAUNCH}"] .board-column__header details.board-menu summary').click()
    admin.locator(f'[data-column-id="{LAUNCH}"] a', has_text='Правила при входе').click()
    settle(admin)
    check('the column\'s rules page', 'Правила при входе · Запуск в работу' in text(admin, 'h1'))
    for line in ('Проверить КД и спецификацию', 'Заказать материал', 'Выдать задание в цех'):
        admin.fill('#board-template-text', line)
        with admin.expect_navigation():
            admin.locator('.board-rules__add button[type=submit]').click()
        settle(admin)
    check('three template lines', admin.locator('.board-rules__item').count() == 3,
          admin.locator('.board-rules__item').count())
    admin.select_option(f'select[name=rule_{PRIORITY_FIELD}]', label='● Высокий')
    with admin.expect_navigation():
        admin.locator('#fields button[type=submit]').click()
    settle(admin)
    check('«Значения полей сохранены»', 'Значения полей сохранены' in admin.content())
    check('no sideways scroll on the rules page', no_sideways_scroll(admin))
    admin.locator('#template').scroll_into_view_if_needed()
    shot(admin, '1-column-rules', size)

    open_card(ivanov, 8)
    check('ZAP-7 has no priority yet', 'Приоритет' not in text(ivanov, '[data-live-board-facts]'))
    ivanov.select_option('[data-live-board-facts] select[name=column_id]', str(LAUNCH))
    with ivanov.expect_navigation():
        ivanov.locator('[data-live-board-facts] .board-drawer__move button[type=submit]').click()
    settle(ivanov)
    items = [' '.join(t.split()) for t in ivanov.locator('[data-live-board-checklist] .board-checklist__text').all_inner_texts()]
    check('ZAP-7 moved in: its «Чек-лист» has the three lines',
          items[-3:] == ['Проверить КД и спецификацию', 'Заказать материал', 'Выдать задание в цех'], items)
    facts = text(ivanov, '[data-live-board-facts]')
    check('…and «Приоритет: Высокий»', 'Высокий' in facts, facts[:200])
    check('«⚙» in the column header', ivanov.locator(f'[data-column-id="{LAUNCH}"] .board-column__rules').count() == 1)
    shot(ivanov, '1-card-after-entry', size)
    ivanov.locator('.board-drawer__tabs a', has_text='Лог').first.click()
    ivanov.wait_for_timeout(300)
    log = text(ivanov, '[data-live-board-log]')
    check('«Лог»: one entry «Правила колонки «Запуск в работу»: Приоритет, чек-лист +3»',
          log.count('Правила колонки «Запуск в работу»: Приоритет, чек-лист +3') == 1, log[:200])

    # 2. «Передать в ПДО».
    admin.goto(BASE + BOARD)
    settle(admin)
    admin.locator('details.board-head__menu summary').click()
    admin.locator('.board-head__menu a', has_text='Действия').click()
    settle(admin)
    admin.locator('a', has_text='+ Действие').first.click()
    settle(admin)
    admin.fill('#action-form input[name=name]', 'Передать в ПДО')
    admin.select_option('#action-form select[name=target_column]', str(LAUNCH))
    admin.check('#action-form input[name=assignee_mode][value=REPLACE]')
    admin.locator('#action-form label', has_text='Петрова').locator('input[name=assignees]').check()
    admin.fill('#action-form textarea[name=message_template]', 'Передано в ПДО: {код} → «{колонка}». Проверьте комплект КД.')
    shot(admin, '2-action-form', size)
    with admin.expect_navigation():
        admin.locator('#action-form button[type=submit]').click()
    settle(admin)
    check('the action is listed', 'Передать в ПДО' in text(admin, '.board-fields'))
    check('no sideways scroll on «Действия»', no_sideways_scroll(admin))
    shot(admin, '2-actions', size)

    open_card(ivanov, 2)
    button = ivanov.locator('[data-board-actions] button', has_text='Передать в ПДО')
    check('the button in the card panel\'s heading', button.count() == 1)
    button.click()
    ivanov.wait_for_timeout(300)
    title = text(ivanov, '[data-confirm-modal-title]')
    check('the shared modal: «Передать ZAP-2 в «Запуск в работу»?»', title == 'Передать ZAP-2 в «Запуск в работу»?', title)
    shot(ivanov, '2-confirm', size)
    with ivanov.expect_navigation():
        ivanov.locator('[data-confirm-modal-accept]').click()
    settle(ivanov)
    check('ZAP-2 stands in «Запуск в работу»',
          ivanov.locator(f'[data-column-id="{LAUNCH}"] [data-card-id="2"]').count() == 1)
    facts = text(ivanov, '[data-live-board-facts]')
    check('…petrova is its исполнитель', 'Петрова' in facts and 'Иван Иванов' not in facts.split('Срок')[0], facts[:200])
    check('…the column\'s entry rules ran: the template is on its «Чек-лист»',
          'Выдать задание в цех' in text(ivanov, '[data-live-board-checklist]'))
    check('…and «Приоритет: Средний» stays — a rule sets, never resets', 'Средний' in facts, facts[:300])
    check('…and no button leading where it stands',
          ivanov.locator('[data-board-actions] button', has_text='Передать в ПДО').count() == 0)
    ivanov.locator('.board-drawer__tabs a', has_text='Чат').first.click()
    ivanov.wait_for_timeout(300)
    chat = text(ivanov, '[data-live-board-comments]')
    check('«Чат»: the message, with the code and the column',
          'Передано в ПДО: ZAP-2 → «Запуск в работу»' in chat, chat[:160])
    shot(ivanov, '2-card-moved-chat', size)
    entries = bell(petrova, 'ZAP-2')
    check('petrova\'s bell: «Назначена карточка ZAP-2»', any('Назначена карточка ZAP-2' in e for e in entries), entries)
    shot(petrova, '2-petrova-bell', size)
    petrova.keyboard.press('Escape')

    # 3. «📌 Закрепить».
    open_card(ivanov, 9)
    with ivanov.expect_navigation():
        ivanov.locator('.board-drawer__pin button').click()
    settle(ivanov)
    titles = column_titles(ivanov, TODO)
    check('ZAP-8 first in «Сделать»', titles and titles[0] == 'Заказать оснастку', titles)
    check('…with «📌»', ivanov.locator(f'[data-column-id="{TODO}"] [data-card-id="9"] .board-tile__pinned').count() == 1)
    shot(ivanov, '3-pinned-first', size)

    # 4. «Списком».
    ivanov.goto(f'{BASE}{BOARD}?new={TODO}')
    settle(ivanov)
    ivanov.locator('[data-board-list-create] summary').click()
    ivanov.fill('[data-board-list-create] textarea[name=text]', 'Заказ 3-1701: кронштейн\n\nЗаказ 3-1702: корпус\nЗаказ 3-1703: крышка\n')
    due = ivanov.input_value('[data-board-list-create] input[name=due_date]')
    check('«Срок» filled, ISO in the value', bool(re.match(r'\d{4}-\d{2}-\d{2}$', due)), due)
    check('the browser speaks Russian', ivanov.evaluate('navigator.language') == 'ru-RU')
    shot(ivanov, '4-list-form', size)
    with ivanov.expect_navigation():
        ivanov.locator('[data-board-list-create] button[type=submit]').click()
    settle(ivanov)
    titles = column_titles(ivanov, TODO)
    check('three new cards in «Сделать», in order, after the pinned one',
          [t for t in titles if t.startswith('Заказ 3-17')] == ['Заказ 3-1701: кронштейн', 'Заказ 3-1702: корпус', 'Заказ 3-1703: крышка']
          and titles[0] == 'Заказать оснастку', titles)
    check('«Создано 3 карточки»', 'Создано 3 карточки' in ivanov.content())
    shot(ivanov, '4-list-created', size)
    for page in (admin, ivanov, petrova):
        page.context.close()


with sync_playwright() as playwright:
    # Russian through and through (review 24): the context's `locale` alone
    # leaves a date field «mm/dd/yyyy» — the field's format follows the
    # browser process's own language, so `--lang` and `LANG` as well.
    browser = playwright.chromium.launch(
        executable_path=CHROMIUM,
        args=['--lang=ru-RU'],
        env={**os.environ, 'LANG': 'ru_RU.UTF-8', 'LANGUAGE': 'ru'},
    )
    for size in SIZES:
        run_round(browser, size)
    browser.close()

check('no JavaScript errors and no browser dialogs', not problems, problems)
failed = [name for name, ok in results if not ok]
print(f'\n{len(results) - len(failed)}/{len(results)} PASS')
sys.exit(len(failed))
