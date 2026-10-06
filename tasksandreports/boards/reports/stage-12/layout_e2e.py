"""Browser check of stage 12: the YouGile layout — boards on the left.

Needs a server on the data of `seed_demo.py` (the ASGI wrapper that adds
static files is stage 5's; real-time is optional):

    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-12/layout_e2e.py [screenshot-dir]

Every scenario runs at 1920×1080 and at 1536×864 (125 % Windows scaling of
the same monitor). Each check prints PASS/FAIL; the exit code is the number of
failures.
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:8765')
CHROMIUM = os.environ.get('CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
SIZES = ((1920, 1080), (1536, 864))

results = []
console_errors = []


def check(name, condition, detail=''):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


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


def nav_names(page):
    return page.eval_on_selector_all('.board-nav__list:not(.board-nav__archive *) .board-nav__link',
                                     'nodes => nodes.map(n => n.textContent.trim())')


def active_board(page):
    link = page.locator('.board-nav__link.is-active')
    return link.inner_text().strip() if link.count() else None


def geometry(page):
    """Where the rows of the page stand, and whether anything scrolls that should not."""
    return page.evaluate("""() => {
        const box = (selector) => { const e = document.querySelector(selector); if (!e) return null;
            const r = e.getBoundingClientRect(); return {top: Math.round(r.top), bottom: Math.round(r.bottom),
            left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width)}; };
        const row = document.querySelector('.board-columns');
        return {
            head: box('.board-head'), tabs: box('[data-live-board-tabs]'), filters: box('.board-filters'),
            layout: box('.board-layout'), nav: box('.board-nav'), panel: box('.board-panel'),
            pageScrollX: document.documentElement.scrollWidth > innerWidth,
            pageScrollY: document.documentElement.scrollHeight > innerHeight,
            rowScrollX: row ? row.scrollWidth > row.clientWidth + 1 : null,
            columns: [...document.querySelectorAll('.board-column')].map(c => Math.round(c.getBoundingClientRect().width)),
        };
    }""")


def rows_in_order(g):
    return (g['head']['bottom'] <= g['tabs']['top'] and g['tabs']['bottom'] <= g['filters']['top']
            and g['filters']['bottom'] <= g['layout']['top'])


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=CHROMIUM)

    for size in SIZES:
        tag = f'{size[0]}'
        # -------------------------------------------------------------- 1
        admin = login(browser, 'admin1', size)
        admin.evaluate('localStorage.clear()')
        admin.goto(BASE + '/work/boards/')
        check(f'{tag} 1 /work/boards/ открывает доску, а не реестр',
              '/work/boards/1/1/' in admin.url and admin.locator('.board-registry-table').count() == 0, admin.url)
        check(f'{tag} 1 слева две живые доски администратора',
              nav_names(admin) == ['Запуск заказов', 'Отгрузки'], str(nav_names(admin)))
        check(f'{tag} 1 текущая доска выделена', active_board(admin) == 'Запуск заказов')
        g = geometry(admin)
        check(f'{tag} 1 шапка, вкладки, фильтры и колонки — по порядку, без наложения', rows_in_order(g), str(g))
        check(f'{tag} 1 четыре колонки и панель досок без прокрутки страницы и ряда',
              not g['pageScrollX'] and not g['pageScrollY'] and g['rowScrollX'] is False and len(g['columns']) == 4,
              str(g))
        admin.screenshot(path=os.path.join(SHOTS, f'admin-board-{tag}.png'))

        admin.click('.board-nav__link:has-text("Отгрузки")')
        admin.wait_for_load_state()
        check(f'{tag} 1 переключение на вторую доску', active_board(admin) == 'Отгрузки'
              and admin.locator('.board-head h1').inner_text().strip() == 'Отгрузки', admin.url)
        admin.click('.board-nav__link:has-text("Запуск заказов")')
        admin.wait_for_load_state()
        admin.click('.board-tabs__link:has-text("Цех ПиР")')
        admin.wait_for_load_state()
        check(f'{tag} 1 вкладка «Цех ПиР» открыта', admin.locator('.board-tabs__item--active').inner_text().strip().startswith('Цех ПиР'))
        workshop_tab = admin.url.replace(BASE, '').split('?')[0]
        admin.check('.board-filters input[name=mine]')
        admin.wait_for_load_state()
        admin.wait_for_timeout(400)
        check(f'{tag} 1 фильтр «Мои» применён, «Сбросить» появилась',
              'mine=1' in admin.url and admin.locator('.board-filters__reset').count() == 1, admin.url)
        admin.goto(BASE + '/work/boards/')
        check(f'{tag} 3 /work/boards/ открывает последнюю поддоску',
              admin.url.replace(BASE, '').split('?')[0] == workshop_tab, f'{admin.url} (ожидалась {workshop_tab})')

        admin.goto(BASE + '/work/boards/1/1/')
        admin.click('[data-board-nav-toggle]')
        admin.wait_for_timeout(300)
        collapsed = admin.evaluate("document.querySelector('[data-board-shell]').classList.contains('board-shell--collapsed')")
        nav_width = geometry(admin)['nav']['width']
        admin.screenshot(path=os.path.join(SHOTS, f'admin-collapsed-{tag}.png'))
        admin.reload()
        admin.wait_for_timeout(300)
        kept = admin.evaluate("document.querySelector('[data-board-shell]').classList.contains('board-shell--collapsed')")
        check(f'{tag} 1 панель сворачивается в полосу и остаётся свёрнутой после перезагрузки',
              collapsed and kept and nav_width < 80, f'ширина {nav_width}')
        admin.click('[data-board-nav-toggle]')
        admin.wait_for_timeout(200)

        # The board's «⋯»: rename, then back.
        admin.click('.board-head__menu summary')
        admin.fill('#board-rename', 'Запуск заказов в работу')
        with admin.expect_navigation():
            admin.click('.board-head__menu button[type=submit]')
        check(f'{tag} 1 доска переименована из меню «⋯»',
              admin.locator('.board-head h1').inner_text().strip() == 'Запуск заказов в работу'
              and 'Запуск заказов в работу' in nav_names(admin))
        admin.click('.board-head__menu summary')
        admin.fill('#board-rename', 'Запуск заказов')
        with admin.expect_navigation():
            admin.click('.board-head__menu button[type=submit]')
        check(f'{tag} 1 в шапке пять кружков и «+3»',
              admin.locator('.board-head__members .board-avatar').count() == 6
              and admin.locator('.board-avatar--more').inner_text().strip() == '+3')

        # -------------------------------------------------------------- 4
        admin.goto(BASE + '/work/boards/create/')
        check(f'{tag} 4 «Новая доска» в той же рамке, без выделенной доски',
              admin.locator('.board-nav').count() == 1 and active_board(admin) is None
              and admin.locator('form.board-form input[name=name]').count() == 1)
        admin.screenshot(path=os.path.join(SHOTS, f'new-board-{tag}.png'))

        # -------------------------------------------------------------- 5
        admin.goto(BASE + '/work/boards/1/1/')
        admin.locator('.board-tile').nth(1).click()
        admin.wait_for_load_state()
        admin.wait_for_timeout(400)
        g = geometry(admin)
        check(f'{tag} 5 карточка открыта: строки не наезжают друг на друга', rows_in_order(g), str(g))
        check(f'{tag} 5 карточка открыта: страница не прокручивается', not g['pageScrollX'] and not g['pageScrollY'], str(g))
        admin.screenshot(path=os.path.join(SHOTS, f'card-after-{tag}.png'))
        admin.context.close()

        # -------------------------------------------------------------- 2
        ivanov = login(browser, 'ivanov', size)
        ivanov.goto(BASE + '/work/boards/')
        check(f'{tag} 2 участник видит слева только свою доску',
              nav_names(ivanov) == ['Запуск заказов'] and ivanov.locator('.board-nav__add').count() == 0,
              str(nav_names(ivanov)))
        check(f'{tag} 2 у участника нет меню доски', ivanov.locator('.board-head__menu').count() == 0)
        ivanov.screenshot(path=os.path.join(SHOTS, f'member-board-{tag}.png'))
        ivanov.context.close()

        loner = login(browser, 'loner', size)
        loner.goto(BASE + '/work/boards/')
        check(f'{tag} 3 без досок — «Вас пока не добавили ни на одну доску»',
              'Вас пока не добавили ни на одну доску' in loner.content() and nav_names(loner) == [])
        loner.context.close()

    check('нет ошибок JavaScript и браузерных диалогов', not console_errors, '; '.join(console_errors))
    browser.close()

failed = sum(1 for _, ok in results if not ok)
print(f'\n{len(results) - failed} of {len(results)} checks passed')
sys.exit(failed)
