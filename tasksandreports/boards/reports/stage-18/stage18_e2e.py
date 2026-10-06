"""Browser check of stage 18: the checklist, «Следить» and «@» in «Чат».

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-18/stage18_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it — the steps unticked again,
nobody following, no message — since the first round changes all three.

Two rounds, 1920×1080 and 1536×864 (125 % Windows scaling of the same
monitor), each:
1. `admin1` opens ZAP-1: «Чек-лист 2/5» under the description, five items,
   two struck through, «☑ 2/5» on the tile; `ivanov` ticks the third in his
   own browser — no reload — and `admin1` sees «3/5» in the panel and on the
   tile without a reload; then `admin1` opens the card's edit form and types
   in it, `ivanov` ticks the fourth: «4/5» arrives under the form and no
   «Карточка изменена» banner is raised, the typed title stays;
2. «Следить» in the panel heading → «Вы следите», and «Подписчики» on
   «Описание» with the avatar;
3. «@» in «Чат» (`ivanov`): the list of readers, «Мар» narrows it to «Мария
   Петрова», Enter writes the name, «Отправить»; `petrova` gets «Вас упомянули
   в карточке ZAP-1…» in the bell, the link opens the card on «Чат», her name
   highlighted;
4. «Таблица» with the «Чек-лист» column («4/5» for ZAP-1).
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
CARD = BOARD + '?card=1'
SIZES = ((1920, 1080), (1536, 864))

results = []
problems = []
# Leaving the edit form with typed text is the browser's own leave prompt
# (`unsaved_guard.js`) — expected exactly once per round, and accepted.
expected_leave = {'count': 0}


def on_dialog(username, dialog):
    if dialog.type == 'beforeunload' and expected_leave['count'] > 0:
        expected_leave['count'] -= 1
        dialog.accept()
        return
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


def open_card(page, query=''):
    page.goto(BASE + CARD + query)
    page.wait_for_load_state('networkidle')
    # The live client is up once it has synced: wait for its first request.
    page.wait_for_timeout(1200)


def panel_count(page):
    count = page.locator('[data-live-board-checklist] [data-checklist-count]')
    return count.inner_text().strip() if count.count() else ''


def tile_count(page):
    tile = page.locator('[data-card-id="1"] [data-tile-checklist]')
    return tile.inner_text().strip() if tile.count() else ''


def wait_for(page, predicate, timeout=8000):
    """Poll `predicate(page)` until it holds — what arrives over SSE takes a moment."""
    waited = 0
    while waited < timeout:
        if predicate(page):
            return True
        page.wait_for_timeout(250)
        waited += 250
    return predicate(page)


def tick(page, index):
    """Tick the `index`-th item (from 0) in this browser — `board_checklist.js`."""
    button = page.locator('[data-live-board-checklist] .board-checklist__item').nth(index).locator('button.board-checklist__check')
    url = page.url
    button.click()
    page.wait_for_timeout(600)
    return page.url == url


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True, stdout=subprocess.DEVNULL)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)

    # 1. The checklist, and a tick from another browser.
    open_card(admin)
    items = admin.locator('[data-live-board-checklist] .board-checklist__item')
    check('«Чек-лист 2/5» in the panel', panel_count(admin) == '2/5', panel_count(admin))
    check('five items, two struck through', items.count() == 5
          and admin.locator('[data-live-board-checklist] .board-checklist__item.is-done').count() == 2)
    check('«☑ 2/5» on the tile', tile_count(admin) == '☑ 2/5', tile_count(admin))
    first = items.nth(0).get_attribute('title') or ''
    check('who ticked it is on the item', first.startswith('Отметил Иван Иванов'), first)
    description = admin.locator('.board-drawer__description').bounding_box()
    checklist = admin.locator('.board-checklist').bounding_box()
    facts = admin.locator('.board-drawer__facts').bounding_box()
    check('the checklist sits right under the description, above the facts',
          description['y'] < checklist['y'] < facts['y'], (description['y'], checklist['y'], facts['y']))
    wide = admin.evaluate('document.documentElement.scrollWidth > window.innerWidth')
    check('no sideways scroll of the page', not wide)
    shot(admin, '1-checklist', size)
    admin.locator('[data-card-id="1"]').screenshot(path=os.path.join(SHOTS, f'1-checklist-tile-{size[0]}.png'))

    open_card(ivanov)
    check('ivanov ticks the third without a reload', tick(ivanov, 2))
    check('ivanov: «3/5» at once', panel_count(ivanov) == '3/5' and tile_count(ivanov) == '☑ 3/5',
          (panel_count(ivanov), tile_count(ivanov)))
    arrived = wait_for(admin, lambda p: panel_count(p) == '3/5' and tile_count(p) == '☑ 3/5')
    check('admin1 sees «3/5» in the panel and on the tile without a reload', arrived,
          (panel_count(admin), tile_count(admin)))
    shot(admin, '1-checklist-live', size)

    # The card's edit form open and typed in: a tick is no conflict.
    open_card(admin, '&edit=1')
    title = admin.locator('.board-card-form input[name=title]')
    title.fill('Согласовать спецификацию — уточнение')
    check('ivanov ticks the fourth', tick(ivanov, 3))
    arrived = wait_for(admin, lambda p: panel_count(p) == '4/5' and tile_count(p) == '☑ 4/5')
    check('under the open edit form «4/5» arrives', arrived, (panel_count(admin), tile_count(admin)))
    banner = admin.locator('[data-board-conflict-banner]')
    check('no «Карточка изменена» banner', banner.is_hidden())
    check('the typed title stays', title.input_value() == 'Согласовать спецификацию — уточнение')
    # The list sits under the form: scrolled to, for the picture.
    admin.locator('[data-live-board-checklist]').scroll_into_view_if_needed()
    shot(admin, '1-checklist-edit-form', size)

    # 2. «Следить». Leaving the typed form: the browser asks, and we leave.
    expected_leave['count'] = 1
    open_card(admin)
    check('the leave prompt was the unsaved guard\'s, once', expected_leave['count'] == 0)
    follow = admin.locator('form.board-follow button')
    check('«Следить» in the heading', follow.inner_text().strip() == 'Следить', follow.inner_text())
    with admin.expect_navigation():
        follow.click()
    admin.wait_for_load_state('networkidle')
    follow = admin.locator('form.board-follow button')
    check('now «Вы следите»', follow.inner_text().strip() == 'Вы следите', follow.inner_text())
    subscribers = admin.locator('.board-drawer__facts div', has=admin.locator('dt', has_text='Подписчики'))
    check('«Подписчики» on «Описание» with the avatar «ОА»',
          subscribers.count() == 1 and 'ОА' in subscribers.inner_text(), subscribers.all_inner_texts())
    shot(admin, '2-follow', size)

    # 3. «@» in «Чат».
    open_card(ivanov, '&tab=chat')
    text = ivanov.locator('[data-mention-input]')
    check('«Упомянуть» is replaced by «@» with JavaScript', ivanov.locator('[data-mention-fallback]').is_hidden())
    text.click()
    text.type('Мария, посмотрите сроки, пожалуйста: @')
    options = ivanov.locator('[data-mention-option]')
    names = [option.inner_text().strip() for option in options.all()]
    check('«@» lists the board\'s readers', 'Мария Петрова' in names and 'Олег Админов' in names
          and 'Иван Иванов' not in names, names)
    text.type('Мар')
    names = [option.inner_text().strip() for option in ivanov.locator('[data-mention-option]').all()]
    check('«@Мар» narrows it to Мария Петрова', names == ['Мария Петрова'], names)
    shot(ivanov, '3-mention-list', size)
    text.press('Enter')
    value = text.input_value()
    check('Enter writes «@Мария Петрова »', value.endswith('@Мария Петрова '), value)
    hidden = ivanov.locator('form[data-board-mentions] input[type=hidden][name=mention]')
    check('and one hidden mention', hidden.count() == 1)
    with ivanov.expect_navigation():
        ivanov.locator('form[data-board-mentions] button[type=submit]').click()
    ivanov.wait_for_load_state('networkidle')
    check('the chat after sending', 'tab=chat' in ivanov.url, ivanov.url)
    mention = ivanov.locator('[data-live-board-comments] .board-mention')
    check('the name is highlighted in the message', mention.count() >= 1 and mention.last.inner_text() == '@Мария Петрова')

    petrova = login(browser, 'petrova', size)
    petrova.goto(BASE + '/')
    petrova.wait_for_load_state('networkidle')
    petrova.locator('[data-notification-summary]').click()
    item = petrova.locator('.notification-menu__item', has_text='Вас упомянули')
    check('petrova: «Вас упомянули в карточке ZAP-1 на доске «Запуск заказов»» in the bell',
          item.count() >= 1 and 'Вас упомянули в карточке ZAP-1 на доске «Запуск заказов»' in item.first.inner_text(),
          item.all_inner_texts())
    shot(petrova, '3-mention-bell', size)
    with petrova.expect_navigation():
        item.first.click()
    petrova.wait_for_load_state('networkidle')
    check('the link opens the card on «Чат»', 'card=1' in petrova.url and 'tab=chat' in petrova.url, petrova.url)
    check('the drawer shows «Чат»', petrova.locator('[data-board-drawer]').get_attribute('data-board-tab') == 'chat')
    highlighted = petrova.locator('[data-live-board-comments] .board-mention')
    check('her name highlighted', highlighted.count() >= 1 and highlighted.last.inner_text() == '@Мария Петрова')
    follow = petrova.locator('form.board-follow button')
    check('mentioned → she follows the card', follow.inner_text().strip() == 'Вы следите', follow.inner_text())
    petrova.wait_for_timeout(600)
    shot(petrova, '3-mention-chat', size)

    # 4. «Таблица» with «Чек-лист».
    admin.goto(BASE + BOARD + '?view=table')
    admin.wait_for_load_state('networkidle')
    headers = admin.eval_on_selector_all(
        '.board-table thead th', 'n => n.map(e => e.textContent.replace(/[↑↓]/g, "").trim())',
    )
    check('«Чек-лист» after «В колонке»', headers[headers.index('В колонке') + 1] == 'Чек-лист', headers)
    column = headers.index('Чек-лист')
    rows = admin.evaluate("""(column) => Object.fromEntries([...document.querySelectorAll('.board-table tbody tr')].map((tr) =>
        [tr.children[0].textContent.trim(), tr.children[column].textContent.trim()]))""", column)
    check('ZAP-1 «4/5», the others «—»', rows.get('ZAP-1') == '4/5'
          and all(value == '—' for code, value in rows.items() if code != 'ZAP-1'), rows)
    shot(admin, '4-table', size)
    for page in (admin, ivanov, petrova):
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
