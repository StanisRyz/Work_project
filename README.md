# Единая цифровая экосистема управления качеством

Веб-система для работы с актами о качестве (АОК) на производстве: акт проходит
маршрут ОТК → КО → ТО → ОТК, а утверждённые корректирующие мероприятия
превращаются в задачи с исполнителями и сроками. Роль пользователя определяет,
какие акты он видит и какие действия ему доступны.

## Что уже работает

**Акты.** Создание акта с данными партии и произвольным числом дефектов;
автоматическая нумерация `АОК-YYYY-NNN`; редактирование до передачи в КО;
маршрут `CREATED_OTK → KO_REVIEW → TO_ANALYSIS → OTK_REVIEW → ARCHIVED` с
возвратами на каждом этапе и обязательным комментарием возврата.

**Решения и анализ.** Решение КО по каждому дефекту; структурированный анализ
ТО: корневые причины, корректирующие мероприятия, подразделения, сроки и
активные исполнители.

**Задачи.** При утверждении акта для каждого корректирующего мероприятия
атомарно создаётся общая задача. Любой из назначенных исполнителей завершает
её один раз, обязательно указав результат выполнения.

**Права и видимость.** Роли ОТК, КО, ТО, руководителя и администратора.
Видимость проверяется на бэкенде: шаблоны используют готовый набор доступных
действий и никогда не решают вопрос доступа сами.

**Рабочая страница.** После входа пользователь попадает сразу в реестр актов
`/acts/`; страница «Главная» остаётся стартовой только для администратора.
Действия по маршруту подтверждаются модальным окном приложения (для возвратов —
с обязательным комментарием), а не диалогом браузера.

**Уведомления.** Внутренние уведомления с дедупликацией создаются в одной
транзакции с бизнес-событием; колокольчик в шапке показывает непрочитанные.
Email — отдельный канал: доставки складываются в очередь в базе и
отправляются отдельной командой, поэтому недоступный SMTP не мешает работе.

**Real-time.** Redis Pub/Sub и Server-Sent Events обновляют колокольчик,
реестры и открытый акт без перезагрузки. Событие сообщает только *что*
изменилось; содержимое всегда перезапрашивается обычным авторизованным
запросом. Пропущенные обновления досчитывает сверка ревизий `/realtime/sync/`,
есть fallback polling и одно соединение на пользователя вместо одного на
вкладку.

**Вложения.** Файлы актов лежат в protected media и выдаются только через
Django-вьюху с проверкой прав на каждый запрос.

**Готовность к эксплуатации.** Явный режим `APP_ENV`, проверки конфигурации
при старте, health и readiness endpoints, операционный журнал с `request_id`,
read-only команды проверки готовности.

## Стек

| Слой | Технология |
| --- | --- |
| Интерфейс | Django Templates + vanilla JavaScript, без сборщика и фреймворков |
| Приложение | Django, ASGI (Uvicorn) |
| База данных | PostgreSQL в production, SQLite в разработке |
| Real-time | Redis Pub/Sub → Server-Sent Events |
| Почта | корпоративный SMTP relay, отправка management-командой |

Точные версии — в `requirements.txt`. Полный список переменных окружения — в
`.env.example`. Celery, WebSocket/Channels, React и npm не используются.

## Архитектура

```
        браузер (Django Templates + vanilla JS)
          │ HTTP: страницы и фрагменты      │ EventSource: /realtime/events/
          ▼                                 ▼
   ┌────────────────────────────────────────────┐
   │            ASGI (Uvicorn) + Django         │
   │   views → permissions → services → models  │
   └──────┬──────────────┬──────────────┬───────┘
          │              │              │
     PostgreSQL        Redis        MEDIA_ROOT
   источник истины    Pub/Sub    protected media
                     best-effort
```

Приложения: `acts` (акты и workflow), `tasks` (задачи), `notifications`
(уведомления и очередь писем), `realtime` (события, SSE, сверка),
`references` (справочники), `accounts` (роли и подразделения),
`maintenance` (технические команды), `ecosystem` (настройки, health,
логирование). Подробнее — в [docs/architecture.md](docs/architecture.md).

## Быстрый локальный запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python manage.py migrate
python manage.py seed_references
python manage.py createsuperuser
python manage.py runserver
```

По умолчанию используется SQLite; PostgreSQL, Redis и SMTP для локального
запуска не нужны.

Демонстрационные роли для разработки (в рабочей установке запрещены):

```powershell
python manage.py seed_demo_accounts --confirm-demo
python manage.py seed_demo_acts
```

Чтобы попробовать real-time, нужен ASGI-сервер и Redis — `runserver` работает
через WSGI и не удерживает SSE-поток:

```powershell
$env:REALTIME_ENABLED = "true"
$env:REALTIME_PUBLISHER_BACKEND = "realtime.backends.RedisRealtimePublisher"
python manage.py check_realtime_transport
python -m uvicorn ecosystem.asgi:application --reload --port 8000
```

Подробности, переменные и типовые локальные проблемы — в
[docs/development.md](docs/development.md).

## Проверки

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python manage.py check_documentation
```

Дополнительно, только чтение: `check_logging`, `check_realtime_transport`,
`check_fresh_bootstrap`, `check_production_readiness`.

Production запускается только после успешных проверок:

```powershell
python manage.py check
python manage.py check_production_readiness
python -m uvicorn ecosystem.asgi:application
```

Тесты конкурентности пропускаются на SQLite, потому что `select_for_update()`
там ничего не делает; проверять блокировки нужно на PostgreSQL. Совместимость
с обоими бэкендами проверяет workflow `database-compatibility`.

## Документация

| Документ | О чём |
| --- | --- |
| [docs/index.md](docs/index.md) | карта документации: что читать перед каким изменением |
| [docs/architecture.md](docs/architecture.md) | слои, приложения, зависимости, источники истины |
| [docs/domain.md](docs/domain.md) | роли, видимость, статусы и переходы, задачи, уведомления |
| [docs/development.md](docs/development.md) | окружение, база, запуск, тесты, диагностика |
| [docs/realtime.md](docs/realtime.md) | контракт событий, Redis Pub/Sub, SSE, сверка, вкладки |
| [docs/deployment.md](docs/deployment.md) | production, чистый bootstrap PostgreSQL, proxy, Redis, SMTP |
| [docs/operations.md](docs/operations.md) | журнал, `request_id`, инциденты, health, email worker |
| [docs/backup_restore.md](docs/backup_restore.md) | состав копии и чек-лист восстановления |
| [AGENTS.md](AGENTS.md) | правила внесения изменений в код |

Исторические материалы — в [docs/archive/](docs/archive/README.md); они могут
не соответствовать текущему коду.

## Направления развития

- разделение ролей PostgreSQL и переход из пилота в постоянную эксплуатацию;
- нагрузочная проверка real-time на реальном сервере и решение о persistent
  connections или пуле соединений по её результатам;
- цифровой ОТК и микро-MES как следующие предметные области — отдельными
  Django-приложениями;
- экспорт актов в PDF/Word (сейчас доступна только печать средствами браузера);
- «Входной контроль» как полноценный тип акта со своим набором правил.

Крупные компоненты — React, WebSocket/Channels, Celery, внешние платформы
логирования — добавляются только по отдельному архитектурному решению.
