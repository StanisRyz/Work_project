# Agile-доски, этап 1 из 6: фундамент (модели, тип задачи BOARD, сервисы, права)

Базовая ветка: `main`.

## Контекст
Вводим простые канбан-доски для Отдела продаж и ПДО. Карточка доски — это обычная
`tasks.Task` с новым `source_type=BOARD` плюс запись `boards.BoardCard` (место на доске).
Задачи с доски видны в «Задачах»; задачи из других источников на доску не попадают.
Этот этап — только данные, сервисы, права и выборка. НИКАКИХ views, URL, шаблонов, JS,
меню, уведомлений, realtime-событий кроме уже существующих `task.*`.

Перед началом прочитай целиком `AGENTS.md`, затем `docs/domain.md`, `docs/architecture.md`
и `tasksandreports/boards/plan.md`. Правила AGENTS.md обязательны. Образцы: приложение
`smk` (структура services/permissions/selectors), миграции `tasks/0018_bug_report_task.py`
и `tasks/0019_document_tasks.py` (как добавлять тип источника задачи).

## 1. Новое приложение `boards`
`boards/models.py`:

- `Board`: `name` (CharField 200), `description` (TextField, blank), `department`
  (FK `accounts.Department`, PROTECT — организационная пометка, прав не даёт),
  `owner` (FK User, PROTECT), `created_at`, `updated_at`.
- `BoardMember`: `board` (FK, CASCADE), `user` (FK User, PROTECT), `added_by` (FK User,
  PROTECT, null), `added_at`. UniqueConstraint (board, user).
- `BoardCard`: `board` (FK, PROTECT), `stage` (CharField, choices `BoardCard.Stage`:
  `TODO` «Сделать», `IN_PROGRESS` «В работе», `REVIEW` «На проверке»), `position`
  (PositiveIntegerField), `title` (CharField 200), `description` (TextField, blank),
  `created_by` (FK User, PROTECT), `created_at`, `updated_at`.
  Index (board, stage, position).

`boards/columns.py` — единственное место, где описаны 4 колонки доски:
`TODO`, `IN_PROGRESS`, `REVIEW` (хранятся в `BoardCard.stage`) и `DONE` «Готово»
(не хранится). Функция `card_column(card, task)`: `DONE`, если задача `COMPLETED`;
`None` (карточка не на доске), если `CANCELLED`; иначе `card.stage`.
Колонки «Готово» нет в `Stage` намеренно: карточка в ней ⇔ её задача выполнена.
Поэтому завершение со страницы задачи и переоткрытие администратором меняют колонку
без единого хука в `tasks.services`.

## 2. Новый тип задачи `BOARD` (`tasks`)
- `Task.SourceType.BOARD = 'BOARD', 'Доска'`.
- `Task.board_card` — FK на `boards.BoardCard`, PROTECT, null/blank, related_name `tasks`.
- Форма записи для `BOARD`: обязательны `board_card`, `department`. Должны быть
  NULL/пустыми: `act`, `root_analysis`, `source_action`, `protocol`, `protocol_action`,
  `smk_source`, `smk_action`, `bug_report`, `document_version`, `individual_assignee`,
  `workflow_stage`. На ВСЕХ остальных ветках `board_card IS NULL`. В `Task.clean()`
  `board_card` добавляется в `forbidden` в одном месте — так же, как `smk_*`, `bug_report`
  и `document_version`.
- Обнови `task_source_relations_match_source_type`: RemoveConstraint → AddField →
  AlterField(source_type) → AddConstraint, как в 0018/0019. Добавь
  `unique_board_card_task` (частичный, `source_type='BOARD'`, по `board_card`):
  одна задача на карточку.
- Миграция изменяет существующую таблицу на месте. Ни одной существующей записи не трогаем.
- `tasks/services.py`:
  - `create_board_card_task(card, assignee_ids, *, created_by, due_date, task_text,
    department)` через `_save_new_task()`; эмитит `task.created`, как соседи;
    `requires_attachment=False`.
  - `update_board_card_task(task, *, task_text, due_date, actor)`: только для
    `BOARD` в `IN_PROGRESS`, иначе `TaskWorkflowError`; `save(update_fields=[…,
    'updated_at'])`; эмитит `task.updated`.
  - Исполнители меняются только через существующий `replace_task_assignees()`.
- Реестр задач и страница задачи НЕ должны падать на задаче `BOARD`. Полное оформление
  (название источника, ссылка, фильтр) — этап 2; сейчас достаточно нейтрального
  отображения. Проверь тестом, что `tasks:list`, `tasks:detail` и Excel-экспорт реестра
  открываются с задачей `BOARD`.

## 3. Права: `boards/permissions.py` (единственное место правил)
- `BOARD_CREATOR_ROLES = frozenset({PDO, OPR, MANAGER, ADMIN})`, проверка через
  `accounts.roles.has_any_role()` + фолбэк настоящего `is_superuser`. Никаких
  сравнений `profile.role`, никаких проверок по коду подразделения.
- `can_view_board(user, board)` — любой аутентифицированный (задачи и так читают все).
- `can_create_board(user)` — роли выше.
- `can_manage_board(user, board)` — владелец или `is_act_admin(user)`.
- `can_work_on_board(user, board)` — активный участник доски или `is_act_admin(user)`.
- Завершение карточки — это `tasks.permissions.can_complete_task()`, без своего правила.

## 4. Сервисы: `boards/services.py` (единственный писатель моделей `boards`)
Исключение `BoardError` с понятным русским сообщением. Каждый сервис — `atomic()`.
Порядок блокировок: `Board.select_for_update()` → карточка → задача; права и состояние
перепроверяются ПОСЛЕ блокировки. Блокировки без `select_related()`.

