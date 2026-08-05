# Подготовка к развёртыванию

Документ описывает **конфигурацию приложения** для пилотной эксплуатации:
переменные окружения, режимы, проверки и health endpoints. Он намеренно не
устанавливает PostgreSQL и Redis, не создаёт роли базы, не настраивает reverse
proxy, HTTPS, сертификаты и службы ОС — это отдельные задачи ИТ-службы после
выбора сервера. Фактическое развёртывание этим этапом не выполняется.

Смежные документы:

- [Чистый запуск PostgreSQL](fresh_postgresql_bootstrap.md) — пошаговый первый запуск;
- [PostgreSQL в production](postgresql_production.md) — роли и права;
- [Резервное копирование и восстановление](backup_restore.md);
- [Развёртывание real-time](realtime_production.md) — ASGI, proxy, Redis.

## 1. Режим окружения: `APP_ENV`

Единственный явный переключатель между удобной локальной средой и защищённым
развёртыванием. Ничего не выводится из `DEBUG` и не угадывается по наличию
переменных.

| Значение | Назначение |
| --- | --- |
| `development` | по умолчанию; SQLite, DEBUG, локальный ключ |
| `test` | автотесты и CI |
| `production` | пилот и рабочая эксплуатация |

Неизвестное значение — `ImproperlyConfigured` при старте. В коде доступны
`IS_DEVELOPMENT`, `IS_TEST`, `IS_PRODUCTION`.

## 2. Что запрещает production

Все эти условия проверяются **при импорте настроек**, то есть процесс не
стартует вовсе, а не падает на первом запросе:

| Правило | Причина |
| --- | --- |
| `SECRET_KEY` обязателен из окружения | подпись сессий и CSRF |
| запрещён опубликованный development-ключ | он лежит в Git и известен всем |
| запрещён пустой, короткий (< 50) и очевидно тестовый ключ | предсказуемая подпись |
| `DEBUG` обязан быть false | DEBUG раскрывает трассировки, настройки и SQL |
| `DATABASE_ENGINE=postgresql` | SQLite не даёт конкурентной записи и блокировок строк |
| `ALLOWED_HOSTS` не пуст и без `*` | wildcard отключает проверку заголовка Host |
| `APP_BASE_URL` — абсолютный `https://` | из него строятся ссылки в письмах |
| `CSRF_TRUSTED_ORIGINS` — абсолютные `https://` | иначе CSRF-проверка бесполезна |

Текст ключа, пароля и строки подключения **никогда** не попадает в сообщение
об ошибке.

## 3. Переменные безопасности

Значения по умолчанию безопасны в production и настраиваются окружением.

| Переменная | По умолчанию (production) | Назначение |
| --- | --- | --- |
| `SESSION_COOKIE_SECURE` | `true` | cookie сессии только по HTTPS |
| `CSRF_COOKIE_SECURE` | `true` | CSRF-cookie только по HTTPS |
| `SESSION_COOKIE_HTTPONLY` | `true` | cookie недоступна из JavaScript |
| `SESSION_COOKIE_SAMESITE` | `Lax` | `Lax`, `Strict` или `None` |
| `CSRF_COOKIE_SAMESITE` | `Lax` | то же |
| `SECURE_SSL_REDIRECT` | `true` | HTTP → HTTPS |
| `SECURE_HSTS_SECONDS` | `0` | **включается вручную**, см. ниже |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `false` | только вместе с HSTS |
| `SECURE_HSTS_PRELOAD` | `false` | только вместе с HSTS |
| `SECURE_CONTENT_TYPE_NOSNIFF` | `true` | запрет угадывания MIME |
| `X_FRAME_OPTIONS` | `DENY` | защита от clickjacking |
| `USE_X_FORWARDED_HOST` | `false` | только за доверенным proxy |
| `TRUST_X_FORWARDED_PROTO` | `false` | см. раздел 4 |

**HSTS никогда не включается сам.** Браузер запоминает заголовок надолго, и
ошибочно выставленный большой срок нельзя быстро отменить: сайт станет
недоступен по HTTP для всех, кто уже получил заголовок. Включайте HSTS только
после того, как HTTPS точно работает для каждого обслуживаемого имени, и
начинайте с небольшого срока.

## 4. Доверие заголовкам reverse proxy

`SECURE_PROXY_SSL_HEADER` устанавливается **только** при
`TRUST_X_FORWARDED_PROTO=true`. При `false` заголовок не устанавливается вовсе.

Риск: если приложение доступно напрямую (в обход proxy), любой клиент может
прислать `X-Forwarded-Proto: https` и заставить Django считать обычный
HTTP-запрос защищённым — тогда secure-cookie уйдут по открытому каналу, а
`SECURE_SSL_REDIRECT` перестанет срабатывать. Включайте флаг, только если:

