# Развёртывание

Документ описывает production-контур приложения: обязательные компоненты,
первый запуск на чистой PostgreSQL, ASGI, reverse proxy, HTTPS, файлы, Redis,
почту, health и проверки готовности.

Он не устанавливает PostgreSQL и Redis, не создаёт роли базы, не настраивает
сертификаты и службы ОС: это задачи ИТ-службы на выбранном сервере.

## 1. Production environment

`APP_ENV` — единственный явный переключатель. Ничего не выводится из `DEBUG` и
не угадывается по наличию переменных; неизвестное значение отклоняется при
старте.

| Значение | Назначение |
| --- | --- |
| `development` | по умолчанию: SQLite, DEBUG, локальный ключ |
| `test` | автотесты и CI |
| `production` | пилот и рабочая эксплуатация |

При `APP_ENV=production` перечисленное проверяется **на импорте настроек**,
то есть процесс не стартует вовсе, а не падает на первом запросе:

| Правило | Причина |
| --- | --- |
| `SECRET_KEY` обязателен и приходит только из окружения | подпись сессий и CSRF |
| опубликованный development-ключ запрещён | он лежит в Git и известен всем |
| пустой, короткий (< 50 символов) и очевидно тестовый ключ запрещён | предсказуемая подпись |
| `DEBUG` обязан быть `false` | DEBUG раскрывает трассировки, настройки и SQL |
| `DATABASE_ENGINE=postgresql` | SQLite не даёт конкурентной записи и блокировок строк |
| `ALLOWED_HOSTS` не пуст и без `*` | wildcard отключает проверку заголовка `Host` |
| `APP_BASE_URL` — абсолютный `https://` | из него строятся ссылки в письмах |
| `CSRF_TRUSTED_ORIGINS` — абсолютные `https://` | иначе CSRF-проверка бесполезна |

Ни ключ, ни пароль, ни строка подключения никогда не попадают в текст ошибки.

### Группы критичных настроек

Полный список переменных с комментариями — в **`.env.example`** и
`ecosystem/settings.py`. Здесь перечислены только группы, которые обязательно
нужно осознанно заполнить:

| Группа | Что в неё входит |
| --- | --- |
| Режим и идентичность | `APP_ENV`, `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `APP_BASE_URL` |
| Безопасность и cookies | secure/httponly/samesite для сессии и CSRF, `SECURE_SSL_REDIRECT`, HSTS, `X_FRAME_OPTIONS` |
| Reverse proxy | `TRUST_X_FORWARDED_PROTO`, `USE_X_FORWARDED_HOST` |
| PostgreSQL | `DATABASE_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_SSLMODE`, runtime-таймауты, `DB_CONN_MAX_AGE` |
| Файлы | `STATIC_ROOT_PATH`, `MEDIA_ROOT_PATH` |
| Real-time | `REALTIME_ENABLED`, `REALTIME_PUBLISHER_BACKEND`, `REALTIME_REDIS_URL`, `REALTIME_CHANNEL_PREFIX`, тайминги |
| Email | `EMAIL_NOTIFICATIONS_ENABLED`, `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, TLS/SSL, `DEFAULT_FROM_EMAIL`, параметры очереди |
| Журнал | `LOG_LEVEL`, `LOG_TO_FILE`, `LOG_TO_CONSOLE`, `LOG_FILE_PATH`, ротация, `APP_RELEASE` |
| Операционные флаги | `ENABLE_DEMO_RESET`, `BACKUP_POLICY_ACKNOWLEDGED`, `REDIS_NETWORK_IS_TRUSTED` |

Секреты читаются только из окружения и никогда не коммитятся. Проект не читает
`.env`-файлы автоматически — переменные должен экспортировать process manager.

## 2. Обязательные компоненты

| Компонент | Обязателен | Примечание |
| --- | --- | --- |
| PostgreSQL | да | production-база; SQLite остаётся вариантом только для разработки |
| ASGI-сервер (Uvicorn) | да | SSE — длительное соединение, WSGI его не держит |
| Reverse proxy с HTTPS | да | внешний доступ только по HTTPS |
| Redis | только для real-time | при `REALTIME_ENABLED=false` не нужен вовсе |
| Корпоративный SMTP relay | только для email-уведомлений | при выключенных уведомлениях не требуется |
| Планировщик ОС | только для email-очереди | systemd timer или Windows Task Scheduler |

