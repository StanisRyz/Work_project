# Подготовка к PostgreSQL

## Назначение

`ecosystem/settings.py` выбирает СУБД через переменную окружения `DATABASE_ENGINE`.
SQLite остаётся значением по умолчанию и не требует никаких дополнительных
переменных — обычный локальный запуск не меняется. `postgresql` — подготовленный,
но не активированный по умолчанию режим для последующего перехода на PostgreSQL.
Этот патч не переносит данные из `db.sqlite3` и не разворачивает сервер PostgreSQL —
он только даёт проекту возможность подключиться к уже существующей базе.

## Запуск с SQLite (по умолчанию)

Никаких переменных окружения не требуется:

```powershell
python manage.py check
python manage.py runserver
```

`DATABASE_ENGINE` не задан → используется `django.db.backends.sqlite3` и файл
`BASE_DIR / "db.sqlite3"`, как и раньше.

## Переменные для PostgreSQL

Сервер PostgreSQL, база данных и пользователь СУБД создаются отдельно, вне этого
проекта — например, `createdb` и `createuser` или соответствующие команды
администратора PostgreSQL. Проект их не создаёт и не устанавливает.

| Переменная | Обязательна | По умолчанию | Назначение |
| --- | --- | --- | --- |
| `DATABASE_ENGINE` | нет | `sqlite` | `sqlite` или `postgresql` |
| `SQLITE_DB_PATH` | нет | `BASE_DIR/db.sqlite3` | только при `sqlite`: путь к конкретному файлу базы; относительный путь разрешается от `BASE_DIR` |
| `DB_NAME` | да (при `postgresql`) | — | имя базы данных |
| `DB_USER` | да (при `postgresql`) | — | пользователь СУБД |
| `DB_PASSWORD` | да (при `postgresql`) | — | пароль пользователя СУБД |
| `DB_HOST` | нет | `127.0.0.1` | адрес сервера PostgreSQL |
| `DB_PORT` | нет | `5432` | порт сервера PostgreSQL |
| `DB_CONN_MAX_AGE` | нет | `0` | время жизни постоянного соединения, секунды |
| `DB_CONN_HEALTH_CHECKS` | нет | `false` | проверка живости переиспользуемых соединений |

Если `DATABASE_ENGINE=postgresql`, а `DB_NAME`, `DB_USER` или `DB_PASSWORD` не
заданы, запуск завершается понятной ошибкой `ImproperlyConfigured` с именем
отсутствующей переменной. Скрытого возврата к SQLite в этом случае нет.
Неподдерживаемое значение `DATABASE_ENGINE` тоже завершает запуск ошибкой с
перечислением допустимых вариантов (`sqlite`, `postgresql`).

Секреты (`DB_PASSWORD` и другие) не должны попадать в Git. Используйте `.env`
(уже в `.gitignore`) или переменные окружения деплой-среды. `.env.example` в
корне репозитория — это шаблон без реальных значений; он не подключается
автоматически, `.env`-файлы читает только внешняя среда выполнения.

## PowerShell: пример запуска с PostgreSQL

```powershell
$env:DATABASE_ENGINE = "postgresql"
$env:DB_NAME = "quality_ecosystem"
$env:DB_USER = "quality_ecosystem"
$env:DB_PASSWORD = "change-me"
$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = "5432"

python manage.py check
```

`manage.py check` — самый быстрый способ убедиться, что переменные считаны
корректно и Django способен собрать конфигурацию `DATABASES`, не запуская сам
сервер. Реальное подключение (`migrate` и далее) требует уже поднятого и
доступного сервера PostgreSQL с созданной базой и пользователем.

## Автоматическая проверка в CI

`.github/workflows/database-compatibility.yml` запускается на каждый push в
`main` и на каждый pull request и независимо проверяет проект на обеих СУБД:

- **SQLite job** — `ubuntu-latest`, Python 3.13, без каких-либо PostgreSQL-переменных.
- **PostgreSQL job** — `ubuntu-latest`, Python 3.13, временный service-контейнер
  `postgres:17` (база `quality_ecosystem_ci`, пользователь `quality_ci`,
  демонстрационный CI-пароль, health check через `pg_isready`). Job явно
  подключается к базе и проверяет `connection.vendor == "postgresql"` перед
  тем, как продолжить — если фактический backend другой, job завершается
  ошибкой.

Оба job выполняют одну и ту же последовательность: `manage.py check`,
`manage.py makemigrations --check --dry-run`, `manage.py migrate --noinput`,
затем полный `manage.py test --verbosity 2` (PostgreSQL job дополнительно
выводит `manage.py showmigrations` после миграции). Ни один шаг не использует
`continue-on-error`; любая ошибка миграции или теста останавливает job.

