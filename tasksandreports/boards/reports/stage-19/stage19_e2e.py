"""Browser check of stage 19: files in a card's «Чат».

Needs a real-time server on the data of `seed_demo.py` (the ASGI wrapper that
adds static files is stage 5's; Redis on 6379):

    REALTIME_ENABLED=true \\
    REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher \\
    REALTIME_REDIS_URL=redis://127.0.0.1:6379/0 \\
    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 \\
        asgi_dev:application --port 8765
    python tasksandreports/boards/reports/stage-19/stage19_e2e.py [screenshot-dir]

`RESET` (an environment variable) is a shell command run before each round
that puts the demo back as `seed_demo.py` left it (no message, no file).

Two rounds, 1920×1080 and 1536×864 (125 % Windows scaling of the same
monitor), each:
1. `ivanov` opens ZAP-1 on «Чат», picks a PDF and a picture with «📎» — two
   chips — types a message and sends it; then pastes a screenshot (Ctrl+V,
   a real `paste` event carrying an image) into an empty message — a chip
   «скриншот-…png» — and sends it alone; a file dropped on «Чат» becomes a
   chip too, and «×» takes it back. The feed shows the two thumbnails and the
   PDF row;
2. `admin1`, who had the card open on «Чат» before, sees both messages with
   their files without a reload;
3. «Только файлы»: the list of every file, newest first, the task's older
   attachment included; «К сообщению» goes back to the message;
4. `ivanov` deletes his PDF through «×» and the confirmation: «Файл удалён»;
5. the older task attachment stands in the feed, «добавлен файл к задаче»;
6. three tabs, and `?tab=files` opens «Чат» on its files; the tile «📎 N»;
7. «Описание» top to bottom — description, checklist, facts, tools,
   «Подписчики» — in the markup's order, which is the order of Tab.
Each check prints PASS/FAIL; the exit code is the number of failures.
"""

import os
import subprocess
import sys
import tempfile

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
expected_leave = {'count': 0}

