"""Browser check of stage 13: the card drawer with its tabs and journal.

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-13/drawer_e2e.py [screenshot-dir]

Every scenario runs at 1920×1080 and at 1536×864 (125 % Windows scaling of
the same monitor); the second run changes the cards back, so the data may be
reused. Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
SIZES = ((1920, 1080), (1536, 864))
BOARD = '/work/boards/1/1/'

results = []
console_errors = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + str(detail)) if detail else ''}")


def login(browser, username, size):
    context = browser.new_context(viewport={'width': size[0], 'height': size[1]})
    page = context.new_page()
    page.on('pageerror', lambda e: console_errors.append(f'{username}: {e}'))
    page.on('dialog', lambda d: (console_errors.append(f'{username}: dialog {d.type}'), d.accept()))
    page.goto(BASE + '/accounts/login/')
    page.fill('input[name=username]', username)
    page.fill('input[name=password]', username)
    page.click('button[type=submit]')
    page.wait_for_load_state()
    return page


def column_widths(page):
    return page.eval_on_selector_all('.board-column', 'n => n.map(c => Math.round(c.getBoundingClientRect().width))')


def drawer_box(page):
    return page.evaluate("""() => {
        const d = document.querySelector('[data-board-drawer]');
        const l = document.querySelector('[data-board-layout]');
        const r = d.getBoundingClientRect(), b = l.getBoundingClientRect();
        return {hidden: d.hidden, left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width),
                top: Math.round(r.top), bottom: Math.round(r.bottom), layoutLeft: Math.round(b.left),
                layoutRight: Math.round(b.right), layoutWidth: Math.round(b.width), layoutTop: Math.round(b.top),
                layoutBottom: Math.round(b.bottom), tab: d.getAttribute('data-board-tab'),
                pageScroll: document.documentElement.scrollHeight > innerHeight
                    || document.documentElement.scrollWidth > innerWidth};
    }""")


def visible_body(page):
    return page.evaluate("""() => [...document.querySelectorAll('[data-board-tab-body]')]
        .filter(e => e.offsetParent !== null).map(e => e.getAttribute('data-board-tab-body'))""")


def open_tile(page, title):
    page.locator('.board-tile', has_text=title).first.click()
    page.wait_for_selector('[data-board-drawer]:not([hidden]) .board-drawer__title')
    page.wait_for_timeout(300)


def tab(page, name):
    page.click(f'[data-board-tab-link="{name}"]')
    page.wait_for_timeout(200)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)

    for size in SIZES:
        tag = f'{size[0]}'
        # -------------------------------------------------------------- 1
        admin = login(browser, 'admin1', size)
        admin.evaluate('localStorage.clear()')
        admin.goto(BASE + BOARD)
        admin.wait_for_timeout(400)
        before = column_widths(admin)
        admin.evaluate('window.__stage13Marker = "same page"')
        open_tile(admin, 'Утвердить технологическую карту')
        after = column_widths(admin)
        box = drawer_box(admin)
        check(f'{tag} 1 панель выехала поверх колонок', not box['hidden'] and box['right'] == box['layoutRight']
              and box['top'] == box['layoutTop'] and box['bottom'] == box['layoutBottom'], box)
        expected = min(640, box['layoutWidth'] // 2)
        check(f'{tag} 1 ширина панели ≈ min(640, половина доски)', abs(box['width'] - expected) <= 2,
              f"{box['width']} при доске {box['layoutWidth']}")
        check(f'{tag} 1 колонки не изменили ширину', before == after, f'{before} → {after}')
        check(f'{tag} 1 страница не перезагружалась',
              admin.evaluate('window.__stage13Marker') == 'same page')
        check(f'{tag} 1 адрес с card и tab', 'card=2' in admin.url and 'tab=description' in admin.url, admin.url)
        check(f'{tag} 1 страница не прокручивается', not box['pageScroll'])
        nav_width = admin.evaluate("Math.round(document.querySelector('.board-nav').getBoundingClientRect().width)")
        check(f'{tag} 1 список досок слева не свернулся', nav_width > 150, nav_width)
        admin.screenshot(path=os.path.join(SHOTS, f'drawer-description-{tag}.png'))

        # -------------------------------------------------------------- 2
        for name in ('chat', 'files', 'log', 'description'):
            tab(admin, name)
            check(f'{tag} 2 вкладка «{name}» видна и в адресе',
                  visible_body(admin) == [name] and f'tab={name}' in admin.url, f'{visible_body(admin)} {admin.url}')
            if name != 'description':
                admin.screenshot(path=os.path.join(SHOTS, f'drawer-{name}-{tag}.png'))
        check(f'{tag} 2 вкладки без перезагрузки', admin.evaluate('window.__stage13Marker') == 'same page')
        admin.go_back()
        admin.wait_for_timeout(300)
        # The tab switches replaced the entry; one «назад» closes the drawer.
        check(f'{tag} 2 «назад» закрывает панель', drawer_box(admin)['hidden'] and 'card=' not in admin.url,
              admin.url)
        admin.go_forward()
        admin.wait_for_selector('[data-board-drawer]:not([hidden]) .board-drawer__title')
        check(f'{tag} 2 «вперёд» открывает её снова', not drawer_box(admin)['hidden'] and 'card=2' in admin.url)
        admin.keyboard.press('Escape')
        admin.wait_for_timeout(200)
        check(f'{tag} 2 Esc закрывает', drawer_box(admin)['hidden'])
        open_tile(admin, 'Утвердить технологическую карту')
        admin.mouse.click(box['layoutLeft'] + 40, box['layoutBottom'] - 30)
        admin.wait_for_timeout(200)
        check(f'{tag} 2 щелчок по затемнённой доске закрывает', drawer_box(admin)['hidden'])

        # -------------------------------------------------------------- 3
        open_tile(admin, 'Запустить партию в цех МП')
        tab(admin, 'chat')
        admin.fill('#board-comment-text', 'Набираю ответ, не отправлено')
        ivanov = login(browser, 'ivanov', size)
        ivanov.goto(BASE + BOARD + '?card=3&tab=chat')
        message = f'Партия запущена {tag} {int(time.time())}'
        ivanov.fill('#board-comment-text', message)
        with ivanov.expect_navigation():
            ivanov.click('.board-chat__form button[type=submit]')
        admin.wait_for_selector(f'[data-live-board-comments] >> text={message}', timeout=10000)
        check(f'{tag} 3 сообщение второго пользователя появилось в «Чате»', True)
        check(f'{tag} 3 вкладка не сбросилась, набранный текст цел',
              drawer_box(admin)['tab'] == 'chat'
              and admin.input_value('#board-comment-text') == 'Набираю ответ, не отправлено')
        admin.screenshot(path=os.path.join(SHOTS, f'live-chat-{tag}.png'))
        tab(admin, 'log')
        ivanov.goto(BASE + BOARD + '?card=3')
        current = ivanov.eval_on_selector('select[name=column_id]', 'e => e.value')
        target = ivanov.eval_on_selector(
            'select[name=column_id]', '(e, v) => [...e.options].find(o => o.value !== v).value', current,
        )
        target_name = ivanov.eval_on_selector(
            'select[name=column_id]', '(e, v) => [...e.options].find(o => o.value === v).text', target,
        )
        ivanov.select_option('select[name=column_id]', target)
        with ivanov.expect_navigation():
            ivanov.click('.board-drawer__move button[type=submit]')
        admin.wait_for_selector(f'[data-live-board-log] >> text=→ «{target_name}»', timeout=10000)
        check(f'{tag} 3 перенос появился в «Логе»', True)
        check(f'{tag} 3 вкладка «Лог» не сбросилась', drawer_box(admin)['tab'] == 'log'
              and visible_body(admin) == ['log'])
        admin.screenshot(path=os.path.join(SHOTS, f'live-log-{tag}.png'))
        # The unsent text was the point; leaving it would make the next
        # navigation ask «Покинуть страницу?».
        admin.evaluate("document.querySelector('#board-comment-text').value = ''; "
                       "window.qualityUnsavedGuard.markClean()")

        # -------------------------------------------------------------- 4
        ivanov.goto(BASE + BOARD + '?card=4')
        ivanov.click('.board-complete > summary')
        ivanov.fill('#board-complete-result', f'Комплектация проверена ({tag})')
        with ivanov.expect_navigation():
            ivanov.click('.board-complete__form button[type=submit]')
        check(f'{tag} 4 «Завершить» с результатом', 'Выполнена' in ivanov.inner_text('.board-drawer__head'))
        admin.goto(BASE + BOARD + '?card=4&tab=log')
        admin.click('.board-drawer__actions [data-confirm-url*="/reopen/"]')
        admin.wait_for_selector('[data-confirm-modal][open]')
        with admin.expect_navigation():
            admin.click('[data-confirm-modal-accept]')
        admin.goto(BASE + BOARD + '?card=4&tab=log')
        admin.wait_for_timeout(400)  # the drawer's slide-in
        log = admin.inner_text('[data-live-board-log]')
        check(f'{tag} 4 «Вернуть в работу» администратором', 'В работе' in admin.inner_text('.board-drawer__head'))
        check(f'{tag} 4 в логе обе записи', 'Задача выполнена' in log and 'Возвращена в работу' in log
              and log.index('Возвращена в работу') < log.index('Задача выполнена'), log[:200])
        admin.screenshot(path=os.path.join(SHOTS, f'log-complete-reopen-{tag}.png'))
        ivanov.context.close()
        admin.context.close()

    check('нет ошибок JavaScript и браузерных диалогов', not console_errors, '; '.join(console_errors))
    browser.close()

failed = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failed} of {len(results)} checks passed')
sys.exit(failed)
