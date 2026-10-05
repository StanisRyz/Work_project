# Отчёт: boards, этап 01

## 1. Ветка и хеш последнего коммита

- Ветка: `claude/agile-boards-stage-1-qulr2i` (от `main` @ `81fdd82`).
- Коммит с кодом этапа: `032c4dd15ed76ce6a235894e0141c841f4549e15`.
- Этот отчёт добавлен следующим коммитом в той же ветке, без изменений кода.

## 2. Что сделано

- **§1 Приложение `boards`.**
  - Модели:
    - `Board`: `name`, `description`, `department` (PROTECT), `owner` (PROTECT), `created_at`, `updated_at`;
    - `BoardMember`: `UniqueConstraint` `unique_board_member` на `(board, user)`, `added_by` nullable;
    - `BoardCard`: `stage` с choices `TODO`/`IN_PROGRESS`/`REVIEW`, `position`, `title`, `description`, `created_by`, индекс `board_card_column_order` на `(board, stage, position)`.
  - `boards/columns.py` описывает четыре колонки в одном месте: `COLUMNS`, `WORK_STAGES`, `DONE`, `card_column(card, task)`. Правило: COMPLETED → `DONE`, CANCELLED → `None`, иначе `card.stage`.
- **§2 Тип задачи `BOARD`.**
  - `Task.SourceType.BOARD = 'BOARD', 'Доска'`, `Task.board_card` (FK, PROTECT, null/blank, related_name `tasks`), свойство `Task.is_board_task`.
  - Ветка `BOARD` в `task_source_relations_match_source_type`: `board_card` и `department` обязательны, все остальные отношения, `individual_assignee` и `workflow_stage` пустые. На всех восьми прежних ветках добавлено `board_card IS NULL`.
  - Частичный `unique_board_card_task`: `board_card` при `source_type='BOARD'`.
  - `Task.clean()`: правило для `BOARD`; запрет `board_card` на чужих типах добавляется в `forbidden` в одном месте.
  - `tasks/services.py`:
    - `create_board_card_task()` — через `_save_new_task()`, с `requires_attachment=False`, эмитит `task.created`;
    - `update_board_card_task()` — только `BOARD` в `IN_PROGRESS`, `update_fields` включают `updated_at`, эмитит `task.updated`, если что-то изменилось.
  - Исполнители меняются только через `replace_task_assignees()`.
- **§3 Права.** `boards/permissions.py`:
  - `BOARD_CREATOR_ROLES = {PDO, OPR, MANAGER, ADMIN}` проверяются через `has_any_role()`, плюс фолбэк `is_superuser`;
  - `can_view_board`, `can_create_board`;
  - `can_manage_board` — владелец или `is_act_admin`;
  - `can_work_on_board` — активный участник или `is_act_admin`.
- **§4 Сервисы.** `boards/services.py`:
  - Все функции из задания и `BoardError`. Каждая — внутри `atomic()`, блокировки Board → карточка → задача без `select_related()`, права и состояние перепроверяются после блокировок.
  - Шаг позиций `POSITION_STEP = 1024`, перенумерация колонки — под блокировкой доски.
  - `complete_card()` — обёртка над `complete_task()`, `stage` не меняет.
  - Логи через `log_event()`, только идентификаторы.
- **§5 Выборка.** `boards/selectors.py`:
  - `build_board_state()` — колонки в порядке `columns.py`; рабочие колонки по `position`, «Готово» по `completed_at` от новых к старым, отменённые не показываются; флаги `can_work`/`can_manage`.
  - `boards_for_user()`.
- **§6 Admin.** `Board` (с инлайном участников), `BoardMember` и `BoardCard` только для чтения через `ReadOnlyAdminMixin`.
- **§7 Тесты.** 57 тестов в `boards/tests/`, подробности в п. 7.
- **§8 Документация.**
  - `AGENTS.md`: строка `boards` в «App ownership», строка `BOARD` в таблице источников, «Ten» заменено на «Eleven», добавлено про `board_card` в `forbidden`, абзац про `OPR`, раздел «Boards (`boards`)».
  - `docs/domain.md`: `BOARD` в §8 и новый §13 «Доски».
  - `docs/architecture.md`: строка в таблице приложений и правило зависимости `boards` → `tasks`.

