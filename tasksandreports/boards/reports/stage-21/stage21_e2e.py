"""Browser check of stage 21: «Сроки и отклонения».

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-21/stage21_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it. `REMIND` is the shell
command that runs `manage.py board_due_reminders` on the server's database.

Two rounds, 1920×1080 and 1536×864, each:
1. `admin1` edits ZAP-1: «Перенос срока» is hidden while the date is the
   stored one and appears as soon as it changes; «Сохранить» without a
   reason is refused on «Причина переноса» with the typed date and comment
   kept; with «Ждём материал (снабжение)» it saves;
2. a second move («Брак»): «Описание» says «исходный …» and «Переносы:
   перенесён 2 раза», the table under it lists both; the tile «↻2» with the
   hint «Срок переносили 2 раза»; «Таблица» has «Исходный срок»,
   «Переносов», «Последняя причина»;
3. `petrova`, who follows ZAP-1, finds «Срок карточки ZAP-1 перенесён на …»
   in her bell;
4. `board_due_reminders` runs: `ivanov`'s bell has «Завтра срок карточки
   ZAP-8» and «Карточка ZAP-3 просрочена», `admin1`'s (the author) the
   latter; a second run adds nothing;
5. «⋯» → «Отклонения» (as `ivanov`, a plain member): the summary by reason,
   most moves first, and the list (the seed's five moves and ZAP-1's two); the sub-board filter; the Excel.
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import datetime
import io
import os
import subprocess
import sys
import zipfile
from xml.etree import ElementTree

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
RESET = os.environ.get('RESET', '')
REMIND = os.environ.get('REMIND', '')
BOARD = '/work/boards/1/1/'
CARD = BOARD + '?card=1'
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
    page.wait_for_timeout(1000)


def text(page, selector):
    locator = page.locator(selector)
    return ' '.join(locator.first.inner_text().split()) if locator.count() else ''


def no_sideways_scroll(page):
    return page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth + 1')


def bell(page, needle):
    page.reload()
    settle(page)
    page.locator('[data-notification-summary]').click()
    items = page.locator('.notification-menu__item', has_text=needle)
    return items.all_inner_texts()


def read_xlsx_rows(content):
    ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    rows = []
    for row in sheet.find('x:sheetData', ns):
        cells = []
        for cell in row:
            inline = cell.find('x:is/x:t', ns)
            value = cell.find('x:v', ns)
            cells.append(inline.text if inline is not None else (value.text if value is not None else None))
        rows.append(cells)
    return rows


def due_input(page):
    return page.locator('form.board-card-form input[name=due_date]')


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)
    petrova = login(browser, 'petrova', size)

    # 1. The reason appears with the move, is required, then saves.
    admin.goto(BASE + CARD + '&edit=1')
    settle(admin)
    block = admin.locator('form.board-card-form [data-due-reason]')
    stored = due_input(admin).get_attribute('data-stored-due')
    check('the edit form carries the stored срок', bool(stored), stored)
    check('«Перенос срока» hidden while the date is the stored one', block.count() == 1 and not block.is_visible())
    first = (datetime.date.fromisoformat(stored) + datetime.timedelta(days=4)).isoformat()
    due_input(admin).fill(first)
    check('…and shown as soon as it changes', block.is_visible())
    reasons = admin.locator('select[name=due_reason] option').all_inner_texts()
    check('the active reasons are offered in order', reasons[:3] == [
        '— выберите причину —', 'Ждём материал (снабжение)', 'Нет КД / ждём конструктора'] and len(reasons) == 9, reasons)
    admin.fill('textarea[name=due_comment]', 'Лист 2 мм придёт в пятницу.')
    shot(admin, '1-reason-fields', size)
    with admin.expect_navigation():
        admin.locator('form.board-card-form button[type=submit]').click()
    settle(admin)
    error = text(admin, 'form.board-card-form [data-due-reason] .errorlist')
    check('without a reason: refused on «Причина переноса»', 'Срок переносится только с причиной' in error, error)
    check('the typed date and comment are kept', due_input(admin).input_value() == first
          and admin.locator('textarea[name=due_comment]').input_value() == 'Лист 2 мм придёт в пятницу.')
    check('the refused block stays shown', block.is_visible())
    shot(admin, '1-refused', size)
    admin.select_option('select[name=due_reason]', label='Ждём материал (снабжение)')
    with admin.expect_navigation():
        admin.locator('form.board-card-form button[type=submit]').click()
    settle(admin)
    facts = text(admin, '[data-live-board-facts]')
    check('with a reason: saved', f'{first[8:10]}.{first[5:7]}.{first[:4]}' in facts and 'перенесён 1 раз' in facts, facts[:300])

    # 2. A second move: «перенесён 2 раза», «исходный», the tile «↻2».
    admin.goto(BASE + CARD + '&edit=1')
    settle(admin)
    second = (datetime.date.fromisoformat(first) + datetime.timedelta(days=3)).isoformat()
    due_input(admin).fill(second)
    admin.select_option('select[name=due_reason]', label='Брак')
    with admin.expect_navigation():
        admin.locator('form.board-card-form button[type=submit]').click()
    settle(admin)
    original = f'{stored[8:10]}.{stored[5:7]}.{stored[:4]}'
    facts = text(admin, '[data-live-board-facts]')
    check('«исходный» beside the срок', f'исходный {original}' in facts, facts[:300])
    history = admin.locator('details.board-due-history')
    check('«Переносы: перенесён 2 раза»', history.count() == 1 and 'перенесён 2 раза' in history.inner_text())
    history.locator('summary').click()
    rows = [' '.join(row.inner_text().split()) for row in history.locator('tbody tr').all()]
    check('the history lists both moves, oldest first, with reasons and who',
          len(rows) == 2 and 'Ждём материал' in rows[0] and 'Лист 2 мм' in rows[0] and 'Брак' in rows[1]
          and all('Олег Админов' in row for row in rows), rows)
    tile = admin.locator('[data-card-id="1"] .board-tile__due-moves')
    check('the tile «↻2», «Срок переносили 2 раза» on hover', tile.count() == 1 and tile.inner_text() == '↻2'
          and tile.get_attribute('title') == 'Срок переносили 2 раза')
    check('no sideways scroll', no_sideways_scroll(admin))
    shot(admin, '2-moved-twice', size)
    tile.scroll_into_view_if_needed()
    admin.locator('[data-card-id="1"]').screenshot(path=os.path.join(SHOTS, f'2-tile-{size[0]}.png'))

    admin.goto(BASE + BOARD + '?view=table&sort=-changes')
    admin.wait_for_load_state('networkidle')
    headers = [' '.join(th.inner_text().replace('↓', '').replace('↑', '').split()) for th in admin.locator('.board-table thead th').all()]
    at = headers.index('Исходный срок') if 'Исходный срок' in headers else -1
    check('«Таблица»: «Исходный срок», «Переносов», «Последняя причина» after «Срок»',
          at > 0 and headers[at - 1].startswith('Срок') and headers[at:at + 3] == [
              'Исходный срок', 'Переносов', 'Последняя причина'], headers)
    first_row = [' '.join(td.inner_text().split()) for td in admin.locator('.board-table tbody tr').first.locator('td').all()]
    check('sorted by «Переносов», most first: ZAP-1, 2, «Брак»', first_row[0] == 'ZAP-1'
          and first_row[at:at + 3] == [original, '2', 'Брак'], first_row)
    shot(admin, '2-table', size)

    # 3. A follower's bell.
    entries = bell(petrova, 'перенесён')
    shown = f'{second[8:10]}.{second[5:7]}.{second[:4]}'
    check('petrova (a follower): «Срок карточки ZAP-1 перенесён на …»',
          any(f'Срок карточки ZAP-1 перенесён на {shown}' in entry for entry in entries), entries)
    check('…without the reason in the text', not any('Брак' in entry or 'материал' in entry for entry in entries))
    shot(petrova, '3-follower-bell', size)
    petrova.keyboard.press('Escape')

    # 4. The reminders.
    if REMIND:
        output = subprocess.run(REMIND, shell=True, check=True, capture_output=True, text=True).stdout
        check('the command reports its counts', output.strip() == 'Напоминаний «срок подходит»: 1, «просрочено»: 2.', output.strip())
    entries = bell(ivanov, 'карточк')
    check('ivanov: «Завтра срок карточки ZAP-8»', any('Завтра срок карточки ZAP-8' in entry for entry in entries), entries)
    check('ivanov: «Карточка ZAP-3 просрочена»', any('Карточка ZAP-3 просрочена' in entry for entry in entries), entries)
    shot(ivanov, '4-reminders-bell', size)
    ivanov.keyboard.press('Escape')
    entries = bell(admin, 'просрочена')
    check('admin1, the author: «Карточка ZAP-3 просрочена»; no «Завтра» for a non-исполнитель',
          any('Карточка ZAP-3 просрочена' in entry for entry in entries)
          and not admin.locator('.notification-menu__item', has_text='Завтра срок').count(), entries)
    admin.keyboard.press('Escape')
    if REMIND:
        again = subprocess.run(REMIND, shell=True, check=True, capture_output=True, text=True).stdout
        check('a second run the same day adds nothing', again.strip() == 'Напоминаний «срок подходит»: 0, «просрочено»: 0.', again.strip())

    # 5. «Отклонения» for a plain member.
    ivanov.goto(BASE + BOARD)
    settle(ivanov)
    ivanov.locator('.board-head__menu > summary').click()
    link = ivanov.locator('.board-head__menu a', has_text='Отклонения')
    check('«⋯» → «Отклонения» for a member', link.count() == 1)
    check('…and no manager item', ivanov.locator('.board-head__menu form').count() == 0)
    with ivanov.expect_navigation():
        link.click()
    ivanov.wait_for_load_state('networkidle')
    summary = [[' '.join(td.inner_text().split()) for td in row.locator('td').all()]
               for row in ivanov.locator('.board-deviations__summary tbody tr').all()]
    check('the summary: «Ждём материал» first (3 moves), then «Брак» (2)',
          summary[:2] and summary[0][:2] == ['Ждём материал (снабжение)', '3'] and summary[1][:2] == ['Брак', '2'], summary)
    reasons = [row[0] for row in summary]
    check('every reason named once; OTG (another board) not in it', len(reasons) == len(set(reasons))
          and 'Ждём оплату' not in reasons, reasons)
    listed = ivanov.locator('.board-deviations__list tbody tr')
    first_listed = ' '.join(listed.first.inner_text().split())
    check('the list: newest first — ZAP-1, 4 → 7 days, «Брак», Олег Админов',
          listed.count() == 7 and first_listed.startswith(datetime.date.today().strftime('%d.%m.%Y'))
          and 'ZAP-1' in first_listed and 'Брак' in first_listed and 'Олег Админов' in first_listed, first_listed)
    meta = text(ivanov, '.board-table-meta')
    check('the period line: the last 30 days, 7 moves on 5 cards', '7 переносов, карточек 5' in meta, meta)
    check('no sideways scroll', no_sideways_scroll(ivanov))
    shot(ivanov, '5-deviations', size)
    ivanov.select_option('select[name=sub]', label='Цех ПиР')
    with ivanov.expect_navigation():
        ivanov.locator('.board-deviations__filters button[type=submit]').click()
    ivanov.wait_for_load_state('networkidle')
    sub_rows = [' '.join(row.inner_text().split()) for row in ivanov.locator('.board-deviations__list tbody tr').all()]
    check('«Цех ПиР»: its one move, «Оборудование»', len(sub_rows) == 1 and 'Оборудование' in sub_rows[0], sub_rows)
    export = ivanov.locator('.board-deviations__filters a', has_text='Excel').get_attribute('href')
    rows = read_xlsx_rows(ivanov.request.get(BASE + export).body())
    check('the Excel of the filtered list', rows[0][:2] == ['Дата', 'Карточка'] and len(rows) == 2
          and rows[1][7] == 'Оборудование' and rows[1][3] == 'Цех ПиР', rows)
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