Celery, Docker, Kubernetes и PgBouncer в проекте не используются.

## 3. Чистый bootstrap PostgreSQL

Первый production-запуск — **чистая установка, а не перенос**. Он сознательно
не использует SQLite export/import bundle, rehearsal importer, текущую
development-базу `db.sqlite3` и демонстрационные или синтетические данные;
инструменты переноса остаются отдельным согласованным сценарием
([архив](archive/postgresql_migration.md)).

### Шаг 0. База создаётся вне Django

Приложение не создаёт базу и не заводит роли — у него нет и не должно быть на
это прав. Пустая база, роль и права создаются администратором PostgreSQL
заранее (см. раздел 9).

### Шаг 1. Окружение

Заполните переменные по `.env.example`. Ключ генерируется так, а вывод
помещается в защищённое хранилище секретов:

```powershell
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### Шаги 2–8

```powershell
python manage.py check
python manage.py check --deploy
python manage.py migrate
python manage.py seed_references
python manage.py createsuperuser
python manage.py collectstatic --noinput
python manage.py check_fresh_bootstrap
python manage.py check_production_readiness --json-report readiness.json
```

- `check` проверяет конфигурацию **без обращения** к PostgreSQL, Redis и SMTP;
- `migrate` выполняется ролью с правом изменять схему, а не рабочей ролью;
- `seed_references` идемпотентна и создаёт полный набор статусов актов и задач,
  операций, видов дефектов и приоритетов; демонстрационные данные — нет;
- `createsuperuser` создаёт **реальную** учётную запись: демонстрационный
  `admin_user` с известным паролем в рабочей установке — блокирующая проблема;
- `collectstatic` собирает статику в `STATIC_ROOT`; `MEDIA_ROOT` так **не**
  раздаётся.

### Проверка `ActNumberSequence`

`check_fresh_bootstrap` сравнивает `ActNumberSequence.last_value` с фактически
выданным максимальным номером `АОК-YYYY-NNN` за каждый год — одним запросом,
независимо от количества актов и лет.

| Ситуация | Результат |
| --- | --- |
| Актов и строк счётчика нет; строка есть, а актов за год нет | `PASS` |
| `last_value` больше или равен фактическому максимуму | `PASS` |
| Акты за год есть, а строки счётчика нет | `BLOCKING` |
| `last_value` меньше фактического максимума | `BLOCKING` |
| `last_value` отрицательный или иначе невозможен | `BLOCKING` |

Номера нестандартного исторического формата в подсчёте не участвуют. Проверка
только читает: она никогда не создаёт отсутствующую строку и не поднимает
`last_value` сама, а в сообщении называет только год и агрегированные значения.

**Ручное исправление** выполняется администратором осознанно, через
`manage.py shell`:

```python
from acts.models import ActNumberSequence
ActNumberSequence.objects.filter(year=<год>).update(last_value=<фактический максимум>)
# либо, если строки для года ещё нет:
ActNumberSequence.objects.get_or_create(year=<год>, defaults={'last_value': <фактический максимум>})
```

Автоматической команды для этого нет намеренно: несогласованное изменение
счётчика может выдать уже занятый номер.

## 4. ASGI и Uvicorn

```powershell
python -m uvicorn ecosystem.asgi:application --host 127.0.0.1 --port 8000
```

- `ASGI_APPLICATION = 'ecosystem.asgi.application'` уже задан; при включённом
  real-time его отсутствие — блокирующая ошибка `ecosystem.E019`;
- число воркеров подбирается так, чтобы долгоживущее соединение не занимало
  воркер целиком: asyncio-воркер держит тысячи соединений, но у каждого процесса
  свой лимит файловых дескрипторов;
- **несколько воркеров меняют схему логирования** — см.
  [operations.md](operations.md). Обычные страницы обслуживает тот же процесс.

## 5. Reverse proxy и требования SSE

Прокси обязан пропускать поток `/realtime/events/` без накопления:

- **буферизация выключена**: приложение отдаёт `X-Accel-Buffering: no`, но
  прокси обязан это уважать. Кэширование, сжатие и трансформация ответа тоже
  выключены;
- **read/idle timeout заметно больше heartbeat** (по умолчанию 25 с) — например
  120 с. Живое соединение не должно разрываться как «молчащее»;
- HTTP/1.1 или HTTP/2 без принудительного закрытия keep-alive, а лимит
  одновременных соединений на upstream покрывает «пользователи × вкладки».

При доступном `BroadcastChannel` активна одна SSE-подписка на пользователя, а
не на вкладку: `соединений ≈ активные пользователи × (1 + доля браузеров без
BroadcastChannel)`. Для пилота порядка 50 пользователей это десятки соединений.
Планировать стоит с запасом ×2 на переподключения и на приватный режим, где
`localStorage` и `BroadcastChannel` могут быть недоступны.

## 6. HTTPS и cookies

- внешний доступ только по HTTPS: SSE передаёт события пользователя;
- `SESSION_COOKIE_SECURE` и `CSRF_COOKIE_SECURE` в production включены по
  умолчанию; `SameSite=Lax` достаточно — EventSource обращается к своему
  origin;
- **HSTS никогда не включается сам.** Браузер помнит заголовок долго, и
  ошибочно выставленный большой срок нельзя быстро отменить. Включайте только
  после того, как HTTPS точно работает для каждого имени, и начинайте с
  небольшого срока;
- `SECURE_PROXY_SSL_HEADER` устанавливается **только** при
  `TRUST_X_FORWARDED_PROTO=true`. Включайте флаг, лишь если TLS терминируется
  на прокси, прокси **перезаписывает** `X-Forwarded-Proto`, и приложение
  недоступно снаружи напрямую. Иначе любой клиент подделает заголовок и
  заставит Django считать открытый HTTP защищённым. То же относится к
  `USE_X_FORWARDED_HOST`.

## 7. Статика и protected media

| | STATIC | MEDIA |
| --- | --- | --- |
| Что | CSS, JavaScript, изображения интерфейса | вложения актов |
| Переменная | `STATIC_ROOT_PATH` | `MEDIA_ROOT_PATH` |
| Как раздаётся | публично, веб-сервером | **только** через `acts.views.act_download_attachment` |
| Доступ | открытый | проверка прав на каждый запрос |

**`MEDIA_ROOT` не должен быть доступен веб-серверу напрямую**: публичная
раздача обошла бы видимость акта полностью. Совпадение `STATIC_ROOT` и
`MEDIA_ROOT` — блокирующая ошибка `ecosystem.E012`.

`ENABLE_DEMO_RESET=false` по умолчанию: при выключенном флаге маршрут
`/acts/clear-all/` **не регистрируется**, прямой запрос получает обычный 404.
В production флаг принудительно выключен, а попытка его включить — блокирующая
ошибка `ecosystem.E013`.

## 8. Redis

- отдельная служба, а не процесс внутри приложения; слушает внутренний
  интерфейс или закрыт сетевыми правилами;
- `rediss://` при передаче по сети либо подтверждённая доверенная внутренняя
  сеть (`REDIS_NETWORK_IS_TRUSTED=true`); плоский `redis://` без подтверждения
  даёт предупреждение `ecosystem.W005`;