## 3. Изменённые и созданные файлы

Созданы:
- `boards/__init__.py`, `boards/apps.py`, `boards/models.py`, `boards/columns.py`,
  `boards/permissions.py`, `boards/services.py`, `boards/selectors.py`, `boards/admin.py`
- `boards/migrations/__init__.py`, `boards/migrations/0001_initial.py`
- `boards/tests/__init__.py`, `boards/tests/helpers.py`, `boards/tests/test_task_shape.py`,
  `boards/tests/test_permissions.py`, `boards/tests/test_services.py`,
  `boards/tests/test_selectors.py`, `boards/tests/test_task_registry.py`
- `tasks/migrations/0020_board_card_task.py`
- `tasksandreports/boards/reports/stage-01.md` (этот отчёт)

Изменены:
- `ecosystem/settings.py`: `boards` в `INSTALLED_APPS`.
- `tasks/models.py`, `tasks/services.py`, `tasks/presentation.py`.
- `templates/tasks/detail.html`: надпись «Карточка доски» над текстом задачи.
- `AGENTS.md`, `docs/domain.md`, `docs/architecture.md`.

## 4. Миграции

- **`boards.0001_initial`** — CreateModel `Board`, `BoardCard`, `BoardMember`, а также индекс и уникальное ограничение. Зависимости: `accounts.0016_userprofile_is_document_responsible` и `AUTH_USER_MODEL`.
- **`tasks.0020_board_card_task`** — пять операций:
  1. RemoveConstraint `task_source_relations_match_source_type`;
  2. AddField `board_card`;
  3. AlterField `source_type` (добавлен choice `BOARD`);
  4. AddConstraint `task_source_relations_match_source_type` (новая форма);
  5. AddConstraint `unique_board_card_task`.

  Зависимости: `boards.0001_initial`, `tasks.0019_document_tasks` и тот же набор, что у 0019 (`accounts.0016`, `acts.0030`, `bugs.0001`, `documents.0008`, `protocols.0007`, `references.0004`, `smk.0011`, `AUTH_USER_MODEL`). Таблица меняется на месте, существующие строки не затрагиваются.
- **Проверка с нуля:** миграции применились с нуля на чистой SQLite и на PostgreSQL 16 из контейнера. Откат до `tasks 0019` и повторное применение прошли на обеих базах.

## 5. Решения, которых не было в задании

- **Кто считается активным сотрудником:** `user.is_active` и активный профиль — то же условие, что в `active_users_for_roles()`. Оно действует для участников, исполнителей и `can_work_on_board`.
- **`before_card_id` приводится к `int`,** потому что форма пришлёт строку. Нечисловое значение, сама перемещаемая карточка, карточка с другой доски или из другой колонки дают `BoardError`.
- **Перенумерация колонки** происходит не только при исчерпании промежутка, но и при приближении позиции к пределу PostgreSQL `integer` (`MAX_POSITION`).
- **Конец колонки** считается по всем карточкам с этим `stage`, включая выполненные. После возврата задачи в работу карточка встаёт на прежнее место.
- **`TaskWorkflowError`** внутри сервисов досок перевыбрасывается как `BoardError` с тем же текстом.
- **Отображение в «Задачах».** На странице задачи у `BOARD` надпись «Карточка доски». Без неё шаблон показал бы «Решение протокола», то есть неверную информацию. В `describe_task_source()` для `BOARD` явно возвращается пустой источник.
- **`Board.updated_at`** обновляется только при изменении состава участников.

## 6. Отклонения от задания

- **Правки вне рамок этапа.** Одна правка шаблона (`templates/tasks/detail.html`) и одна ветка в `tasks/presentation.py`. Обе нужны только для того, чтобы задача `BOARD` не выдавала себя за протокольную. Требование «не падать» без них выполнялось, «нейтральное отображение» — нет.
- **Фильтр реестра по `BOARD`** работает автоматически, потому что `SOURCE_TYPE_CHOICES` строится из всех типов. Я его не отключал.

## 7. Тесты

