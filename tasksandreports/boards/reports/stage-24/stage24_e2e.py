"""Browser check of stage 24: «Приём заявок», «Заявки», «Входящие».

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-24/stage24_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it (and restarts the server).

Two rounds, 1920×1080 and 1536×864, each:
1. `admin1`, the owner of «Запуск заказов» (ZAP, the ПДО board), opens «⋯» →
   «Приём заявок», turns it on with a hint, `ivanov` as the handler, «Номер
   заявки» required and «Приоритет» in the form;
2. `otk_master` — ОТК, **not** a member of ZAP — finds «Заявки» in the menu,
   files a request through the form; a board page of ZAP is a 403 for her;
3. `ivanov` has ZAP open: «Входящие (1)» appears in the tabs without a
   reload, and his bell says «Новая заявка на доске «Запуск заказов»»;
4. `ivanov` accepts it from «Входящие»: the card stands in «Сделать» with the
   request's fields, and its «Чат» opens with «Заявка от …»;
5. `otk_master`'s «Мои заявки»: «Принята», the card's code, its column and
   «В работе» — no link to the board; `ivanov` completes the card, and her
   bell says «Заявка №N выполнена»;
6. a second request is rejected with a reason: «Отклонена» and the reason in
   «Мои заявки».
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


def file_request(page, title, number, size, shot_name=None):
    page.goto(BASE + '/work/requests/')
    settle(page)
    page.locator('.board-requests__board a', has_text='Запуск заказов').click()
    settle(page)
    page.fill('input[name=title]', title)
    page.fill('textarea[name=description]', f'{title}: партия 40 шт., чертёж 9АВ.616.001.')
    page.fill('input[name=field_1]', number)
    page.select_option('select[name=field_4]', label='● Высокий')
    if shot_name:
        shot(page, shot_name, size)
    with page.expect_navigation():
        page.locator('button[type=submit]', has_text='Подать заявку').click()
    settle(page)
    return int(re.search(r'/item/(\d+)/', page.url).group(1))


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True, capture_output=True)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)
    master = login(browser, 'otk_master', size)

    # 1. The owner turns the intake on.
    check('no «Заявки» in the menu while no board takes requests',
          master.locator('.sidebar__category', has_text='Заявки').count() == 0)
    admin.goto(BASE + BOARD)
    settle(admin)
    admin.locator('details.board-head__menu summary').click()
    admin.locator('.board-head__menu a', has_text='Приём заявок').click()
    settle(admin)
    admin.check('input[name=enabled]')
    admin.fill('input[name=due_days]', '3')
    admin.fill('textarea[name=hint]', 'Укажите номер заявки 1С и приоритет. Чертежи приложим в чат карточки.')
    admin.locator('label', has_text='Иван Иванов').locator('input[name=handlers]').check()
    admin.check('input[name=form_1][value=required]')
    admin.check('input[name=form_4][value=on]')
    shot(admin, '1-intake-settings', size)
    with admin.expect_navigation():
        admin.locator('.board-intake button[type=submit]').click()
    settle(admin)
    check('«Настройки приёма заявок сохранены»', 'сохранены' in admin.content())

    # 2. A stranger to the board files a request.
    ivanov.goto(BASE + BOARD)
    settle(ivanov)
    ivanov.evaluate('() => { window.__stage24 = 1; }')
    check('ivanov sees «Входящие (0)» in the tabs', text(ivanov, '[data-board-inbox]') == 'Входящие (0)')
    master.goto(BASE + '/')
    settle(master)
    menu = master.locator('.sidebar__category', has_text='Заявки')
    check('otk_master: «Заявки» in the menu', menu.count() == 1)
    check('…and on the dashboard', master.locator('a[href="/work/requests/"]').count() >= 2)
    master.locator('[data-sidebar-toggle]').click()
    master.wait_for_timeout(300)
    shot(master, '2-menu', size)
    menu.first.click()
    settle(master)
    check('«Подать заявку» lists ZAP with its hint', 'Запуск заказов' in text(master, '.board-requests__boards')
          and 'Укажите номер заявки' in text(master, '.board-requests__boards'))
    form_page = BASE + '/work/requests/1/new/'
    master.goto(form_page)
    settle(master)
    labels = text(master, '.board-request-form__grid')
    check('the form asks the board\'s fields, «Номер заявки» required',
          'Номер заявки *' in labels and 'Приоритет' in labels and 'Сумма' not in labels, labels)
    master.fill('input[name=title]', 'Без номера')
    check('the browser asks the required field first', master.get_attribute('input[name=field_1]', 'required') is not None)
    # The server's own rule, without the browser's: the attribute removed.
    master.evaluate("() => document.querySelector('input[name=field_1]').removeAttribute('required')")
    with master.expect_navigation():
        master.locator('button[type=submit]', has_text='Подать заявку').click()
    check('a required field left empty is refused beside it', 'обязательное поле заявки' in master.content())
    request_id = file_request(master, 'Нарезать заготовки ПиР', '3-1601', size, '2-request-form')
    check('the request page: «Новая»', text(master, '.board-request-status') == 'Новая')
    shot(master, '2-request-new', size)
    master.goto(BASE + BOARD)
    check('the board itself is a 403 for her', '403' in master.title() or 'Недостаточно прав' in master.content()
          or master.locator('h1', has_text='403').count() > 0)

    # 3. The handler sees it without a reload.
    try:
        ivanov.wait_for_function(
            "() => (document.querySelector('[data-board-inbox]') || {}).textContent === 'Входящие (1)'",
            timeout=10000,
        )
        live = True
    except Exception:  # noqa: BLE001 - reported below
        live = False
    check('«Входящие (1)» on ivanov\'s board without a reload', live and ivanov.evaluate('() => window.__stage24') == 1)
    shot(ivanov, '3-inbox-counter', size)
    entries = bell(ivanov, 'Новая заявка')
    check('ivanov\'s bell: «Новая заявка на доске «Запуск заказов»»',
          any(f'Новая заявка на доске «Запуск заказов»: Заявка №{request_id}' in entry for entry in entries), entries)
    shot(ivanov, '3-handler-bell', size)
    ivanov.keyboard.press('Escape')

    # 4. Accept.
    ivanov.goto(BASE + '/work/boards/1/inbox/')
    settle(ivanov)
    check('«Входящие»: the request, its author and department',
          'Нарезать заготовки ПиР' in text(ivanov, '.board-inbox') and 'Мастерова' in text(ivanov, '.board-inbox'))
    check('no sideways scroll', no_sideways_scroll(ivanov))
    shot(ivanov, '4-inbox', size)
    ivanov.locator(f'a[href="/work/boards/1/inbox/{request_id}/"]').first.click()
    settle(ivanov)
    check('«Принять» starts from the handler and the default срок',
          ivanov.locator('input[name=accept-assignees]:checked').count() == 1
          and ivanov.input_value('input[name=accept-due_date]') != '')
    shot(ivanov, '4-accept-form', size)
    with ivanov.expect_navigation():
        ivanov.locator('button[type=submit]', has_text='Принять').click()
    settle(ivanov)
    chat = text(ivanov, '[data-live-board-comments]')
    check('the card opens on «Чат» with «Заявка от …»', 'Заявка от Ольга Мастерова' in chat, chat[:120])
    card_code = text(ivanov, '[data-live-board-panel] a', ) or ''
    match = re.search(r'ZAP-\d+', ivanov.content())
    card_code = match.group(0) if match else ''
    shot(ivanov, '4-card-chat', size)
    ivanov.locator('.board-drawer__tabs a', has_text='Описание').first.click()
    ivanov.wait_for_timeout(300)
    facts = text(ivanov, '[data-live-board-facts]')
    check('…with the request\'s fields', '3-1601' in facts and 'Высокий' in facts, facts[:200])
    ivanov.locator('.board-drawer__tabs a', has_text='Лог').first.click()
    ivanov.wait_for_timeout(300)
    check('«Лог»: «создана из заявки №N»', f'из заявки №{request_id}' in text(ivanov, '[data-live-board-log]'))

    # 5. The author follows it.
    master.goto(BASE + '/work/requests/')
    settle(master)
    row = text(master, f'tr[data-row-url="/work/requests/item/{request_id}/"]')
    check('«Мои заявки»: «Принята», the card, «Сделать», «В работе»',
          'Принята' in row and 'Сделать' in row and 'В работе' in row, row)
    check('…as a projection, no link into the board',
          master.locator(f'tr[data-row-url="/work/requests/item/{request_id}/"] a[href*="/work/boards/"]').count() == 0)
    check('no sideways scroll', no_sideways_scroll(master))
    shot(master, '5-my-requests', size)
    card_id = ivanov.evaluate("() => new URL(location.href).searchParams.get('card')")
    ivanov.goto(f'{BASE}{BOARD}?card={card_id}')
    settle(ivanov)
    ivanov.locator('[data-live-board-panel] details summary', has_text='Завершить').click()
    ivanov.fill('#board-complete-result', 'Заготовки нарезаны и переданы в цех.')
    with ivanov.expect_navigation():
        ivanov.locator('[data-live-board-panel] form button[type=submit]', has_text='Завершить').click()
    settle(ivanov)
    entries = bell(master, 'выполнена')
    check('her bell: «Заявка №N выполнена»', any(f'Заявка №{request_id} выполнена' in e for e in entries), entries)
    shot(master, '5-author-bell', size)
    master.keyboard.press('Escape')
    master.goto(BASE + f'/work/requests/item/{request_id}/')
    settle(master)
    check('the request page: «Выполнена» and the closing column',
          'Выполнена' in text(master, '.board-request__outcome') and 'Готово' in text(master, '.board-request__outcome'))

    # 6. Rejection with a reason.
    second = file_request(master, 'Покрасить корпус', '3-1602', size)
    ivanov.goto(BASE + f'/work/boards/1/inbox/{second}/')
    settle(ivanov)
    ivanov.locator('.board-request-decide__reject button', has_text='Отклонить').click()
    ivanov.fill('[data-confirm-modal-comment-input]', 'Покраска — не наш участок, обратитесь в цех ПиР.')
    shot(ivanov, '6-reject-modal', size)
    with ivanov.expect_navigation():
        ivanov.locator('[data-confirm-modal-accept]').click()
    settle(ivanov)
    master.goto(BASE + '/work/requests/')
    settle(master)
    row = text(master, f'tr[data-row-url="/work/requests/item/{second}/"]')
    check('«Мои заявки»: «Отклонена» with the reason', 'Отклонена' in row and 'не наш участок' in row, row)
    shot(master, '6-rejected', size)
    entries = bell(master, 'отклонена')
    check('her bell: «Заявка №N отклонена»', any(f'Заявка №{second} отклонена' in e for e in entries), entries)
    master.keyboard.press('Escape')
    for page in (admin, ivanov, master):
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