- **только транспорт**: persistence для real-time не требуется, события не
  хранятся и не переигрываются;
- отдельная база (`/0`, `/1`, …) или отдельный `REALTIME_CHANNEL_PREFIX` на
  каждую среду, чтобы тестовая и рабочая не пересекались.

После запуска:

```powershell
python manage.py check_realtime_transport
```

Затем ручная проверка: две вкладки под одним пользователем, действие второй
учётной записью, обновление колокольчика без F5. Подробности — в
[realtime.md](realtime.md).

## 9. PostgreSQL: роли и соединение

Рекомендуемая модель — три роли. Готовых SQL-команд с реальными именами и
паролями документ не содержит: их формирует администратор PostgreSQL.

| Роль | Назначение |
| --- | --- |
| владелец | владеет базой и схемой; используется **только** для `migrate` |
| рабочая роль приложения | `DB_USER`: `SELECT`/`INSERT`/`UPDATE`/`DELETE` на таблицах, `USAGE`/`SELECT`/`UPDATE` на последовательностях, `USAGE` на схеме |
| роль резервного копирования | только чтение, для `pg_dump` |

Рабочей роли **не нужны** `CREATE` на схеме, `DROP` и `ALTER`: приложение не
меняет структуру во время работы. Порядок обновления — остановить приложение,
выполнить `migrate` под ролью владельца, вернуть работу под рабочей ролью. Для
первого пилота допустимо задокументированное упрощение — одна совмещённая роль;
зафиксируйте, что компромисс принят, почему и когда планируется разделение.
Разделение обязательно при переходе из пилота в постоянную эксплуатацию.