- `create_board(*, name, department, owner, actor, description='', member_ids=())`:
  право `can_create_board(actor)`; владелец всегда участник.
- `add_board_members(board, user_ids, *, actor)` / `remove_board_member(board, user, *, actor)`:
  право `can_manage_board`; принимаются только активные пользователи. Владельца удалить
  нельзя. Нельзя удалить участника, который исполнитель открытой (`IN_PROGRESS`)
  карточки этой доски, — сначала переназначить.
- `compose_task_text(title, description)` — единственная функция, собирающая
  `Task.task_text` (заголовок; если есть описание — пустая строка и описание).
- `create_card(board, *, actor, title, due_date, assignee_ids, description='',
  stage=TODO)`: право `can_work_on_board`; заголовок не пустой после strip; срок
  обязателен; минимум один исполнитель; все исполнители — активные участники доски.
  Карточка встаёт в конец колонки; задача создаётся в той же транзакции через
  `create_board_card_task()` с `department=board.department`.
- `update_card(card, *, actor, title, description, due_date, assignee_ids)`: право
  `can_work_on_board`; отказ, если задача финальная (`status.is_final`); обновляет
  карточку, затем `update_board_card_task()` и `replace_task_assignees()`.
- `move_card(card, *, actor, stage, before_card_id=None)`: право `can_work_on_board`;
  `stage` — только TODO/IN_PROGRESS/REVIEW; отказ для финальной задачи (выполненную
  возвращает только администратор через существующий `tasks:reopen`).
  `before_card_id=None` означает в конец колонки; иначе встать перед этой карточкой,
  а чужая или несуществующая — `BoardError`. Позиции с шагом 1024; если промежутка
  нет — перенумеровать колонку под той же блокировкой доски.
- `complete_card(card, *, actor, execution_comment)`: тонкая обёртка над
  `tasks.services.complete_task()` (комментарий обязателен, право — его).
  `stage` карточки НЕ меняется.
- Логирование только через `ecosystem.logging_utils.log_event()`, только
  идентификаторы и outcome — без заголовков, описаний и имён.

## 5. Выборка: `boards/selectors.py`
`build_board_state(board, user)` → колонки в порядке `columns.py`, в каждой
список карточек с задачей, исполнителями и сроком:
- рабочие колонки — по `position`;
- «Готово» — выполненные задачи по `completed_at` убыванию;
- отменённые не показываются.

Плюс флаги `can_work`/`can_manage` для будущего UI. Число запросов не зависит от
числа карточек — закрепи тестом через `assertNumQueries`.
`boards_for_user(user)` → доски, где пользователь участник.

## 6. Django Admin
Только для чтения, как для остальных бизнес-моделей: просмотр `Board`,
`BoardMember`, `BoardCard`, без add/change/delete.

## 7. Тесты (`boards/tests/…`)
- `BOARD`-задача: валидная форма сохраняется; каждое запрещённое отношение и
  отсутствие `board_card`/`department` отклоняются и `clean()`, и
  CheckConstraint; `board_card` на задаче другого типа отклоняется;
  `unique_board_card_task` срабатывает.
- Права: каждая роль из `BOARD_CREATOR_ROLES` может создать доску, ОТК/КО/ТО/МАС —
  нет; роль, одолженная через `RoleSubstitution`, работает; неактивный профиль
  прав не даёт; суперпользователь может.
- Сервисы: создание карточки создаёт ровно одну задачу с верным текстом,
  сроком, подразделением и исполнителями; исполнитель не участник → отказ;
  участник без права / посторонний → отказ; `update_card` синхронизирует задачу;
  `move_card` ставит в конец и перед карточкой, перенумеровывает при исчерпании
  промежутка, отказывает финальной задаче и чужой `before_card_id`;
  `complete_card` требует комментарий и права исполнителя; удаление владельца и
  исполнителя открытой карточки запрещено.
- Выборка: выполненная со страницы задачи карточка оказывается в «Готово»;
  после `reopen_task()` возвращается в свою колонку; отменённая исчезает;
  число запросов постоянно.
- `tasks:list`, `tasks:detail`, Excel-экспорт реестра не падают на `BOARD`.

## 8. Документация (в том же изменении)
- `AGENTS.md`:
  - строка `boards` в «App ownership»;
  - строка `BOARD` в таблице источников задач, и «Ten values» → «Eleven values»;
  - абзац про `OPR`/`OZK`/…: теперь ОПР может создавать доски через
    `BOARD_CREATOR_ROLES`;
  - короткий раздел про доски: карточка = `BoardCard` + одна `Task`, «Готово»
    вычисляется, `columns.py`, сервисы, права.
- `docs/domain.md` и `docs/architecture.md` — соответствующие разделы. Без
  маркеров «этап N» вне `docs/archive/`.

## 9. Проверка перед пушем
    python manage.py check
    python manage.py makemigrations --check --dry-run
    python manage.py test
    python manage.py check_documentation
Миграции должны применяться с нуля. Существующие тесты не ослабляются и не
пропускаются.

## 10. Вне рамок этапа (не делать)
Views, URL, шаблоны, CSS, JS, меню, главная, уведомления, новые realtime-события,
оформление `BOARD` в `tasks/presentation.py` (кроме «не падать»), отмена карточки,
архив доски, свои колонки.

## 11. Git и отчёт
Закоммить и запушь в свою ветку, PR не создавать. Отчёт — по шаблону из
`tasksandreports/README.md`, в `tasksandreports/boards/reports/stage-01.md` своей ветки.