WORK = tempfile.mkdtemp(prefix='stage19-files-')
PDF = os.path.join(WORK, 'чертёж-ZAP-1.pdf')
with open(PDF, 'wb') as handle:
    handle.write(b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n')
PICTURE = os.path.join(WORK, 'фото-заготовки.png')


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
    # The live client is up once it has synced.
    page.wait_for_timeout(1200)


def wait_for(page, predicate, timeout=8000):
    waited = 0
    while waited < timeout:
        if predicate(page):
            return True
        page.wait_for_timeout(250)
        waited += 250
    return predicate(page)


def chips(page):
    return [chip.inner_text().strip() for chip in page.locator('[data-chat-files-list] .board-chat__chip-name').all()]


PASTE = """async ({ selector, bytes }) => {
    const blob = new Blob([new Uint8Array(bytes)], { type: 'image/png' });
    const file = new File([blob], 'image.png', { type: 'image/png' });
    const data = new DataTransfer();
    data.items.add(file);
    const target = document.querySelector(selector);
    target.focus();
    const event = new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    return event.defaultPrevented;
}"""

DROP = """({ selector, name }) => {
    const data = new DataTransfer();
    data.items.add(new File(['%PDF-1.4 dropped'], name, { type: 'application/pdf' }));
    const target = document.querySelector(selector);
    const over = new DragEvent('dragover', { dataTransfer: data, bubbles: true, cancelable: true });
    target.dispatchEvent(over);
    const lit = target.closest('[data-board-tab-body="chat"]').classList.contains('is-dropping');
    target.dispatchEvent(new DragEvent('drop', { dataTransfer: data, bubbles: true, cancelable: true }));
    return { accepted: over.defaultPrevented, lit };
}"""


def run_round(browser, size):
    print(f'\n=== {size[0]}×{size[1]} ===')
    if RESET:
        subprocess.run(RESET, shell=True, check=True, stdout=subprocess.DEVNULL)
    admin = login(browser, 'admin1', size)
    ivanov = login(browser, 'ivanov', size)
    if not os.path.exists(PICTURE):
        # A real picture: the board itself, as a worker would photograph it.
        admin.goto(BASE + BOARD)
        admin.wait_for_load_state('networkidle')
        admin.screenshot(path=PICTURE, clip={'x': 0, 'y': 0, 'width': 900, 'height': 520})

    # `admin1` watches the card's «Чат» from the start.
    open_card(admin, '&tab=chat')
    open_card(ivanov, '&tab=chat')

    # 6 (first look). Three tabs.
    tabs = ivanov.eval_on_selector_all('[data-board-tab-link]', 'n => n.map(e => e.dataset.boardTabLink)')
    check('three tabs: Описание, Чат, Лог', tabs == ['description', 'chat', 'log'], tabs)

    # 5. The older task attachment in the feed.
    legacy = ivanov.locator('.board-discussion__message--attachment')
    check('the task\'s older attachment stands in the feed', legacy.count() == 1
          and 'спецификация-ZAP-1.pdf' in legacy.inner_text() and 'добавлен файл к задаче' in legacy.inner_text(),
          legacy.all_inner_texts())
    check('it downloads through the task', '/quality/tasks/' in (legacy.locator('a.board-file__download').get_attribute('href') or ''))
    shot(ivanov, '5-older-attachment', size)

    # 1. «📎»: a PDF and a picture, chosen in two picks, then a message.
    box = ivanov.locator('.board-chat__file-input').bounding_box()
    check('«📎 Файлы» stands for the file input, which is visually hidden',
          ivanov.locator('form[data-chat-files-ready]').count() == 1 and box['width'] <= 1
          and ivanov.locator('.board-chat__attach').is_visible(), box)
    file_input = ivanov.locator('[data-chat-files-input]')
    file_input.set_input_files(PDF)
    file_input.set_input_files(PICTURE)
    check('two picks — two chips', chips(ivanov) == ['чертёж-ZAP-1.pdf', 'фото-заготовки.png'], chips(ivanov))
    # A dropped file joins them, and «×» takes it back.
    dropped = ivanov.evaluate(DROP, {'selector': '[data-live-board-comments]', 'name': 'лишний.pdf'})
    check('a file dropped on «Чат» is taken and the area lit', dropped == {'accepted': True, 'lit': True}, dropped)
    check('it is a chip too', chips(ivanov)[-1:] == ['лишний.pdf'], chips(ivanov))
    ivanov.locator('[data-chat-file-remove="2"]').click()
    check('«×» takes it back before sending', chips(ivanov) == ['чертёж-ZAP-1.pdf', 'фото-заготовки.png'], chips(ivanov))
    posted = ivanov.evaluate('document.querySelector("[data-chat-files-input]").files.length')
    check('the form posts exactly the chosen files', posted == 2, posted)
    ivanov.locator('#board-comment-text').fill('Чертёж и фото заготовки для ZAP-1')
    shot(ivanov, '1-chosen', size)
    with ivanov.expect_navigation():
        ivanov.locator('form.board-chat__form button[type=submit]').click()
    ivanov.wait_for_load_state('networkidle')
    ivanov.wait_for_timeout(800)

    # A screenshot pasted into an empty message, sent alone.
    with open(PICTURE, 'rb') as handle:
        picture = list(handle.read())
    prevented = ivanov.evaluate(PASTE, {'selector': '#board-comment-text', 'bytes': picture})
    names = chips(ivanov)
    check('Ctrl+V of a screenshot becomes «скриншот-ГГГГ-ММ-ДД-ЧЧММСС.png»', prevented and len(names) == 1
          and names[0].startswith('скриншот-') and names[0].endswith('.png') and len(names[0]) == len('скриншот-2026-10-07-142530.png'),
          names)
    shot(ivanov, '1-pasted', size)
    with ivanov.expect_navigation():
        ivanov.locator('form.board-chat__form button[type=submit]').click()
    ivanov.wait_for_load_state('networkidle')
    ivanov.wait_for_timeout(800)
    messages = ivanov.locator('[data-board-chat-view="messages"] .board-discussion__message:not(.board-discussion__message--attachment)')
    check('two messages', messages.count() == 2, messages.count())
    check('on «Чат» nothing of «Описание» shows', ivanov.locator('.board-drawer__facts').first.is_hidden()
          and ivanov.locator('.board-checklist').is_hidden())
    thumbs = ivanov.locator('[data-board-chat-view="messages"] .board-chat__image img')
    check('two thumbnails (the photo and the screenshot)', thumbs.count() == 2, thumbs.count())
    loaded = ivanov.evaluate('[...document.querySelectorAll(".board-chat__image img")].every(i => i.complete && i.naturalWidth > 0)')
    check('the thumbnails load through the protected preview', loaded)
    height = ivanov.evaluate('Math.max(...[...document.querySelectorAll(".board-chat__image img")].map(i => i.getBoundingClientRect().height))')
    check('a thumbnail is at most 160 px high', height <= 160, height)
    row = ivanov.locator('[data-board-chat-view="messages"] .board-chat__files .board-file', has_text='чертёж-ZAP-1.pdf')
    check('the PDF is a row: badge, name, size, «Скачать»', row.count() == 1 and 'PDF' in row.inner_text()
          and 'Скачать' in row.inner_text(), row.all_inner_texts())
    check('the screenshot message has no text of its own', messages.nth(1).locator('.board-discussion__text').count() == 0)
    wide = ivanov.evaluate('document.documentElement.scrollWidth > window.innerWidth')
    check('no sideways scroll of the page', not wide)
    shot(ivanov, '1-chat-files', size)
    href = thumbs.first.evaluate('i => i.closest("a").href')
    preview = ivanov.context.request.get(href)
    check('a thumbnail opens the image inline, sandboxed', preview.ok and preview.headers.get('content-type') == 'image/png'
          and preview.headers.get('content-disposition') == 'inline'
          and preview.headers.get('content-security-policy') == 'sandbox', preview.headers)

    # 2. admin1 sees both without a reload.
    arrived = wait_for(admin, lambda p: p.locator('[data-board-chat-view="messages"] .board-chat__image img').count() == 2
                       and p.locator('.board-chat__files .board-file', has_text='чертёж-ZAP-1.pdf').count() == 1)
    check('admin1 sees the messages with their files without a reload', arrived)
    count = admin.locator('[data-board-chat-files-count]').inner_text().strip()
    check('and «Только файлы · 4» (three of the chat, one of the task)', count == '4', count)
    admin.locator('[data-live-board-comments]').evaluate('l => l.scrollTop = l.scrollHeight')
    admin.wait_for_timeout(300)
    shot(admin, '2-live', size)

    # 3. «Только файлы».
    admin.locator('[data-board-chat-mode-link="files"]').click()
    admin.wait_for_timeout(300)
    check('«Только файлы» switches in place', admin.locator('[data-board-chat-mode]').get_attribute('data-board-chat-mode') == 'files'
          and 'chat=files' in admin.url, admin.url)
    listed = [name.inner_text().strip() for name in admin.locator('.board-chat__all-files .board-file__name').all()]
    check('every file, newest first, the task\'s attachment last', len(listed) == 4 and listed[0].startswith('скриншот-')
          and listed[-1] == 'спецификация-ZAP-1.pdf', listed)
    meta = admin.locator('.board-chat__all-files .board-file__meta').first.inner_text()
    check('who and when beside each', 'Иван Иванов' in meta, meta)
    shot(admin, '3-only-files', size)
    goto = admin.locator('.board-chat__all-files .board-file', has_text='чертёж-ZAP-1.pdf').locator('[data-board-chat-goto]')
    goto.click()
    admin.wait_for_timeout(500)
    check('«К сообщению» shows the messages at that message',
          admin.locator('[data-board-chat-mode]').get_attribute('data-board-chat-mode') == 'messages'
          and admin.locator('.board-discussion__message.is-highlighted').count() == 1)

    # 4. ivanov deletes his PDF.
    ivanov.locator('.board-chat__files .board-file', has_text='чертёж-ZAP-1.pdf').locator('.board-file__delete').click()
    dialog = ivanov.locator('dialog[data-confirm-modal]')
    check('the shared confirmation asks first', dialog.is_visible() and 'чертёж-ZAP-1.pdf' in dialog.inner_text())
    with ivanov.expect_navigation():
        dialog.locator('[data-confirm-modal-accept]').click()
    ivanov.wait_for_load_state('networkidle')
    ivanov.wait_for_timeout(600)
    deleted = ivanov.locator('[data-board-chat-view="messages"] .board-file--deleted')
    check('«Файл удалён» in its place', deleted.count() == 1 and deleted.inner_text().strip() == 'Файл удалён')
    check('the PDF is gone from the message',
          ivanov.locator('[data-board-chat-view="messages"] .board-file__name', has_text='чертёж-ZAP-1.pdf').count() == 0)
    shot(ivanov, '4-deleted', size)
    gone = wait_for(admin, lambda p: p.locator('[data-board-chat-view="messages"] .board-file--deleted').count() == 1)
    check('admin1 sees «Файл удалён» without a reload', gone)
    check('and «Только файлы · 3»', admin.locator('[data-board-chat-files-count]').inner_text().strip() == '3')

    # 6. `?tab=files` and the tile.
    open_card(admin, '&tab=files')
    check('?tab=files opens «Чат» on its files', admin.locator('[data-board-drawer]').get_attribute('data-board-tab') == 'chat'
          and admin.locator('[data-board-chat-mode]').get_attribute('data-board-chat-mode') == 'files')
    tile = admin.locator('[data-card-id="1"] .board-tile__files')
    check('«📎 3» on the tile', tile.count() == 1 and ''.join(tile.inner_text().split()) == '📎3', tile.all_inner_texts())
    shot(admin, '6-tab-files', size)
    admin.locator('[data-card-id="1"]').screenshot(path=os.path.join(SHOTS, f'6-tile-{size[0]}.png'))

    # 7. «Описание» in the order of the screen, «Подписчики» below the tools.
    open_card(admin)
    with admin.expect_navigation():
        admin.locator('form.board-follow button').click()
    admin.wait_for_load_state('networkidle')
    boxes = [admin.locator(selector).first.bounding_box() for selector in (
        '.board-drawer__description', '.board-checklist', '.board-drawer__facts', '.board-drawer__tools',
        '.board-drawer__followers',
    )]
    tops = [box['y'] if box else None for box in boxes]
    check('description → checklist → facts → tools → «Подписчики», top to bottom',
          None not in tops and tops == sorted(tops), tops)
    in_order = admin.evaluate("""() => {
        const check = document.querySelector('.board-checklist__check');
        const edit = [...document.querySelectorAll('.board-drawer__tools a')].find((a) => a.textContent.trim() === 'Редактировать');
        return Boolean(check && edit && (check.compareDocumentPosition(edit) & Node.DOCUMENT_POSITION_FOLLOWING));
    }""")
    check('the checklist comes before «Редактировать» in the markup (the order of Tab)', in_order)
    followers = admin.locator('.board-drawer__followers').inner_text()
    check('«Подписчики» with the avatar «ОА»', 'Подписчики' in followers and 'ОА' in followers, followers)
    admin.locator('.board-drawer__pane').evaluate('p => p.scrollTop = p.scrollHeight')
    admin.wait_for_timeout(200)
    shot(admin, '7-description-order', size)
    for page in (admin, ivanov):
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
