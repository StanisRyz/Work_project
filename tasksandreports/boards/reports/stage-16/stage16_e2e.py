"""Browser check of stage 16: filtering a board by its fields, finding a card by a value.

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-16/stage16_e2e.py [screenshot-dir]

Two rounds, 1920×1080 and 1536×864 (125 % Windows scaling of the same
monitor), each:
1. «Поля» → «Приоритет: Высокий»: the right cards stay, the column counts are
   the counts of the tiles drawn, the chip and «Поля · 1» show, the panel is
   open again after the reload;
2. «Срок изготовления» по 30.11.2026 and «Стоп: Да» together, the chips' «×»;
3. «3-1579» found on the board, in «Задачи» («Источник») and by the topbar
   search;
4. a second user (Петрова) gives a card «Высокий»: it appears in the first
   user's filtered column without a reload;
5. a card dragged under the filter, and back.
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/1/'
COLUMNS = ('Сделать', 'В работе', 'На проверке', 'Готово')
# Per size: the card the second user gives «Высокий», and what «Высокий»
# shows before that (the first round's card is «Высокий» in the second).
ROUNDS = (
    ((1920, 1080), {'live': 'ZAP-7', 'high': {'Сделать': ['ZAP-1'], 'В работе': ['ZAP-3'], 'Готово': ['ZAP-6']}}),
    ((1536, 864), {'live': 'ZAP-8', 'high': {'Сделать': ['ZAP-1', 'ZAP-7'], 'В работе': ['ZAP-3'], 'Готово': ['ZAP-6']}}),
)

results = []
problems = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + str(detail)) if detail else ''}")


def login(browser, username, size):
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]})
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
    return page.locator('.board-column__item', has=page.locator('.board-tile__number', has_text=re.compile(f'^{code}$')))


def board_state(page):
    """`{column: (the count in its header, [the codes of its tiles])}`."""
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.board-column')].map((column) => [
        column.querySelector('h2').textContent.trim(),
        [Number(column.querySelector('[data-column-count]').textContent.trim()),
         [...column.querySelectorAll('.board-tile__number')].map((n) => n.textContent.trim())],
    ]))""")


def expect(state, wanted):
    """Every column holds exactly `wanted[column]` (in any order — a drag may
    put a card back elsewhere in its column) and says so in its header."""
    return all(
        sorted(state[name][1]) == sorted(wanted.get(name, [])) and state[name][0] == len(wanted.get(name, []))
        for name in COLUMNS
    )


def fields_row(page, name):
    return page.locator('.board-field-filter', has=page.locator('legend', has_text=re.compile(f'^{name}$')))


def option_box(page, field, label):
    """The tick box of a list option, by the option's chip."""
    return fields_row(page, field).locator(
        'label', has=page.locator('.board-chip', has_text=re.compile(f'^{label}$')),
    ).locator('input')


def open_fields(page):
    if not page.locator('details.board-filters__fields[open]').count():
        page.locator('.board-filters__fields-toggle').click()
    page.wait_for_selector('.board-field-filters', state='visible')


def chips(page):
    return page.eval_on_selector_all('.board-filter-chip > span', 'n => n.map(e => e.textContent.trim())')


def toggle_text(page):
    return page.locator('.board-filters__fields-toggle').inner_text().strip()


