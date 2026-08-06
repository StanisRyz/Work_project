# Разработка

Локальная установка не требует ни PostgreSQL, ни Redis, ни SMTP: по умолчанию
используется SQLite, real-time и email выключены.

## 1. Требования

- Python 3.13 или новее (CI проверяет 3.13);
- Git;
- зависимости из `requirements.txt` — Django, `psycopg`, `redis`, `uvicorn`.
  Точные версии живут только там; в документации они не дублируются;
- PostgreSQL — только если нужно проверить поведение на реальной СУБД;
- Redis — только если нужно проверить real-time.

Проект **не читает `.env`-файлы**: ни `python-dotenv`, ни `django-environ` не
установлены. Переменные должны быть экспортированы в окружение процесса —
оболочкой, конфигурацией запуска в IDE или process manager'ом.
`.env.example` — справочник значений, а не подключаемый файл.

## 2. Виртуальное окружение

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Зависимости

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 4. База данных

### SQLite (по умолчанию)

Переменные не нужны: `DATABASE_ENGINE` не задан → SQLite и файл
`BASE_DIR/db.sqlite3`.

`SQLITE_DB_PATH` позволяет указать другой файл, не подменяя рабочий, — это
нужно инструментам переноса и генератору тестового набора данных.

### PostgreSQL локально

Сервер, база и роль создаются вне Django. Приложение подключается к уже
существующей базе и никогда не создаёт её само.

```powershell
$env:DATABASE_ENGINE = "postgresql"
$env:DB_NAME = "quality_local"
$env:DB_USER = "quality_local"
$env:DB_PASSWORD = "<пароль из окружения>"
$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = "5432"
```

Скрытого отката к SQLite нет: пропущенная обязательная переменная или
неизвестное значение `DATABASE_ENGINE` — `ImproperlyConfigured` при старте.

Миграции обязаны работать на обоих бэкендах; это проверяет workflow
`database-compatibility` для SQLite и PostgreSQL.

## 5. Миграции и справочники

```powershell
python manage.py migrate
python manage.py seed_references
```

`seed_references` идемпотентна и создаёт справочники, без которых workflow не
работает: операции, виды дефектов, приоритеты, статусы актов (`CREATED_OTK`,
`KO_REVIEW`, `TO_ANALYSIS`, `OTK_REVIEW`, `ACTIONS_ASSIGNED`, `ARCHIVED`,
`CLOSED`, `CANCELLED`) и статусы задач (`IN_PROGRESS`, `COMPLETED`).
Демонстрационные учётные записи и акты она не создаёт.

## 6. Пользователи

Реальная учётная запись:

```powershell
python manage.py createsuperuser
```

Демонстрационный набор ролей — **только для локальной разработки**:

```powershell
python manage.py seed_demo_accounts
python manage.py seed_demo_acts
```

Он создаёт учётные записи с известным паролем (`otk_user`, `ko_user`,
`to_user`, `manager_user`, `admin_user`). В рабочей установке они запрещены:
`check_fresh_bootstrap` считает демонстрационного администратора блокирующей
проблемой.

## 7. Запуск

Обычные страницы:

```powershell
python manage.py runserver
```

`runserver` — WSGI: он не удерживает SSE-поток. Чтобы работал real-time,
запускайте ASGI-сервер:

```powershell
python -m uvicorn ecosystem.asgi:application --reload --port 8000
```

Real-time включается явно, вместе с транспортом:

```powershell
$env:REALTIME_ENABLED = "true"
$env:REALTIME_PUBLISHER_BACKEND = "realtime.backends.RedisRealtimePublisher"
$env:REALTIME_REDIS_URL = "redis://127.0.0.1:6379/0"
python manage.py check_realtime_transport
```

При `REALTIME_ENABLED=false` клиент Redis не создаётся вовсе, и сервер Redis
не нужен. Подробности — в [realtime.md](realtime.md).

Email по умолчанию выключен, а backend — консольный: письма печатаются в
терминал. Очередь обрабатывается вручную:

```powershell
python manage.py process_notification_deliveries --batch-size 100
```

## 8. Тесты

```powershell
python manage.py test
python manage.py test acts.tests.test_workflow
```

Тесты конкурентности (`acts/tests/test_concurrency.py`) пропускаются на SQLite,
потому что `select_for_update()` там не делает ничего. Проверять блокировки
нужно на PostgreSQL — их нельзя ослаблять или отключать ради зелёного прогона.

## 9. Диагностические команды

Все перечисленные команды только читают.

```powershell
python manage.py check                       # конфигурация и deployment-проверки
python manage.py makemigrations --check --dry-run
python manage.py check_documentation         # ссылки и структура документации
python manage.py check_logging               # обработчики, уровень, ротация, путь
python manage.py check_realtime_transport    # PING и round trip публикации в Redis
python manage.py check_fresh_bootstrap       # состояние чистой установки
python manage.py check_production_readiness  # сводная готовность
```

## 10. Частые локальные проблемы

| Симптом | Причина и что делать |
| --- | --- |
| `Required act status "..." is missing. Run seed_references first.` | справочники не заполнены — выполните `seed_references` |
| `Environment variable "DB_NAME" is required but not set.` | выбран `DATABASE_ENGINE=postgresql` без обязательных переменных |
| `Unsupported APP_ENV "..."` | допустимы только `development`, `test`, `production` |
| Переменные из `.env` не действуют | файл не читается автоматически — экспортируйте переменные в окружение |
| Колокольчик не обновляется без F5 | запущен `runserver` (WSGI) вместо Uvicorn, либо real-time выключен, либо Redis недоступен |
| `/realtime/events/` отвечает 204 | `REALTIME_ENABLED=false` — это штатное поведение |
| `/realtime/events/` отвечает 503 | Redis не отвечает на PING; проверьте `check_realtime_transport` |
| Тесты конкурентности пропущены | ожидаемо на SQLite; запустите их на PostgreSQL |
| `manage.py check` молчит о production-проблемах | deployment-проверки намеренно молчат вне `APP_ENV=production` |
