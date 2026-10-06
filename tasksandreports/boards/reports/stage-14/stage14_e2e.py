"""Browser check of stage 14: card numbers, pinned people, moving between sub-boards.

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-14/stage14_e2e.py [screenshot-dir]

Every scenario runs at 1920×1080 and at 1536×864 (125 % Windows scaling of
the same monitor), each on cards of its own, so the seeded data is used once.
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/1/'
# «Цех ПиР»: sub-board 2 is the one of «Отгрузки», created before it.
SECOND_TAB = '/work/boards/1/3/'
# Per size: the card dragged into the pinned column, and the card moved to «Цех ПиР».
ROUNDS = (
    ((1920, 1080), {'drag': 'ZAP-1', 'cross': 'ZAP-4', 'cross_to': 'Сделать', 'code': 'n19'}),
    ((1536, 864), {'drag': 'ZAP-2', 'cross': 'ZAP-3', 'cross_to': 'В работе', 'code': 'n15'}),
)

results = []
problems = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + str(detail)) if detail else ''}")


def login(browser, username, size):
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]})
    context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=BASE)
    page = context.new_page()
    page.on('pageerror', lambda e: problems.append(f'{username}: {e}'))
    page.on('dialog', lambda d: (problems.append(f'{username}: dialog {d.type}'), d.dismiss()))
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def tile(page, code):
    return page.locator('.board-column__item', has=page.locator('.board-tile__number', has_text=code))


def tile_codes(page):
    return page.eval_on_selector_all('.board-tile__number', 'n => n.map(e => e.textContent.trim())')


def column_of_tile(page, code):
    return page.evaluate("""(code) => {
        const number = [...document.querySelectorAll('.board-tile__number')].find(e => e.textContent.trim() === code);
        return number ? number.closest('[data-column-id]').querySelector('h2').textContent.trim() : null;
    }""", code)


def open_card(page, code):
    tile(page, code).locator('.board-tile').click()
    page.wait_for_selector('[data-board-drawer]:not([hidden]) .board-drawer__code')
    page.wait_for_timeout(300)


def centre(locator):
    box = locator.bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def drag_to_column(page, code, column_name):
    source = tile(page, code).locator('.board-tile')
    target = page.locator('.board-column', has=page.locator('h2', has_text=column_name)).locator('[data-column-list]')
    x, y = centre(source)
    tx, ty = centre(target)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 8, y + 8, steps=2)
    page.mouse.move(tx, ty, steps=14)
    page.mouse.up()
    page.wait_for_function("() => document.querySelector('[data-board]').getAttribute('data-board-busy') === null")
    page.wait_for_timeout(900)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)

    for size, cards in ROUNDS:
        tag = f'{size[0]}'
        admin = login(browser, 'admin1', size)
        admin.evaluate('localStorage.clear()')

        # ------------------------------------------------ 1. a new board with its code
        admin.goto(BASE + '/work/boards/create/')
        admin.fill('input[name=name]', f'Проверка {tag}')
        admin.fill('input[name=code]', cards['code'])
        admin.screenshot(path=os.path.join(SHOTS, f'1-new-board-form-{tag}.png'))
        admin.click('button[type=submit]:has-text("Создать доску")')
        admin.wait_for_load_state()
        badge = admin.locator('.board-head__code').inner_text().strip()
        check(f'{tag} 1 доска создана, код в верхнем регистре', badge == cards['code'].upper(), badge)
        admin.goto(BASE + '/work/boards/create/')
        admin.fill('input[name=name]', 'Дубль')
        admin.fill('input[name=code]', 'zap')
        admin.click('button[type=submit]:has-text("Создать доску")')
        admin.wait_for_load_state()
        check(f'{tag} 1 занятый код — ошибка у поля',
              admin.locator('.board-form__code', has_text='Код «ZAP» уже занят другой доской.').count() == 1)
        admin.screenshot(path=os.path.join(SHOTS, f'1-new-board-taken-code-{tag}.png'))

        # ------------------------------------------------ 2. numbers on tiles and in the heading
        admin.goto(BASE + BOARD)
        admin.wait_for_timeout(500)
        codes = tile_codes(admin)
        check(f'{tag} 2 номера на плитках', {'ZAP-1', 'ZAP-2'} <= set(codes), codes)
        open_card(admin, 'ZAP-2')
        heading = admin.locator('.board-drawer__code').inner_text().strip()
        check(f'{tag} 2 в шапке «Карточка ZAP-2»', heading == 'Карточка ZAP-2', heading)
        url_before = admin.url
        admin.click('.board-drawer__code')
        admin.wait_for_timeout(400)
        message = admin.locator('[data-board-message]').inner_text().strip()
        copied = admin.evaluate('navigator.clipboard.readText()')
        check(f'{tag} 2 щелчок по коду копирует ссылку',
              message == 'Ссылка на карточку ZAP-2 скопирована.' and copied == BASE + '/work/boards/1/?card=2',
              f'{message!r} {copied!r}')
        check(f'{tag} 2 щелчок по коду не уводит со страницы', admin.url == url_before, admin.url)
        admin.screenshot(path=os.path.join(SHOTS, f'2-numbers-{tag}.png'))

        # ------------------------------------------------ 3. search by the code
        admin.goto(BASE + BOARD)
        admin.fill('[data-registry-search]', 'zap-3')
        admin.wait_for_timeout(1500)
        admin.wait_for_load_state()
        codes = tile_codes(admin)
        check(f'{tag} 3 фильтр доски по коду', codes == ['ZAP-3'], codes)
        admin.screenshot(path=os.path.join(SHOTS, f'3-search-board-{tag}.png'))
        # The topbar search: one hit opens it at once — here the card itself.
        admin.goto(BASE + '/')
        admin.fill('[data-quick-search]', 'zap-3')
        admin.press('[data-quick-search]', 'Enter')
        admin.wait_for_load_state()
        admin.wait_for_timeout(500)
        heading = admin.locator('.board-drawer__code').all_inner_texts()
        check(f'{tag} 3 быстрый поиск по коду открывает карточку', heading == ['Карточка ZAP-3'], f'{admin.url} {heading}')
        admin.screenshot(path=os.path.join(SHOTS, f'3-search-quick-{tag}.png'))
        admin.goto(BASE + '/quality/tasks/?tab=all&source=zap-3')
        admin.wait_for_timeout(300)
        rows = admin.locator('tbody tr').count()
        check(f'{tag} 3 реестр задач по коду', rows == 1 and 'ZAP-3' in admin.locator('tbody').inner_text(), rows)
        ivanov = login(browser, 'ivanov', size)
        ivanov.goto(BASE + '/search/?q=OTG-1')
        check(f'{tag} 3 чужая доска по коду не находится', 'OTG-1' not in ivanov.locator('main').inner_text())

        # ------------------------------------------------ 4. pin a person, drag a card there
        admin.goto(BASE + BOARD)
        admin.wait_for_timeout(500)
        menu = admin.locator('.board-column', has=admin.locator('h2', has_text='В работе')).locator('.board-menu')
        menu.locator('summary').click()
        menu.locator('.board-pins__person', has_text='Мария Петрова').locator('input').check()
        admin.screenshot(path=os.path.join(SHOTS, f'4-pin-menu-{tag}.png'))
        menu.locator('form.board-pins button[type=submit]').click()
        admin.wait_for_load_state()
        admin.wait_for_timeout(500)
        pins = admin.locator('.board-column', has=admin.locator('h2', has_text='В работе')).locator('.board-column__pins')
        check(f'{tag} 4 в шапке колонки аватар закреплённого', pins.count() == 1 and 'МП' in pins.inner_text())
        ivanov.goto(BASE + BOARD)
        ivanov.wait_for_timeout(800)
        drag_to_column(ivanov, cards['drag'], 'В работе')
        check(f'{tag} 4 карточка в колонке «В работе»', column_of_tile(ivanov, cards['drag']) == 'В работе')
        avatars = tile(ivanov, cards['drag']).locator('.board-tile__assignees').inner_text()
        check(f'{tag} 4 исполнитель появился на плитке', 'МП' in avatars and 'ИИ' in avatars, avatars)
        ivanov.screenshot(path=os.path.join(SHOTS, f'4-pin-after-drag-{tag}.png'))
        open_card(ivanov, cards['drag'])
        ivanov.click('[data-board-tab-link="log"]')
        ivanov.wait_for_timeout(300)
        log = ivanov.locator('[data-live-board-log]').inner_text()
        check(f'{tag} 4 в логе запись по колонке', 'Исполнители по колонке «В работе»' in log, log[:200])
        ivanov.screenshot(path=os.path.join(SHOTS, f'4-pin-log-{tag}.png'))

        # Tab kept: on «Лог», another card opens on «Лог».
        tile(ivanov, 'ZAP-1' if cards['drag'] != 'ZAP-1' else 'ZAP-3').locator('.board-tile').click()
        ivanov.wait_for_timeout(700)
        kept = ivanov.evaluate("document.querySelector('[data-board-drawer]').getAttribute('data-board-tab')")
        check(f'{tag} 4 вкладка «Лог» сохранилась при открытии другой карточки', kept == 'log', kept)

        # ------------------------------------------------ 5. move to another sub-board
        cross = cards['cross']
        number = cross.split('-')[1]
        ivanov.goto(BASE + BOARD + f'?card={number}')
        ivanov.wait_for_timeout(800)
        admin.goto(BASE + BOARD + f'?card={number}')
        admin.wait_for_timeout(500)
        option = admin.locator(f'select[name=column_id] optgroup[label="Цех ПиР"] option', has_text=cards['cross_to'])
        admin.select_option('select[name=column_id]', option.get_attribute('value'))
        admin.screenshot(path=os.path.join(SHOTS, f'5-move-to-{tag}.png'))
        admin.click('.board-drawer__move button[type=submit]')
        admin.wait_for_load_state()
        admin.wait_for_timeout(500)
        check(f'{tag} 5 переход на поддоску карточки с открытой панелью',
              SECOND_TAB in admin.url and f'card={number}' in admin.url, admin.url)
        check(f'{tag} 5 карточка на новой поддоске', column_of_tile(admin, cross) == cards['cross_to'])
        admin.click('[data-board-tab-link="log"]')
        admin.wait_for_timeout(300)
        log = admin.locator('[data-live-board-log]').inner_text()
        check(f'{tag} 5 в логе перенос со снимками поддосок', '«Основная /' in log and f'«Цех ПиР / {cards["cross_to"]}»' in log,
              log[:200])
        admin.screenshot(path=os.path.join(SHOTS, f'5-moved-{tag}.png'))
        ivanov.wait_for_timeout(1500)
        notice = ivanov.locator('.board-drawer__moved')
        check(f'{tag} 5 у зрителя старой поддоски: карточка ушла с колонок', column_of_tile(ivanov, cross) is None)
        check(f'{tag} 5 у зрителя старой поддоски: панель говорит, где карточка',
              notice.count() == 1 and 'Цех ПиР' in notice.inner_text() and SECOND_TAB not in ivanov.url,
              ivanov.url)
        ivanov.screenshot(path=os.path.join(SHOTS, f'5-moved-notice-{tag}.png'))

        # ------------------------------------------------ 6. the action row is one line
        admin.goto(BASE + BOARD + '?card=1' if cards['drag'] != 'ZAP-1' else BASE + BOARD + '?card=2')
        admin.wait_for_timeout(500)
        boxes = admin.evaluate("""() => [...document.querySelectorAll(
            '.board-drawer__tools > a, .board-drawer__move select, .board-drawer__move button, .board-drawer__tools > button')]
            .map(e => { const r = e.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.bottom)]; })""")
        tops = {box[0] for box in boxes}
        bottoms = {box[1] for box in boxes}
        check(f'{tag} 6 строка действий выровнена', len(boxes) == 4 and max(tops) - min(tops) <= 1
              and max(bottoms) - min(bottoms) <= 1, boxes)
        admin.locator('.board-drawer__tools').screenshot(path=os.path.join(SHOTS, f'6-tools-row-{tag}.png'))
        admin.screenshot(path=os.path.join(SHOTS, f'6-description-{tag}.png'))

        admin.context.close()
        ivanov.context.close()

    browser.close()

check('нет ошибок JavaScript и браузерных диалогов', not problems, problems)
failed = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failed}/{len(results)} PASS')
sys.exit(failed)
