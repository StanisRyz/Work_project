"""Browser check of stage 23: «Норматив этапа», the traffic light, «Этапы»,
the report's «Этапы» and «Дайджест на почту».

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-23/stage23_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it (and restarts the server).
`DIGEST` is a shell command run after the subscription of round 1 that mails
the digest (see `digest_letter.py`) and prints the letter.

Two rounds, 1920×1080 and 1536×864, each:
1. `admin1` opens «⋯» of «На проверке», sets «Норматив этапа» 2 — the header
   reads «⏱ 2 р.д.»; «Сделать» (3 р.д.) and «В работе» (5 р.д.) hold green,
   yellow and red tiles with «план до ДД.ММ», the red ones highlighted;
   «Показать» → «Просрочен этап» keeps only the red ones;
2. ZAP-11 «Изготовить кронштейн» on «Описание»: «Этапы» — «Сделать» +2,
   «В работе» +1, «На проверке» current; a move by a colleague redraws the
   block without a reload;
3. «Отклонения» → «Этапы»: exits, average, maximum, share, «сейчас
   просрочен этап»; «Excel» downloads;
4. `ivanov` takes «Дайджест на почту: ежедневно» in the board's «⋯»; the
   letter (round 1) is saved beside the screenshots.
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
DIGEST = os.environ.get('DIGEST', '')
BOARD = '/work/boards/1/1/'
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
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]}, accept_downloads=True)
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


def lights(page, column_id):
    tiles = page.locator(f'section.board-column[data-column-id="{column_id}"] [data-card-id]')
    found = {}
    for index in range(tiles.count()):
        tile = tiles.nth(index)
        stage = tile.locator('[data-tile-stage]')
        light = stage.get_attribute('data-light') if stage.count() else None
        found[tile.get_attribute('data-card-id')] = (light, ' '.join(stage.inner_text().split()) if stage.count() else '')
    return found


def run_round(browser, size, first):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True, capture_output=True)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)

    # 1. The norm in the column's menu, three colours, «Просрочен этап».
    admin.goto(BASE + BOARD)
    settle(admin)
    column = admin.locator('section.board-column[data-column-id="3"]')
    column.locator('.board-menu__toggle').click()
    form = column.locator('form.board-norm')
    check('«⋯» of «На проверке»: «Норматив этапа: N раб. дн.»', 'Норматив этапа: N раб. дн.' in form.inner_text())
    form.locator('input[name=days]').fill('2')
    shot(admin, '1-norm-menu', size)
    with admin.expect_navigation():
        form.locator('button[type=submit]').click()
    settle(admin)
    norms = {cid: text(admin, f'section.board-column[data-column-id="{cid}"] .board-column__norm') for cid in '1234'}
    check('headers: ⏱ 3 / 5 / 2 р.д., none on the closing column',
          norms == {'1': '⏱ 3 р.д.', '2': '⏱ 5 р.д.', '3': '⏱ 2 р.д.', '4': ''}, norms)
    todo, work, review = lights(admin, '1'), lights(admin, '2'), lights(admin, '3')
    every = list(todo.values()) + list(work.values()) + list(review.values())
    colours = sorted({light for light, _ in every if light})
    check('three colours on the tiles', colours == ['green', 'red', 'yellow'], (todo, work, review))
    check('a light says «план до ДД.ММ» and keeps «в колонке …»',
          all(re.match(r'план до \d\d\.\d\d · в колонке', label) for light, label in todo.values() if light), todo)
    late = admin.locator('.board-tile--late')
    check('the red tiles are highlighted, the others not',
          late.count() == sum(1 for light, _ in every if light == 'red'), (late.count(), review))
    check('no sideways scroll', no_sideways_scroll(admin))
    shot(admin, '1-lights', size)
    show = admin.locator('details.board-filters__show')
    show.locator('summary').click()
    shot(admin, '1-show-menu', size)
    with admin.expect_navigation():
        show.locator('input[name=stale]').check()
    settle(admin)
    working = admin.locator('section.board-column:not(.board-column--done) [data-card-id]')
    reds = [working.nth(i).locator('[data-tile-stage]').get_attribute('data-light') for i in range(working.count())]
    check('«Просрочен этап» keeps only the red ones', reds and set(reds) == {'red'}, reds)
    check('«Показать · 1»', text(admin, '.board-filters__show-toggle') == 'Показать · 1')
    row = admin.locator('.board-filters')
    box = row.bounding_box()
    check('the filter row stays one line', box is not None and box['height'] < 60, box)
    shot(admin, '1-late-filter', size)

    # 2. «Этапы» of ZAP-11, live.
    admin.goto(BASE + BOARD + '?card=13')
    settle(admin)
    stages = admin.locator('[data-live-board-stages]')
    rows = [' '.join(r.inner_text().split()) for r in stages.locator('tbody tr').all()]
    check('«Этапы»: three stays in order', len(rows) == 3 and rows[0].startswith('Сделать')
          and rows[1].startswith('В работе') and rows[2].startswith('На проверке'), rows)
    check('…«Сделать» +2, «В работе» +1', rows[:2] and rows[0].endswith('+2') and rows[1].endswith('+1'), rows)
    check('…the current stage «в этапе»', len(rows) == 3 and 'в этапе' in rows[2], rows)
    check('…«по текущему нормативу»', 'по текущему нормативу' in stages.inner_text())
    stages.scroll_into_view_if_needed()
    shot(admin, '2-stages', size)
    admin.evaluate('() => { window.__stage23 = 1; }')
    ivanov.goto(BASE + BOARD + '?card=13')
    settle(ivanov)
    ivanov.select_option('[data-live-board-facts] select[name=column_id]', '2')
    with ivanov.expect_navigation():
        ivanov.locator('[data-live-board-facts] button', has_text='Переместить').click()
    settle(ivanov)
    try:
        admin.wait_for_function(
            '() => document.querySelectorAll("[data-live-board-stages] tbody tr").length === 4', timeout=10000,
        )
        grew = True
    except Exception:  # noqa: BLE001 - reported below
        grew = False
    check('a colleague\'s move adds a stay on admin1\'s page…', grew)
    check('…without a reload and without the conflict banner', admin.evaluate('() => window.__stage23') == 1
          and admin.locator('[data-board-conflict-banner]:visible').count() == 0)
    shot(admin, '2-stages-live', size)

    # 3. «Отклонения» → «Этапы».
    admin.goto(BASE + '/work/boards/1/deviations/')
    settle(admin)
    admin.locator('.board-view-switch__link', has_text='Этапы').click()
    settle(admin)
    table = admin.locator('.board-deviations__stages table')
    headers = [' '.join(th.inner_text().split()) for th in table.locator('thead th').all()]
    check('the report\'s «Этапы» table', table.count() == 1, headers)
    body = {}
    for r in table.locator('tbody tr').all():
        cells = [' '.join(td.inner_text().split()) for td in r.locator('td, th').all()]
        body[cells[0]] = cells
    exits = {name: cells[2] for name, cells in body.items()}
    check('«Сделать» and «В работе» had exits in the period',
          int(exits.get('Основная / Сделать', 0)) > 0 and int(exits.get('Основная / В работе', 0)) > 0, exits)
    check('…with the average, the maximum and the share within the norm',
          all(body['Основная / Сделать'][i] not in ('', '—') for i in (3, 4, 5)), body.get('Основная / Сделать'))
    print('   ', headers)
    for cells in body.values():
        print('   ', cells)
    with admin.expect_download() as download:
        admin.locator('a', has_text='Excel').first.click()
    name = download.value.suggested_filename
    check('«Excel» downloads the stage table', name.startswith('ZAP-etapy-') and name.endswith('.xlsx'), name)
    check('no sideways scroll', no_sideways_scroll(admin))
    shot(admin, '3-report-stages', size)

    # 4. «Дайджест на почту».
    ivanov.goto(BASE + BOARD)
    settle(ivanov)
    menu = ivanov.locator('details.board-head__menu')
    menu.locator('summary').click()
    digest = menu.locator('form.board-digest')
    check('the board\'s «⋯» offers «Дайджест на почту» to a member', digest.count() == 1)
    digest.locator('select[name=frequency]').select_option('DAILY')
    shot(ivanov, '4-digest-menu', size)
    with ivanov.expect_navigation():
        digest.locator('button[type=submit]').click()
    settle(ivanov)
    menu.locator('summary').click()
    check('…saved: «ежедневно» chosen', menu.locator('select[name=frequency]').input_value() == 'DAILY')
    if first and DIGEST:
        letter = subprocess.run(DIGEST, shell=True, check=True, capture_output=True, text=True).stdout
        check('the digest is sent to ivanov', 'Кому: ivanov@example.com' in letter)
        for needle in ('Просроченные карточки', 'Просрочен этап', 'Выполнено за период', '?view=table&stale=1'):
            check(f'…the letter holds «{needle}»', needle in letter)
    for page in (admin, ivanov):
        page.context.close()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)
    for index, size in enumerate(SIZES):
        run_round(browser, size, index == 0)
    browser.close()

check('no JavaScript errors and no browser dialogs', not problems, problems)
failed = [name for name, ok in results if not ok]
print(f'\n{len(results) - len(failed)}/{len(results)} PASS')
sys.exit(len(failed))
