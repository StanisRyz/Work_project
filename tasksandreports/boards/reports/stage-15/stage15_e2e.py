"""Browser check of stage 15: a board's own card fields.

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-15/stage15_e2e.py [screenshot-dir]

The 1920×1080 round sets the fields up through «Поля карточек» — «Номер
заявки», «Заказ покупателя» (text), «Срок изготовления» (date), «Приоритет»
(Высокий/red, Средний/yellow, Низкий/gray) and «Стоп» (Да/red); the 1536×864
round (125 % Windows scaling of the same monitor) reads them. Each round
fills its own card, has a second user watch the tiles change live, and
archives «Заказ покупателя» (then puts it back). Each check prints
PASS/FAIL; the exit code is the number of failures.
"""

import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BOARD = '/work/boards/1/1/'
FIELDS = '/work/boards/1/fields/'
# Per size: the card filled through its form, and the card whose value the
# second user watches change.
ROUNDS = (
    ((1920, 1080), {'card': 'ZAP-1', 'live': 'ZAP-3', 'order': 'З-1579', 'date': '2026-11-30', 'priority': 'Высокий'}),
    ((1536, 864), {'card': 'ZAP-2', 'live': 'ZAP-4', 'order': 'З-1580', 'date': '2026-12-15', 'priority': 'Средний'}),
)
FIELD_SETUP = (
    ('Номер заявки', 'TEXT', ()),
    ('Заказ покупателя', 'TEXT', ()),
    ('Срок изготовления', 'DATE', ()),
    ('Приоритет', 'SELECT', (('Высокий', 'red'), ('Средний', 'yellow'), ('Низкий', 'gray'))),
    ('Стоп', 'SELECT', (('Да', 'red'),)),
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


def open_card(page, code):
    tile(page, code).locator('.board-tile').click()
    page.wait_for_selector('[data-board-drawer]:not([hidden]) .board-drawer__code')
    page.wait_for_timeout(300)


def field_input(page, name):
    """The card form's input of a field, by its label."""
    return page.locator('.board-card-form__fields label', has=page.locator('span', has_text=re.compile(f'^{name}$'))).locator('input, select')


def create_fields(page, tag):
    page.goto(BASE + FIELDS)
    for name, kind, options in FIELD_SETUP:
        form = page.locator('form[data-board-field-form]')
        form.locator('input[name=name]').fill(name)
        form.locator('select[name=kind]').select_option(kind)
        rows_visible = form.locator('[data-board-field-options]').is_visible()
        check(f'{tag} 1 варианты видны только у списка ({name})', rows_visible == (kind == 'SELECT'))
        for index, (label, color) in enumerate(options):
            if index:
                form.locator('[data-add-option-row]').click()
            row = form.locator('[data-option-rows] [data-option-row]').nth(index)
            row.locator('input[name=option_label]').fill(label)
            row.locator('select[name=option_color]').select_option(color)
        form.locator('button[type=submit]:has-text("Добавить поле")').click()
        page.wait_for_load_state()
    names = page.eval_on_selector_all('.board-field__name', 'n => n.map(e => e.textContent.trim())')
    check(f'{tag} 1 пять полей по порядку', names == [name for name, _, _ in FIELD_SETUP], names)
    chips = page.eval_on_selector_all(
        '#field-4 .board-field__options .board-chip', 'n => n.map(e => [e.textContent.trim(), e.className])',
    )
    check(
        f'{tag} 1 варианты «Приоритета» с цветами',
        [label for label, _ in chips] == ['Высокий', 'Средний', 'Низкий']
        and 'board-chip--red' in chips[0][1] and 'board-chip--yellow' in chips[1][1] and 'board-chip--gray' in chips[2][1],
        chips,
    )


def fields_page(page, tag):
    page.goto(BASE + FIELDS)
    page.wait_for_load_state()
    page.screenshot(path=os.path.join(SHOTS, f'1-fields-page-{tag}.png'))
    page.locator('#field-4 .board-field__edit > summary').click()
    page.wait_for_timeout(200)
    page.locator('#field-4').scroll_into_view_if_needed()
    page.screenshot(path=os.path.join(SHOTS, f'1-fields-edit-priority-{tag}.png'))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)

    for size, cards in ROUNDS:
        tag = f'{size[0]}'
        admin = login(browser, 'admin1', size)
        admin.evaluate('localStorage.clear()')

        # ------------------------------------------------ 1. «Поля карточек»
        if size[0] == 1920:
            create_fields(admin, tag)
        fields_page(admin, tag)
        admin.goto(BASE + BOARD)
        admin.locator('.board-head__menu > summary').click()
        check(f'{tag} 1 в «⋯» доски есть «Поля карточек»', admin.locator('.board-head__menu a:has-text("Поля карточек")').is_visible())
        admin.keyboard.press('Escape')

        # ------------------------------------------------ 2. a card with these values
        open_card(admin, cards['card'])
        admin.locator('[data-board-tab-body=description] a:has-text("Редактировать")').click()
        admin.wait_for_selector('.board-card-form__fields')
        labels = admin.eval_on_selector_all('.board-card-form__fields label > span', 'n => n.map(e => e.textContent.trim())')
        check(f'{tag} 2 форма: поля доски по порядку', labels == [name for name, _, _ in FIELD_SETUP], labels)
        check(
            f'{tag} 2 форма: дата — type=date, список с пустым вариантом',
            field_input(admin, 'Срок изготовления').get_attribute('type') == 'date'
            and field_input(admin, 'Приоритет').locator('option').first.inner_text().strip() == '—',
        )
        field_input(admin, 'Номер заявки').fill(cards['order'])
        field_input(admin, 'Заказ покупателя').fill('ООО «Энергомаш», заказ 4521')
        field_input(admin, 'Срок изготовления').fill(cards['date'])
        priority = field_input(admin, 'Приоритет')
        priority.select_option(label=f'● {cards["priority"]}')
        field_input(admin, 'Стоп').select_option(label='● Да')
        admin.locator('.board-card-form__fields').scroll_into_view_if_needed()
        admin.wait_for_timeout(500)
        admin.screenshot(path=os.path.join(SHOTS, f'2-card-form-{tag}.png'))
        admin.locator('.board-card-form button[type=submit]').click()
        admin.wait_for_load_state()
        admin.wait_for_selector('[data-board-tab-body=description] .board-drawer__facts')
        facts = admin.eval_on_selector_all(
            '[data-board-tab-body=description] .board-drawer__field',
            'n => n.map(e => [e.querySelector("dt").textContent.trim(), e.querySelector("dd").textContent.trim()])',
        )
        day = '.'.join(reversed(cards['date'].split('-')))
        expected = [
            ['Номер заявки', cards['order']],
            ['Заказ покупателя', 'ООО «Энергомаш», заказ 4521'],
            ['Срок изготовления', day],
            ['Приоритет', cards['priority']],
            ['Стоп', 'Да'],
        ]
        check(f'{tag} 2 «Описание»: значения по порядку полей', facts == expected, facts)
        chip = admin.locator('[data-board-tab-body=description] .board-drawer__field .board-chip').first
        check(f'{tag} 2 «Описание»: список — цветной плашкой', 'board-chip--' in (chip.get_attribute('class') or ''))
        admin.wait_for_timeout(500)
        admin.screenshot(path=os.path.join(SHOTS, f'2-card-description-{tag}.png'))
        admin.locator('[data-board-tab-link=log]').click()
        admin.wait_for_timeout(200)
        log = admin.locator('[data-board-tab-body=log]').inner_text()
        check(
            f'{tag} 2 «Лог»: «Изменено: Номер заявки, Заказ покупателя, …»',
            'Изменено: Номер заявки, Заказ покупателя, Срок изготовления, Приоритет, Стоп' in log,
        )
        admin.locator('[data-board-drawer-close]').click()
        admin.wait_for_timeout(300)
        values = tile(admin, cards['card']).locator('.board-tile__fields').inner_text()
        tile_chips = tile(admin, cards['card']).locator('.board-tile__fields .board-chip').count()
        tile_lines = tile(admin, cards['card']).locator('.board-tile__fields > *').count()
        check(
            f'{tag} 2 плитка: до четырёх значений, список — плашкой',
            tile_lines == 4 and tile_chips == 1 and f'Номер заявки: {cards["order"]}' in values
            and f'Срок изготовления: {day}' in values and 'Стоп' not in values,
            values,
        )
        tile(admin, cards['card']).screenshot(path=os.path.join(SHOTS, f'2-card-tile-{tag}.png'))
        admin.screenshot(path=os.path.join(SHOTS, f'2-board-{tag}.png'))

        # ------------------------------------------------ 3. another user sees it live
        watcher = login(browser, 'petrova', size)
        watcher.goto(BASE + BOARD)
        watcher.wait_for_selector('.board-tile')
        watcher.evaluate('window.__noReload = true')
        before = tile(watcher, cards['live']).inner_text()
        open_card(admin, cards['live'])
        admin.locator('[data-board-tab-body=description] a:has-text("Редактировать")').click()
        admin.wait_for_selector('.board-card-form__fields')
        field_input(admin, 'Номер заявки').fill(cards['order'] + '-Б')
        field_input(admin, 'Приоритет').select_option(label='● Низкий')
        admin.locator('.board-card-form button[type=submit]').click()
        admin.wait_for_load_state()
        try:
            watcher.wait_for_function(
                """(code) => {
                    const number = [...document.querySelectorAll('.board-tile__number')].find(e => e.textContent.trim() === code);
                    const tile = number && number.closest('.board-tile');
                    return tile && tile.textContent.includes('Низкий');
                }""",
                arg=cards['live'], timeout=15000,
            )
            arrived = True
        except Exception:  # noqa: BLE001 — reported as a failed check
            arrived = False
        after = tile(watcher, cards['live']).inner_text()
        check(
            f'{tag} 3 второй пользователь видит новые значения без перезагрузки',
            arrived and f'Номер заявки: {cards["order"]}-Б' in after and watcher.evaluate('window.__noReload === true'),
            f'{before!r} → {after!r}',
        )
        tile(watcher, cards['live']).screenshot(path=os.path.join(SHOTS, f'3-live-tile-{tag}.png'))
        watcher.screenshot(path=os.path.join(SHOTS, f'3-live-board-{tag}.png'))

        # ------------------------------------------------ 4. an archived field on «Описание»
        admin.goto(BASE + FIELDS)
        admin.locator('#field-2 button:has-text("В архив")').click()
        admin.wait_for_load_state()
        check(f'{tag} 4 поле «Заказ покупателя» в архиве', admin.locator('#field-2 .status-badge--archived').is_visible())
        # The second user's tile loses it live too.
        try:
            watcher.wait_for_function(
                """(code) => {
                    const number = [...document.querySelectorAll('.board-tile__number')].find(e => e.textContent.trim() === code);
                    return number && !number.closest('.board-tile').textContent.includes('Заказ покупателя');
                }""",
                arg=cards['card'], timeout=15000,
            )
            gone = True
        except Exception:  # noqa: BLE001
            gone = False
        check(f'{tag} 4 архивное поле ушло с плитки у второго пользователя вживую', gone)
        admin.goto(BASE + BOARD)
        open_card(admin, cards['card'])
        archived = admin.locator('.board-drawer__field--archived').inner_text()
        check(
            f'{tag} 4 «Описание»: архивное значение с пометкой',
            'Заказ покупателя' in archived and '(в архиве)' in archived and 'Энергомаш' in archived,
            archived,
        )
        admin.locator('.board-drawer__field--archived').scroll_into_view_if_needed()
        admin.wait_for_timeout(500)
        admin.screenshot(path=os.path.join(SHOTS, f'4-archived-field-{tag}.png'))
        admin.locator('[data-board-tab-body=description] a:has-text("Редактировать")').click()
        admin.wait_for_selector('.board-card-form__fields')
        labels = admin.eval_on_selector_all('.board-card-form__fields label > span', 'n => n.map(e => e.textContent.trim())')
        check(f'{tag} 4 архивного поля нет в форме', 'Заказ покупателя' not in labels, labels)
        # Back, for the next round.
        admin.goto(BASE + FIELDS)
        admin.locator('#field-2 button:has-text("Вернуть")').click()
        admin.wait_for_load_state()

        admin.context.close()
        watcher.context.close()

    browser.close()

failed = [name for name, ok in results if not ok]
print(f'\n{len(results) - len(failed)}/{len(results)} PASS')
if problems:
    print('Problems:', *problems, sep='\n  ')
sys.exit(len(failed) + len(problems))