1. TLS терминируется на reverse proxy;
2. proxy **перезаписывает** `X-Forwarded-Proto`, а не пропускает клиентский;
3. приложение недоступно напрямую снаружи (слушает loopback или закрыто
   сетевыми правилами).

То же относится к `USE_X_FORWARDED_HOST`.

## 5. PostgreSQL runtime

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `DB_SSLMODE` | `prefer` | `disable`/`allow`/`prefer`/`require`/`verify-ca`/`verify-full` |
| `DB_APPLICATION_NAME` | `quality-ecosystem` | виден в `pg_stat_activity` |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | ограничение одного запроса |
| `DB_LOCK_TIMEOUT_MS` | `10000` | ожидание блокировки |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | `60000` | забытая открытая транзакция |
| `DB_CONN_MAX_AGE` | `0` | см. ниже |
| `DB_CONN_HEALTH_CHECKS` | `false` | |

Передаются через `DATABASES["default"]["OPTIONS"]`: `sslmode`,
`application_name` и строка libpq `options` с тремя таймаутами. Значение `0`
отключает соответствующий таймаут и отражается предупреждением
`ecosystem.W004`. Для SQLite эти опции **не добавляются** вовсе.

Для сетевого соединения рекомендуется `require` и выше; `disable`, `allow` и
`prefer` допускают незашифрованное соединение и дают предупреждение
`ecosystem.W003`.

**`DB_CONN_MAX_AGE=0` остаётся рекомендуемым значением под ASGI.** Длительное
SSE-соединение не должно удерживать соединение с базой на всё своё время
жизни. Persistent connections, встроенный пул и PgBouncer — решение по
результатам реального нагрузочного теста
(см. [производительность](realtime_performance.md), раздел 8), а не значение
по умолчанию.

## 6. Статика и медиа

Это **разные** вещи, и их нельзя раздавать одинаково.

| | STATIC | MEDIA |
| --- | --- | --- |
| Что | CSS, JavaScript, изображения интерфейса | вложения актов |
| Переменная | `STATIC_ROOT_PATH` (по умолчанию `BASE_DIR/staticfiles`) | `MEDIA_ROOT_PATH` (по умолчанию `BASE_DIR/media`) |
| Как раздаётся | публично, веб-сервером | **только** через `acts.views.act_download_attachment` |
| Доступ | открытый | проверка прав на каждый запрос |

`python manage.py collectstatic --noinput` собирает статику в `STATIC_ROOT`.
`STATICFILES_DIRS` и существующие ассеты не менялись.

**`MEDIA_ROOT` не должен быть доступен веб-серверу напрямую.** Вложения актов
содержат производственные данные, и их видимость определяется теми же
правилами, что и видимость самого акта. Публичная раздача `MEDIA_ROOT`
обошла бы эту проверку полностью. Права на скачивание вложений этим этапом не
менялись. Совпадение `STATIC_ROOT` и `MEDIA_ROOT` — блокирующая ошибка
`ecosystem.E012`.

## 7. Demo reset

`ENABLE_DEMO_RESET=false` по умолчанию. Флаг управляет разрушительным
действием «удалить все акты вместе с историей, комментариями и вложениями».

- при выключенном флаге URL `/acts/clear-all/` **не регистрируется** —
  прямой запрос получает обычный 404, а кнопка не выводится;
- в production значение принудительно выключается независимо от окружения, а
  сама попытка включить его — блокирующая ошибка `ecosystem.E013`;
- при включённом флаге сохраняется вся остальная защита: только роль
  администратора, только POST, CSRF обязателен, GET ничего не удаляет.

Раньше защита опиралась на имя учётной записи `admin_user`. Это было хрупко:
переименование или создание такой учётной записи где угодно снова открывало
разрушительное действие. Теперь защитой является флаг.

## 8. Системные проверки

`ecosystem/checks.py`, выполняются обычным `python manage.py check`. Они
**только читают конфигурацию**: не подключаются к PostgreSQL, Redis или SMTP,
не выводят секреты и вне production молчат.

Блокирующие: `E001` DEBUG; `E002` пустой ALLOWED_HOSTS; `E003` wildcard;
`E004` HTTP APP_BASE_URL; `E005`/`E006` CSRF origins; `E007`/`E008` secure
cookies; `E009` SSL redirect; `E010` не PostgreSQL; `E011` нет STATIC_ROOT;
`E012` STATIC_ROOT совпадает с MEDIA_ROOT; `E013` demo reset; `E014` console
email при включённых уведомлениях; `E015` TLS+SSL одновременно; `E016` пустой
отправитель; `E017` неположительные параметры очереди (включая
`EMAIL_NOTIFICATION_PROCESSING_TIMEOUT_SECONDS`); `E018` real-time с Noop
publisher; `E019` нет ASGI_APPLICATION; `E020` пустой EMAIL_HOST при
включённых уведомлениях; `E021` EMAIL_PORT вне диапазона 1-65535.
`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` ни при каких условиях не проверяются
как обязательные — корпоративный SMTP relay может работать по IP allow-list
без аутентификации.