| Переменная | Рекомендация | Примечание |
| --- | --- | --- |
| `DB_SSLMODE` | `require` и выше | `disable`/`allow`/`prefer` допускают незашифрованное соединение (`ecosystem.W003`) |
| `DB_APPLICATION_NAME` | значимое имя | видно в `pg_stat_activity` |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | без таймаутов зависший запрос держит блокировки неограниченно |
| `DB_LOCK_TIMEOUT_MS` | `10000` | `0` отключает таймаут (`ecosystem.W004`) |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | `60000` | забытая открытая транзакция |
| `DB_CONN_MAX_AGE` | `0` под ASGI | долгий SSE-поток не должен удерживать соединение с базой |

Persistent connections, встроенный пул и PgBouncer добавляются только по
результатам реального измерения (`check_sse_db_connections`), а не по
умолчанию. `max_connections` сервера должен покрывать процессы ASGI × их
параллелизм + миграции + резервное копирование + запас на переподключения.

## 10. Корпоративный SMTP и email worker

Пользовательское действие, запись `Notification` и её `NotificationDelivery`
создаются в одной транзакции. SMTP в HTTP-запросе не вызывается.

Отдельное серверное задание **раз в минуту** запускает одну пачку:

```powershell
python manage.py process_notification_deliveries --batch-size 100
```

Команда восстанавливает зависшие `processing`, выбирает не более указанного
числа готовых `pending`-доставок и завершает работу. Условный атомарный перевод
`pending → processing` даёт право отправки только одному из пересекающихся
процессов; `sent`, `failed` и `skipped` повторно не выбираются. Интервал
восстановления должен заведомо превышать SMTP timeout.

Проверки конфигурации email никогда не подключаются к SMTP: недоступный
почтовый сервер не должен блокировать развёртывание. `EMAIL_HOST_USER` и
`EMAIL_HOST_PASSWORD` не считаются обязательными — корпоративный relay может
работать по IP allow-list без аутентификации.

### Linux: systemd

```bash
sudo install -d -m 0750 /etc/quality-ecosystem
sudo install -m 0600 deploy/systemd/email-queue.env.example /etc/quality-ecosystem/email-queue.env
sudo editor /etc/quality-ecosystem/email-queue.env

sudo install -m 0644 deploy/systemd/quality-email-queue.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/quality-email-queue.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quality-email-queue.timer
```

`WantedBy=timers.target` включает таймер после перезагрузки, `Persistent=true`
выполняет пропущенный запуск, и systemd не создаёт второй экземпляр уже
активного oneshot-service. В unit-файле укажите отдельную непривилегированную
учётную запись.

### Windows Server: Task Scheduler

```powershell
$credential = Get-Credential 'DOMAIN\quality-service'
.\deploy\windows\Register-EmailQueueTask.ps1 `
  -ProjectPath 'D:\Apps\QualityEcosystem' `
  -PythonPath 'D:\Apps\QualityEcosystem\.venv\Scripts\python.exe' `
  -TaskName 'QualityEcosystem-EmailQueue' `
  -LogDirectory 'D:\Logs\QualityEcosystem' `
  -Credential $credential