Контейнер PostgreSQL в CI — одноразовый: он создаётся заново для каждого
запуска workflow, не сохраняет данные между запусками и не является
production-развёртыванием. Успешное прохождение обоих job подтверждает
только совместимость кода и миграций с PostgreSQL — оно не означает, что
перенос реальных данных или production-развёртывание уже выполнены.

Чтобы повторить PostgreSQL-проверку локально, поднимите временный контейнер
с той же конфигурацией и укажите те же переменные:

```powershell
docker run --rm -e POSTGRES_DB=quality_ecosystem_ci -e POSTGRES_USER=quality_ci `
  -e POSTGRES_PASSWORD=ci_only_demo_password -p 5432:5432 postgres:17

$env:DATABASE_ENGINE = "postgresql"
$env:DB_NAME = "quality_ecosystem_ci"
$env:DB_USER = "quality_ci"
$env:DB_PASSWORD = "ci_only_demo_password"
$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = "5432"

python manage.py check
python manage.py migrate --noinput
python manage.py test --verbosity 2
```

## Гарантии при параллельной работе

Бизнес-операции подготовлены к реальной многопользовательской работе. Каждая
критическая операция открывает `transaction.atomic()`, повторно загружает и
блокирует строку акта через `select_for_update()`, и только после этого заново
проверяет права пользователя и текущий статус. Защищены:

- `send_to_ko`;
- `apply_ko_decision` (дефекты дополнительно перечитываются под блокировкой и
  сопоставляются по primary key, поэтому дефект чужого акта отклоняется);
- `return_to_otk`, `return_to_ko`, `return_to_to`;
- `apply_to_analysis` и `apply_structured_to_analysis`;
- `approve_act` (мероприятия, отделы, исполнители и сроки перечитываются под
  блокировкой; ровно одна общая задача на мероприятие);
- `close_act`;
- POST-редактирование акта: если акт уже вышел из `CREATED_OTK`, форма не
  сохраняется, пользователь получает сообщение и возвращается к акту.

Порядок блокировок одинаков во всём модуле: Act → дефекты / корневые проработки
/ мероприятия → задачи → история и уведомления. Второй параллельный или
устаревший запрос завершается контролируемым `ActWorkflowError` и не создаёт
повторную историю, комментарии возврата, задачи, исполнителей, уведомления или
email-доставки.

Номера актов выдаёт `ActNumberSequence` — техническая таблица с одной строкой на
год. Строка блокируется `select_for_update()` на время выдачи, поэтому
одновременное создание актов не может выдать одинаковый `АОК-YYYY-NNN`.

### PostgreSQL-only тесты

`acts/tests/test_concurrency.py` содержит настоящие многопоточные тесты
(`TransactionTestCase`): два одновременных `send_to_ko`, два одновременных
`approve_act`, несколько одновременных созданий актов и два одновременных
завершения одной задачи. Каждый поток использует собственное соединение,
потоки синхронизируются через `threading.Barrier` и имеют конечные timeout,
поэтому job в GitHub Actions не может зависнуть.

На SQLite эти тесты пропускаются: `select_for_update()` там документированно
ничего не делает, и результат теста ничего бы не доказывал. На PostgreSQL они
обязательны и не должны отключаться ради прохождения CI.

Успешное прохождение PostgreSQL job подтверждает только совместимость кода,
миграций и конкурентных гарантий. Оно **не** выполняет перенос рабочих данных
из `db.sqlite3` и не является production-развёртыванием.

## Инструменты переноса данных

Приложение `maintenance` содержит три команды, которые готовят перенос данных,
но не выполняют его автоматически:

- `export_migration_bundle --output <dir>` — собирает миграционный пакет из
  остановленной копии SQLite (`SQLITE_DB_PATH` указывает на копию);
- `import_migration_bundle --input <dir> [--dry-run]` — проверяет пакет и
  загружает его в **пустую** PostgreSQL;
- `verify_migration_bundle --input <dir> [--report <path>]` — итоговая сверка
  данных, связей и файлов.

Порядок действий, требования к целевой базе и разбор ошибок описаны в
[Перенос данных из SQLite в PostgreSQL](postgresql_migration.md).

## Чего этот патч не делает

- Не переносит существующие данные из `db.sqlite3` в PostgreSQL.
- Не устанавливает и не настраивает сам сервер PostgreSQL (кроме одноразового
  CI-контейнера, который существует только на время запуска workflow).
- Не меняет модели и не добавляет миграции — текущие миграции остаются
  совместимыми с обеими СУБД без изменений.
- Не включает PostgreSQL по умолчанию и не разворачивает production-окружение.
- Не настраивает Docker Compose приложения, Nginx, IIS или production WSGI.