Добавлено 57 тестов в `boards/tests/`:
- **`test_task_shape.py` (8):**
  - валидная форма сохраняется;
  - отсутствие `board_card`/`department` отклоняется и `clean()`, и CHECK;
  - каждое запрещённое отношение, `individual_assignee` и `workflow_stage` отклоняются обоими способами;
  - `board_card` на задаче `BUG` отклоняется;
  - `unique_board_card_task` срабатывает.
- **`test_permissions.py` (11):**
  - каждая роль из `BOARD_CREATOR_ROLES` может создать доску, ОТК/КО МП/ТО/МАС/СМК/ОЗК — нет;
  - одолженная роль действует, истёкшее замещение — нет;
  - неактивный профиль прав не даёт, суперпользователь может, аноним нет;
  - права просмотра, управления и работы на доске.
- **`test_services.py` (26):**
  - владелец всегда участник; создание доски без права запрещено;
  - добавляются только активные; участник не управляет составом;
  - нельзя удалить владельца и исполнителя открытой карточки (после выполнения карточки — можно);
  - создание карточки: ровно одна задача с верным текстом, сроком, подразделением, исполнителями и `created_by`, позиции в конце колонки, отказы (не участник, посторонний, пустые поля, колонка `DONE`);
  - `update_card` синхронизирует задачу и отказывает для финальной задачи;
  - `move_card`: в конец колонки, перед карточкой, id строкой, перенумерация, отказ для финальной задачи, для чужой, несуществующей или нечисловой `before_card_id`, для `DONE` и постороннего;
  - `complete_card` требует комментарий и права исполнителя и не меняет `stage`.
- **`test_selectors.py` (8):**
  - порядок колонок и флаги;
  - сортировка по `position`;
  - карточка, выполненная со страницы задачи, попадает в «Готово», а после `reopen_task()` возвращается в свою колонку;
  - «Готово» отсортировано от новых к старым;
  - отменённая карточка не показывается;
  - в карточке есть исполнители и срок;
  - число запросов не зависит от числа карточек (`assertNumQueries`).
- **`test_task_registry.py` (4):** `tasks:list` (вкладки `my`/`all` и фильтр `BOARD`), `tasks:detail` и Excel-экспорт открываются с задачей `BOARD`.

Итоговые строки проверочных команд (SQLite):

```
python manage.py check
System check identified no issues (0 silenced).

python manage.py makemigrations --check --dry-run
No changes detected

python manage.py test
Ran 1229 tests in 72.916s
OK (skipped=8)

python manage.py check_documentation
Документация корректна, предупреждений — 4.
```

Все четыре предупреждения `check_documentation` — о длине файлов. Для `AGENTS.md`, `README.md` и `docs/domain.md` они были и до этапа. Про `docs/realtime.md` (этот файл я не трогал) я не проверял.

Дополнительно тесты `boards`, `tasks`, `smk`, `bugs` и `documents` прогнаны на PostgreSQL 16. Падают 7 тестов `documents` с ошибкой `connection is closed`. Те же 7 падают и на чистом `main` (`81fdd82`) без изменений этапа, то есть к нему они не относятся. Остальное зелёное.

## 8. Известные проблемы, риски, сомнения

- **Одно правило в двух местах.** Условие «активный сотрудник» записано дважды: в `permissions.is_active_employee()` и в фильтрах `services._active_users()` / `_clean_assignees()`. При изменении правила править нужно оба места.
- **Двойное событие при `update_card`.** Если меняются и текст, и исполнители, уходят два события `task.updated`: от `update_board_card_task()` и от `replace_task_assignees()`. Вреда нет, но это лишнее событие.
- **Перемещение карточки** не создаёт realtime-событий: задача не меняется, а новые типы событий на этом этапе запрещены. Живое обновление доски нужно решить на этапе с UI.
- **Право завершать карточку.** Его определяет только `can_complete_task()`. Поэтому исполнитель, которого уже нет среди участников, может завершить свою задачу. Исключить его, пока карточка открыта, нельзя, так что на практике это редкий случай.
- **Отменённые карточки.** Сервиса отмены карточки нет (вне рамок этапа). В тестах отменённое состояние выставляется напрямую через `update()`.
- **Ошибки `documents` на PostgreSQL.** Падение 7 тестов существовало до этапа и не исправлялось.