```

Пароль передаётся API Task Scheduler и не записывается в скрипт.
`MultipleInstances=IgnoreNew` не запускает второй экземпляр поверх работающего.
Удаление — `deploy\windows\Unregister-EmailQueueTask.ps1`.

### Порядок включения SMTP

1. оставьте `EMAIL_NOTIFICATIONS_ENABLED=false` и таймер/задание выключенным;
2. заполните SMTP-параметры в окружении служебной учётной записи;
3. включите `EMAIL_NOTIFICATIONS_ENABLED=true`, создайте одну тестовую доставку
   на тестовый ящик и выполните команду **вручную**;
4. убедитесь в статусе `sent` и в записях журнала, и только затем включите
   таймер или задание.

При `EMAIL_NOTIFICATIONS_ENABLED=false` новые доставки создаются сразу со
статусом `skipped`: после включения SMTP они не превращаются в накопленную
рассылку.

## 11. Health и readiness

| Endpoint | Что означает | Что проверяет |
| --- | --- | --- |
| `GET /health/live/` | процесс жив | **ничего** — ни базы, ни Redis, ни диска |
| `GET /health/ready/` | готов принимать трафик | `SELECT 1`, отсутствие неприменённых миграций, Redis PING (только при real-time на Redis-backend), `MEDIA_ROOT`, `STATIC_ROOT` |

Оба: без авторизации, только `GET` и `HEAD`, `Cache-Control: no-store`.
Readiness отвечает `200 {"status": "ready"}` либо `503
{"status": "unavailable"}` и **ничего больше**: ни SQL, ни текста исключения,
ни путей, хостов, имён пользователей, Redis URL и учётных данных. Подробности
пишутся в logger `deployment`. Liveness намеренно не зависит от базы, а
readiness ничего не изменяет — см. [operations.md](operations.md).

## 12. Production readiness

`check_fresh_bootstrap` и `check_production_readiness` строго только читают,
возвращают ненулевой код при `BLOCKING` и никогда не печатают имя пользователя
или содержимое объектов.

`check_fresh_bootstrap` — состояние чистой установки: backend, соединение,
миграции, обязательные справочники, `ActNumberSequence`, отсутствие
демонстрационных и синтетических данных и демонстрационного администратора,
выключенный demo reset, `MEDIA_ROOT`, `STATIC_ROOT`, непротиворечивость
real-time и email. Наличие **рабочих** данных ошибкой не считается: команда
остаётся повторно запускаемой и сообщает об этом предупреждением.

`check_production_readiness` — сводный отчёт `PASS` / `WARNING` / `BLOCKING`:
системные проверки Django, backend и версия PostgreSQL, миграции, справочники,
`ActNumberSequence`, real-time с настоящим PING, static/media, demo reset,
email, логирование и подтверждение резервного копирования. Каждый логический
раздел появляется ровно один раз. JSON-отчёт не содержит секретов, полных
путей, имён пользователей и бизнес-данных, но остаётся рабочим артефактом и в
Git не добавляется.

Системные проверки `ecosystem/checks.py` выполняются обычным `manage.py check`,
инспектируют **только конфигурацию** (никаких подключений к PostgreSQL, Redis и
SMTP), не печатают секретов и вне production молчат. Их идентификаторы —
`ecosystem.E…` и `ecosystem.W…`; точные условия и тексты живут в самом файле.

## 13. Checklist перед пилотом

```
[ ] APP_ENV=production, DEBUG=false, реальный SECRET_KEY из окружения
[ ] ALLOWED_HOSTS и CSRF_TRUSTED_ORIGINS заполнены, https:// APP_BASE_URL
[ ] PostgreSQL: пустая база и роли созданы, DB_SSLMODE=require и выше
[ ] migrate + seed_references + createsuperuser + collectstatic выполнены
[ ] check, check_fresh_bootstrap, check_production_readiness — без BLOCKING
[ ] STATIC_ROOT и MEDIA_ROOT — разные каталоги; MEDIA_ROOT не раздаётся напрямую
[ ] ENABLE_DEMO_RESET выключен; демонстрационных учётных записей нет
[ ] ASGI-сервер запущен; /health/live/ → 200 ok, /health/ready/ → 200 ready
[ ] reverse proxy: HTTPS, буферизация выключена, read timeout > heartbeat
[ ] Redis закрыт от внешней сети; check_realtime_transport проходит
[ ] SMTP проверен ручной обработкой одной тестовой доставки, планировщик включён
[ ] журнал вне репозитория, вне STATIC_ROOT и MEDIA_ROOT; check_logging проходит
[ ] выполнен и задокументирован тест восстановления из резервной копии
[ ] ручной smoke: вход, создание акта, переход workflow, вложение, живое обновление
```

Тест восстановления обязателен: `BACKUP_POLICY_ACKNOWLEDGED=true` —
административное подтверждение, а не доказательство. Процедура — в
[backup_restore.md](backup_restore.md).

Эксплуатация после запуска — в [operations.md](operations.md).