Предупреждения: `W001` HSTS выключен; `W002` не настроены proxy-заголовки;
`W003` sslmode допускает незашифрованное соединение; `W004` отключены runtime
timeouts; `W005` `redis://` без подтверждения доверенной сети; `W006`
backup не подтверждён.

## 9. Health endpoints

| Endpoint | Что означает | Что проверяет |
| --- | --- | --- |
| `GET /health/live/` | процесс жив | **ничего** — ни базы, ни Redis, ни диска |
| `GET /health/ready/` | готов принимать трафик | `SELECT 1`, отсутствие неприменённых миграций, Redis PING (только при включённом real-time с Redis), `MEDIA_ROOT`, `STATIC_ROOT` |

Оба: без авторизации, только `GET` и `HEAD` (иначе 405), `Cache-Control:
no-store`. Liveness всегда `200 {"status": "ok"}`. Readiness — `200
{"status": "ready"}` либо `503 {"status": "unavailable"}` и **ничего больше**:
ни SQL, ни текста исключения, ни путей, хостов, имён пользователей, Redis URL
и учётных данных. Подробности пишутся в logger `deployment`, где их видит
администратор.

Liveness намеренно не зависит от базы: иначе недоступность PostgreSQL
заставила бы process manager перезапускать здоровые процессы по кругу.
Readiness ничего не изменяет.

## 10. Команды проверки

Обе только читают.

```powershell
python manage.py check_fresh_bootstrap
python manage.py check_production_readiness --json-report readiness.json
```

`check_fresh_bootstrap` — первый запуск на пустой PostgreSQL: backend,
соединение, миграции, обязательные справочники, `ActNumberSequence`,
отсутствие демонстрационных и `PERF-SYNTHETIC` данных, отсутствие
демонстрационного администратора, выключенный demo reset, `MEDIA_ROOT`,
`STATIC_ROOT`, непротиворечивость real-time и email. Наличие **рабочих**
данных ошибкой не считается — команда остаётся повторно запускаемой после
создания администратора и начала эксплуатации, и сообщает об этом
предупреждением. Имена пользователей и содержимое объектов не выводятся.

Обязательные справочники — полный набор статусов актов (`CREATED_OTK`,
`KO_REVIEW`, `TO_ANALYSIS`, `OTK_REVIEW`, `ACTIONS_ASSIGNED`, `ARCHIVED`,
`CLOSED`, `CANCELLED`, читается из `acts.models.ACT_STATUS_CODES` — той же
константы, что использует сам workflow) и статусов задач (`IN_PROGRESS`,
`COMPLETED`). Проверка `ActNumberSequence` сравнивает `last_value` с
фактически выданным максимальным номером `АОК-YYYY-NNN` за каждый год (один
запрос независимо от числа актов и лет), никогда не создаёт и не изменяет
данные счётчика, и называет в сообщении только год и агрегированные значения —
см. [Первый запуск на чистой PostgreSQL](fresh_postgresql_bootstrap.md) для
таблицы состояний и ручной процедуры исправления.

`check_production_readiness` — сводный отчёт: системные проверки Django,
backend и версия PostgreSQL, миграции, справочники, `ActNumberSequence`, Redis
(с настоящим PING, если real-time включён), static/media, demo reset, email и
подтверждение резервного копирования. Backend/соединение/миграции/справочники/
`ActNumberSequence` переиспользуются из `check_fresh_bootstrap`; real-time,
static/media, demo reset и email эта команда проверяет сама, подробнее —
поэтому каждый логический раздел появляется в отчёте ровно один раз. Формат:
`PASS` / `WARNING` / `BLOCKING`; при наличии `BLOCKING` код возврата
ненулевой. JSON-отчёт не содержит секретов, полных путей, имён пользователей и
бизнес-данных.

## 11. Резервное копирование

`BACKUP_POLICY_ACKNOWLEDGED=false` по умолчанию. Это **административное
подтверждение, а не доказательство**: флаг говорит лишь о том, что кто-то взял
ответственность за резервное копирование. Он ничего не сообщает о том,
выполняется ли копирование и восстанавливались ли данные хоть раз.