def single_line(page):
    """The filter row is one line high and the page has no sideways scroll."""
    box = page.locator('.board-filters').bounding_box()
    wide = page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
    return box['height'] < 48 and not wide, f"height {box['height']:.0f}, sideways scroll {wide}"


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

    for size, round_ in ROUNDS:
        tag = f'{size[0]}'
        high = round_['high']
        admin = login(browser, 'admin1', size)
        admin.evaluate('localStorage.clear(); sessionStorage.clear()')
        admin.goto(BASE + BOARD)
        admin.wait_for_selector('.board-tile')

        # ------------------------------------------------ 1. «Приоритет: Высокий»
        check(f'{tag} 1 кнопка «Поля» без счётчика', toggle_text(admin) == 'Поля', toggle_text(admin))
        open_fields(admin)
        names = admin.eval_on_selector_all('.board-field-filter legend', 'n => n.map(e => e.textContent.trim())')
        check(
            f'{tag} 1 панель «Поля»: живые поля по порядку',
            names == ['Номер заявки', 'Заказ покупателя', 'Срок изготовления', 'Приоритет', 'Стоп'], names,
        )
        option_chips = fields_row(admin, 'Приоритет').evaluate(
            "row => [...row.querySelectorAll('.board-chip')].map(e => [e.textContent.trim(), e.className])",
        )
        check(
            f'{tag} 1 варианты — флажки с цветными плашками и «не задано»',
            [label for label, _ in option_chips] == ['Высокий', 'Средний', 'Низкий']
            and 'board-chip--red' in option_chips[0][1]
            and fields_row(admin, 'Приоритет').locator('input[value=none]').count() == 1,
            option_chips,
        )
        admin.wait_for_timeout(300)
        admin.screenshot(path=os.path.join(SHOTS, f'1-fields-panel-{tag}.png'))
        with admin.expect_navigation():
            option_box(admin, 'Приоритет', 'Высокий').check()
        admin.wait_for_selector('.board-tile')
        state = board_state(admin)
        check(f'{tag} 1 «Высокий»: нужные карточки, счётчики колонок верны', expect(state, high), state)
        check(
            f'{tag} 1 адрес несёт фильтр и только его',
            re.search(r'\?f_\d+=\d+$', admin.url) is not None, admin.url,
        )
        check(f'{tag} 1 плашка «Приоритет: Высокий» и «Поля · 1»', chips(admin) == ['Приоритет: Высокий'] and toggle_text(admin) == 'Поля · 1', (chips(admin), toggle_text(admin)))
        check(f'{tag} 1 панель «Поля» снова открыта после применения', admin.locator('details.board-filters__fields[open]').count() == 1)
        ok, detail = single_line(admin)
        check(f'{tag} 1 строка фильтров в одну строку, без горизонтальной прокрутки', ok, detail)
        admin.wait_for_timeout(300)
        admin.screenshot(path=os.path.join(SHOTS, f'1-priority-high-{tag}.png'))
        admin.keyboard.press('Escape')
        admin.wait_for_timeout(200)
        admin.screenshot(path=os.path.join(SHOTS, f'1-priority-high-chips-{tag}.png'))

        # The chip's «×»: the whole board again.
        with admin.expect_navigation():
            admin.locator('.board-filter-chip__remove').first.click()
        check(f'{tag} 1 «×» снимает фильтр', not chips(admin) and 'f_' not in admin.url and toggle_text(admin) == 'Поля', admin.url)

        # ------------------------------------------------ 2. a date and «Стоп: Да» together
        open_fields(admin)
        with admin.expect_navigation():
            fields_row(admin, 'Срок изготовления').locator('input[name$=_to]').fill('2026-11-30')
        admin.wait_for_selector('.board-tile')
        open_fields(admin)
        with admin.expect_navigation():
            option_box(admin, 'Стоп', 'Да').check()
        admin.wait_for_selector('.board-tile')
        state = board_state(admin)
        check(
            f'{tag} 2 «по 30.11.2026» и «Стоп: Да»: ZAP-1 и ZAP-4',
            expect(state, {'Сделать': ['ZAP-1'], 'На проверке': ['ZAP-4']}), state,
        )
        check(
            f'{tag} 2 две плашки и «Поля · 2»',
            chips(admin) == ['Срок изготовления: по 30.11.2026', 'Стоп: Да'] and toggle_text(admin) == 'Поля · 2',
            (chips(admin), toggle_text(admin)),
        )
        check(
            f'{tag} 2 значения формы сохранены',
            fields_row(admin, 'Срок изготовления').locator('input[name$=_to]').input_value() == '2026-11-30'
            and option_box(admin, 'Стоп', 'Да').is_checked(),
        )
        admin.wait_for_timeout(300)
        admin.screenshot(path=os.path.join(SHOTS, f'2-date-and-stop-{tag}.png'))
        admin.keyboard.press('Escape')
        # The open card stays open under the filter, and its links keep it.
        tile(admin, 'ZAP-4').locator('.board-tile').click()
        admin.wait_for_selector('[data-board-drawer]:not([hidden]) .board-drawer__code')
        admin.wait_for_timeout(400)
        chip_href = admin.locator('.board-filter-chip__remove').first.get_attribute('href')
        check(f'{tag} 2 «×» плашки держит открытую карточку', 'card=' in chip_href and 'f_' in chip_href, chip_href)
        admin.screenshot(path=os.path.join(SHOTS, f'2-date-and-stop-card-{tag}.png'))
        with admin.expect_navigation():
            admin.locator('[data-board-filter-reset]').click()
        check(
            f'{tag} 2 «Сбросить» снимает все фильтры, карточка открыта',
            not chips(admin) and 'f_' not in admin.url and admin.locator('[data-board-drawer]:not([hidden])').count() == 1,
            admin.url,
        )

        # ------------------------------------------------ 3. «3-1579» on the board, in «Задачи», in the topbar
        admin.goto(BASE + BOARD)
        with admin.expect_navigation():
            admin.locator('.board-filters__search').fill('3-1579')
        admin.wait_for_selector('.board-tile')
        state = board_state(admin)
        check(f'{tag} 3 поиск доски «3-1579» находит ZAP-1', expect(state, {'Сделать': ['ZAP-1']}), state)
        admin.screenshot(path=os.path.join(SHOTS, f'3-search-board-{tag}.png'))
        admin.goto(BASE + '/quality/tasks/?tab=all&source=3-1579')
        rows = admin.locator('table tbody tr')
        text = rows.first.inner_text() if rows.count() else ''
        check(f'{tag} 3 «Задачи», «Источник» = 3-1579: одна задача ZAP-1', rows.count() == 1 and 'ZAP-1' in text, text)
        admin.screenshot(path=os.path.join(SHOTS, f'3-search-tasks-{tag}.png'))
        admin.locator('[data-quick-search]').fill('3-1579')
        with admin.expect_navigation():
            admin.locator('[data-quick-search]').press('Enter')
        # One hit: the search opens it — the task, i.e. its card on the board.
        admin.wait_for_selector('[data-board-drawer]:not([hidden]) .board-drawer__code')
        admin.wait_for_timeout(700)
        code = admin.locator('.board-drawer__code').inner_text()
        check(f'{tag} 3 поиск в шапке «3-1579» открывает карточку ZAP-1', 'ZAP-1' in code and 'card=' in admin.url, (code, admin.url))
        admin.screenshot(path=os.path.join(SHOTS, f'3-search-topbar-{tag}.png'))
        # Several hits: the results page lists them.
        admin.locator('[data-quick-search]').fill('3-158')
        with admin.expect_navigation():
            admin.locator('[data-quick-search]').press('Enter')
        found = admin.locator('main').inner_text()
        check(
            f'{tag} 3 поиск в шапке «3-158» — список карточек',
            all(f'ZAP-{n}' in found for n in (2, 3, 4, 5, 6, 8)) and 'ZAP-1 ' not in found, found[:200],
        )
        admin.screenshot(path=os.path.join(SHOTS, f'3-search-topbar-list-{tag}.png'))

        # ------------------------------------------------ 4. a second user gives a card «Высокий»
        admin.goto(BASE + BOARD)
        open_fields(admin)
        with admin.expect_navigation():
            option_box(admin, 'Приоритет', 'Высокий').check()
        admin.keyboard.press('Escape')
        admin.wait_for_selector('.board-tile')
        admin.evaluate('window.__noReload = true')
        check(f'{tag} 4 до правки {round_["live"]} не в фильтре', expect(board_state(admin), high), board_state(admin))
        watcher = login(browser, 'petrova', size)
        watcher.goto(BASE + BOARD)
        tile(watcher, round_['live']).locator('.board-tile').click()
        watcher.wait_for_selector('[data-board-drawer]:not([hidden]) .board-drawer__code')
        watcher.locator('[data-board-tab-body=description] a:has-text("Редактировать")').click()
        watcher.wait_for_selector('.board-card-form__fields')
        watcher.locator(
            '.board-card-form__fields label', has=watcher.locator('span', has_text=re.compile('^Приоритет$')),
        ).locator('select').select_option(label='● Высокий')
        watcher.locator('.board-card-form button[type=submit]').click()
        watcher.wait_for_load_state()
        try:
            admin.wait_for_function(
                """(code) => [...document.querySelectorAll('.board-tile__number')].some(e => e.textContent.trim() === code)""",
                arg=round_['live'], timeout=15000,
            )
            arrived = True
        except Exception:  # noqa: BLE001 — reported as a failed check
            arrived = False
        admin.wait_for_timeout(500)
        state = board_state(admin)
        wanted = {**high, 'Сделать': high['Сделать'] + [round_['live']]}
        check(
            f'{tag} 4 карточка с «Высокий» появилась у первого пользователя без перезагрузки',
            arrived and expect(state, wanted) and admin.evaluate('window.__noReload === true'), state,
        )
        admin.screenshot(path=os.path.join(SHOTS, f'4-live-{tag}.png'))
        watcher.context.close()

        # ------------------------------------------------ 5. dragging under the filter
        drag_to_column(admin, 'ZAP-1', 'На проверке')
        state = board_state(admin)
        moved = {**wanted, 'Сделать': [c for c in wanted['Сделать'] if c != 'ZAP-1'], 'На проверке': ['ZAP-1']}
        check(f'{tag} 5 перенос под фильтром: колонки и счётчики', expect(state, moved), state)
        admin.screenshot(path=os.path.join(SHOTS, f'5-drag-{tag}.png'))
        admin.reload()
        admin.wait_for_selector('.board-tile')
        check(
            f'{tag} 5 после перезагрузки то же, фильтр в адресе',
            expect(board_state(admin), moved) and 'f_' in admin.url, board_state(admin),
        )
        drag_to_column(admin, 'ZAP-1', 'Сделать')
        state = board_state(admin)
        check(f'{tag} 5 обратно в «Сделать»', 'ZAP-1' in state['Сделать'][1] and state['На проверке'][0] == 0, state)

        admin.context.close()

    browser.close()

failed = [name for name, ok in results if not ok]
print(f'\n{len(results) - len(failed)}/{len(results)} PASS')
if problems:
    print('Problems:', *problems, sep='\n  ')
sys.exit(len(failed) + len(problems))
