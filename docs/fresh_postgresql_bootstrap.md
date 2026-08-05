# Первый запуск на чистой PostgreSQL

Пошаговая процедура запуска приложения на **пустой** базе PostgreSQL.

## Что этот сценарий сознательно НЕ использует

Первый production-запуск — это чистая установка, а не перенос:

- **не** используется SQLite export/import bundle (`export_migration_bundle`,
  `import_migration_bundle`);
- **не** используется PostgreSQL rehearsal importer;
- **не** переносится текущая development-база `db.sqlite3`;
- **не** загружаются демонстрационные данные (`seed_demo_accounts`,
  `seed_demo_acts`) и синтетический набор `seed_performance_dataset`.

Инструменты переноса из [docs/postgresql_migration.md](postgresql_migration.md)
остаются для отдельного, отдельно согласованного сценария миграции
существующих данных. Здесь база создаётся пустой и наполняется работой
пользователей.

## Шаг 0. База данных создаётся вне Django

Приложение не создаёт базу и не заводит роли — у него для этого нет и не
должно быть прав. Пустая база, роль и права создаются администратором
PostgreSQL заранее; рекомендации по ролям — в
[PostgreSQL в production](postgresql_production.md).

Django ожидает уже существующую пустую базу, к которой у указанной роли есть
доступ.

## Шаг 1. Переменные окружения

Заполните окружение по образцу `.env.example`. Минимум для production:

```
APP_ENV=production
SECRET_KEY=<сгенерированный ключ, только из окружения>
DEBUG=false
ALLOWED_HOSTS=quality.example.internal
CSRF_TRUSTED_ORIGINS=https://quality.example.internal
APP_BASE_URL=https://quality.example.internal

DATABASE_ENGINE=postgresql
DB_NAME=<имя базы>
DB_USER=<роль приложения>
DB_PASSWORD=<пароль, только из окружения>
DB_HOST=<адрес>
DB_SSLMODE=require

STATIC_ROOT_PATH=/var/lib/quality/staticfiles
MEDIA_ROOT_PATH=/var/lib/quality/media

ENABLE_DEMO_RESET=false
BACKUP_POLICY_ACKNOWLEDGED=false
```

Ключ генерируется, например, так — вывод помещается в защищённое хранилище
секретов, а не в файл в репозитории:

```powershell
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Секреты никогда не коммитятся в Git и не передаются аргументами командной
строки.

## Шаг 2. Проверка конфигурации

```powershell
python manage.py check
```

Проверяет конфигурацию **без обращения** к PostgreSQL, Redis и SMTP. Здесь
выявляются DEBUG, SQLite, слабый ключ, пустой или wildcard `ALLOWED_HOSTS`,
HTTP-адреса, выключенные secure cookies, включённый demo reset и
противоречивая конфигурация email и real-time.

Дополнительно можно выполнить встроенные проверки Django для развёртывания:

```powershell
python manage.py check --deploy
```

## Шаг 3. Миграции

```powershell
python manage.py migrate
```

Выполняются ролью, у которой есть права на изменение схемы (owner/migration).
Рабочая роль приложения в штатной эксплуатации таких прав иметь не должна —
см. [PostgreSQL в production](postgresql_production.md).

## Шаг 4. Справочные данные

```powershell
python manage.py seed_references
```

Идемпотентно: команда безопасна для повторного запуска. Создаёт операции,
виды дефектов, статусы актов и задач, приоритеты. Демонстрационные учётные
записи и акты **не** создаются.

## Шаг 5. Администратор

```powershell
python manage.py createsuperuser
```

Реальная учётная запись с реальным паролем. Демонстрационный `admin_user` с
известным паролем в рабочей установке не создаётся — его наличие
`check_fresh_bootstrap` считает блокирующей проблемой.

## Шаг 6. Статика

```powershell
python manage.py collectstatic --noinput
```

Собирает CSS, JavaScript и изображения в `STATIC_ROOT` для публичной раздачи
веб-сервером. `MEDIA_ROOT` таким образом **не** раздаётся: вложения актов
доступны только через endpoint с проверкой прав.

## Шаг 7. Проверка чистой установки

```powershell
python manage.py check_fresh_bootstrap
```

Проверяет backend, соединение, применённые миграции, обязательные справочники,
`ActNumberSequence`, отсутствие демонстрационных и синтетических данных,
отсутствие демонстрационного администратора, выключенный demo reset,
`MEDIA_ROOT`, `STATIC_ROOT`, непротиворечивость real-time и email.

Наличие рабочих данных ошибкой не считается: команду можно запускать повторно
и после начала эксплуатации — она сообщит предупреждением, что установка уже
не является чистой. Имена пользователей и содержимое объектов не выводятся.

Ненулевой код возврата означает `BLOCKING` — продолжать нельзя.

## Шаг 8. Готовность к эксплуатации

```powershell
python manage.py check_production_readiness --json-report readiness.json
```

Сводный отчёт `PASS` / `WARNING` / `BLOCKING`: системные проверки, backend и
версия PostgreSQL, миграции, справочники, Redis (с настоящим PING при
включённом real-time), static/media, demo reset, email, fresh bootstrap и
подтверждение резервного копирования. При наличии `BLOCKING` код возврата
ненулевой.

Отчёт не содержит секретов, полных путей, имён пользователей и бизнес-данных,
но является рабочим артефактом и в Git не добавляется.

## Шаг 9. Проверка health endpoints

После запуска ASGI-сервера:

```powershell
curl -i https://quality.example.internal/health/live/
curl -i https://quality.example.internal/health/ready/
```

Ожидается `200 {"status": "ok"}` и `200 {"status": "ready"}`. Ответ `503
{"status": "unavailable"}` означает, что какая-то обязательная зависимость
недоступна; причина — в логе `deployment`, а не в теле ответа.

## Шаг 10. Ручная проверка

- вход реальной учётной записью;
- создание акта и прохождение хотя бы одного перехода workflow;
- загрузка и скачивание вложения (проверка прав доступа);
- при включённых уведомлениях — одно письмо на реальный ящик;
- при включённом real-time — `check_realtime_transport` и открытая вкладка с
  живым обновлением колокольчика.

## Порядок для копирования

```powershell
# 0. Пустая база и роль созданы администратором PostgreSQL заранее
# 1. Окружение заполнено по .env.example
python manage.py check
python manage.py migrate
python manage.py seed_references
python manage.py createsuperuser
python manage.py collectstatic --noinput
python manage.py check_fresh_bootstrap
python manage.py check_production_readiness
```