Флаг не заменяет реальную проверку восстановления. Процедура и чек-лист — в
[Резервное копирование и восстановление](backup_restore.md); минимум один
успешный тест восстановления обязателен до начала пилота.

## 12. Логирование

**Основной носитель — ротируемый текстовый файл UTF-8** (`LOG_TO_FILE`, по
умолчанию `true` при `APP_ENV=production`). Консоль остаётся включённой рядом
(`LOG_TO_CONSOLE`, по умолчанию `true`): её собирает process manager, и через
неё позже подключается централизованный сбор без изменения бизнес-кода. Хотя бы
один обработчик обязан быть включён — иначе процесс не стартует. Сторонние
logging-фреймворки и внешние платформы (ELK, Loki, Sentry, Graylog) не
добавляются.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | общий уровень |
| `REALTIME_LOG_LEVEL` | как `LOG_LEVEL` | уровень logger'а `realtime` |
| `LOG_TO_FILE` | `true` в production | файловый журнал |
| `LOG_TO_CONSOLE` | `true` | stdout |
| `LOG_FILE_PATH` | `BASE_DIR/logs/application.log` | в production абсолютный, вне репозитория |
| `LOG_FILE_MAX_BYTES` | `20971520` | размер одного файла |
| `LOG_FILE_BACKUP_COUNT` | `10` | архивных копий |
| `LOG_SLOW_REQUEST_MS` | `2000` | порог медленного запроса |
| `LOG_MUTATING_REQUESTS` | `true` | писать POST/PUT/PATCH/DELETE |
| `LOG_HEALTH_REQUESTS` | `false` | писать health-пробы |
| `APP_RELEASE` | пусто | безопасная метка версии |

Формат строки:

```
[timestamp] LEVEL logger request=<request_id> user=<user_id>: event key=value ...
```

Настроенные loggers: `django.request`, `django.security`, `ecosystem.request`,
`ecosystem.startup`, `ecosystem.workflow`, `ecosystem.attachments`,
`notifications.email`, `maintenance`, `deployment`, `realtime`.

Каждый обычный запрос получает `request_id`, который возвращается в заголовке
`X-Request-ID` и проставляется во все строки, записанные во время этого
запроса. Входящий `X-Request-ID` не принимается. На уровне INFO пишутся только
изменяющие запросы; `GET` — лишь при превышении `LOG_SLOW_REQUEST_MS`, при
4xx/5xx или при исключении. Health-пробы, static, media и favicon исключены;
SSE `/realtime/events/` не подпадает под правило медленного запроса.

Проверки конфигурации: `ecosystem.E022` (нет обработчиков), `E023`/`E024`
(некорректная ротация), `E025` (журнал внутри `STATIC_ROOT`/`MEDIA_ROOT`),
`E026` (относительный путь в production), `E027`/`W007` (недоступен для
записи). Отдельно:

```powershell
python manage.py check_logging
python manage.py check_logging --write-probe
```

**Никогда не логируются**: `SECRET_KEY`, `DB_PASSWORD`, Redis URL с учётными
данными, `EMAIL_HOST_PASSWORD`, cookie сессии, CSRF-токены, заголовки
`Authorization`, query string и тело запроса, тексты комментариев, описания
дефектов, корневые причины, тексты задач и результатов, данные заказчика и
партии, имена пользователей, адреса получателей, содержимое и имена вложений.
Фильтр редактирования навешен на все обработчики.

`ActHistory` остаётся бизнес-аудитом; журнал — диагностика и может быть
ротирован. `RotatingFileHandler` корректен только в однопроцессном пилоте — при
нескольких Uvicorn workers ротацию должен выполнять внешний системный механизм.
Подробно: [Операционное логирование](operational_logging.md).

## 13. Email

Очередь и команда не менялись:

```powershell
python manage.py process_notification_deliveries --batch-size 100
```

Celery не используется. Конфигурация планировщика (systemd timer, cron,
Windows Task Scheduler) этим этапом **не создаётся**: её настраивает ИТ-служба
после выбора операционной системы сервера — см.
[Автоматическая обработка очереди](email_queue_automation.md).

Проверки конфигурации email не подключаются к SMTP: недоступный почтовый
сервер не должен блокировать развёртывание.

## 14. Чего этот этап не делает

Не устанавливает PostgreSQL и Redis; не создаёт роли базы данных; не
настраивает reverse proxy (Nginx/IIS/Apache), HTTPS и сертификаты; не создаёт
systemd units и службы Windows; не настраивает планировщик очереди писем; не
настраивает реальное резервное копирование; не использует Docker, Kubernetes и
PgBouncer; не добавляет WebSocket, Django Channels, React, npm и Celery; не
переносит существующую SQLite; не выполняет production-развёртывание.
