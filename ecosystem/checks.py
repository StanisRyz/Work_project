"""Deployment system checks.

These answer one question: *is this configuration safe to serve real users
with?* They run inside the ordinary `python manage.py check`, so a dangerous
production configuration stops a deployment before the process ever binds a
port.

Three rules shape everything here:

* **configuration only** — nothing connects to PostgreSQL, Redis or SMTP. A
  check must be fast, offline and deterministic, so it can run in a build step
  or on a machine that cannot reach the services yet. Live connectivity is
  what `check_production_readiness` and the readiness endpoint are for.
* **no secrets** — a message names the *setting* and the *problem*, never the
  value. No key, password, URL with credentials or host list is ever printed.
* **quiet outside production** — a half-finished development setup is normal.
  Only `APP_ENV=production` turns these into errors.
"""

from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Warning, register


NOOP_PUBLISHER = 'realtime.backends.NoopRealtimePublisher'
REDIS_PUBLISHER = 'realtime.backends.RedisRealtimePublisher'
CONSOLE_EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


def _is_production():
    return bool(getattr(settings, 'IS_PRODUCTION', False))


@register()
def check_deployment_configuration(app_configs, **kwargs):
    """Blocking errors and warnings for a production deployment.

    Registered without `deploy=True` on purpose: an unsafe production
    configuration must be caught by the ordinary `manage.py check` that every
    deployment already runs, not only by the `--deploy` variant somebody has
    to remember to add.
    """
    if not _is_production():
        # Development and test are allowed to be convenient.
        return []

    problems = []
    problems.extend(_check_core())
    problems.extend(_check_security())
    problems.extend(_check_database())
    problems.extend(_check_static_and_media())
    problems.extend(_check_demo_reset())
    problems.extend(_check_email())
    problems.extend(_check_realtime())
    problems.extend(_check_backup())
    return problems


# -- blocking ---------------------------------------------------------------

def _check_core():
    problems = []
    if getattr(settings, 'DEBUG', False):
        problems.append(
            Error(
                'DEBUG=True при APP_ENV=production.',
                hint='DEBUG раскрывает трассировки, настройки и SQL. Выключите DEBUG.',
                id='ecosystem.E001',
            )
        )

    hosts = list(getattr(settings, 'ALLOWED_HOSTS', []) or [])
    if not hosts:
        problems.append(
            Error(
                'ALLOWED_HOSTS пуст при APP_ENV=production.',
                hint='Перечислите реальные имена хостов через ALLOWED_HOSTS.',
                id='ecosystem.E002',
            )
        )
    elif '*' in hosts:
        problems.append(
            Error(
                'ALLOWED_HOSTS содержит wildcard "*".',
                hint='Wildcard полностью отключает проверку заголовка Host.',
                id='ecosystem.E003',
            )
        )

    base_url = str(getattr(settings, 'APP_BASE_URL', '') or '')
    if urlsplit(base_url).scheme != 'https':
        problems.append(
            Error(
                'APP_BASE_URL не является https:// адресом.',
                hint='Ссылки в письмах уведомлений строятся из APP_BASE_URL.',
                id='ecosystem.E004',
            )
        )

    origins = list(getattr(settings, 'CSRF_TRUSTED_ORIGINS', []) or [])
    if not origins:
        problems.append(
            Error(
                'CSRF_TRUSTED_ORIGINS не задан при APP_ENV=production.',
                hint='Укажите https:// origin, с которого обслуживается приложение.',
                id='ecosystem.E005',
            )
        )
    elif any(urlsplit(origin).scheme != 'https' for origin in origins):
        problems.append(
            Error(
                'CSRF_TRUSTED_ORIGINS содержит не-https origin.',
                hint='В production доверенные origin должны быть только https://.',
                id='ecosystem.E006',
            )
        )
    return problems


def _check_security():
    problems = []
    if not getattr(settings, 'SESSION_COOKIE_SECURE', False):
        problems.append(
            Error(
                'SESSION_COOKIE_SECURE выключен.',
                hint='Cookie сессии уйдёт по незашифрованному соединению.',
                id='ecosystem.E007',
            )
        )
    if not getattr(settings, 'CSRF_COOKIE_SECURE', False):
        problems.append(
            Error(
                'CSRF_COOKIE_SECURE выключен.',
                hint='CSRF-cookie уйдёт по незашифрованному соединению.',
                id='ecosystem.E008',
            )
        )
    if not getattr(settings, 'SECURE_SSL_REDIRECT', False):
        problems.append(
            Error(
                'SECURE_SSL_REDIRECT выключен.',
                hint=(
                    'HTTP-запросы не будут перенаправлены на HTTPS. Если этим '
                    'занимается reverse proxy, включите SECURE_SSL_REDIRECT=false '
                    'осознанно и задокументируйте решение.'
                ),
                id='ecosystem.E009',
            )
        )

    # -- warnings
    if not int(getattr(settings, 'SECURE_HSTS_SECONDS', 0) or 0):
        problems.append(
            Warning(
                'SECURE_HSTS_SECONDS = 0: HSTS не включён.',
                hint=(
                    'Включайте HSTS только когда HTTPS точно работает для всех '
                    'имён: браузеры запоминают заголовок надолго.'
                ),
                id='ecosystem.W001',
            )
        )
    if not getattr(settings, 'SECURE_PROXY_SSL_HEADER', None) and not getattr(
        settings, 'SECURE_SSL_REDIRECT', False
    ):
        problems.append(
            Warning(
                'Ни SECURE_PROXY_SSL_HEADER, ни SECURE_SSL_REDIRECT не настроены.',
                hint=(
                    'За TLS-терминирующим proxy установите TRUST_X_FORWARDED_PROTO=true, '
                    'но только если proxy сам перезаписывает X-Forwarded-Proto.'
                ),
                id='ecosystem.W002',
            )
        )
    return problems


def _check_database():
    problems = []
    default = settings.DATABASES.get('default', {})
    if 'postgresql' not in str(default.get('ENGINE', '')):
        problems.append(
            Error(
                'В production выбран backend базы данных, отличный от PostgreSQL.',
                hint='SQLite не обеспечивает конкурентную запись и блокировки строк.',
                id='ecosystem.E010',
            )
        )
        return problems

    if getattr(settings, 'DB_SSLMODE', '') in getattr(settings, 'UNENCRYPTED_SSLMODES', ()):
        problems.append(
            Warning(
                'DB_SSLMODE допускает незашифрованное соединение с PostgreSQL.',
                hint='Для соединения по сети используйте require, verify-ca или verify-full.',
                id='ecosystem.W003',
            )
        )

    disabled = [
        name
        for name in (
            'DB_STATEMENT_TIMEOUT_MS',
            'DB_LOCK_TIMEOUT_MS',
            'DB_IDLE_IN_TRANSACTION_TIMEOUT_MS',
        )
        if not int(getattr(settings, name, 0) or 0)
    ]
    if disabled:
        problems.append(
            Warning(
                f'Отключены runtime-таймауты PostgreSQL: {", ".join(disabled)}.',
                hint=(
                    'Без них один зависший запрос или забытая открытая транзакция '
                    'удерживают блокировки неограниченно долго.'
                ),
                id='ecosystem.W004',
            )
        )
    return problems


def _check_static_and_media():
    problems = []
    if not getattr(settings, 'STATIC_ROOT', None):
        problems.append(
            Error(
                'STATIC_ROOT не задан.',
                hint='collectstatic не сможет собрать статику для веб-сервера.',
                id='ecosystem.E011',
            )
        )
    static_root = getattr(settings, 'STATIC_ROOT', None)
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if static_root and media_root and str(static_root) == str(media_root):
        problems.append(
            Error(
                'STATIC_ROOT и MEDIA_ROOT указывают на один каталог.',
                hint=(
                    'Статика раздаётся публично, а вложения актов — только через '
                    'endpoint с проверкой прав. Их нельзя смешивать.'
                ),
                id='ecosystem.E012',
            )
        )
    return problems


def _check_demo_reset():
    if getattr(settings, 'DEMO_RESET_REQUESTED', False):
        return [
            Error(
                'ENABLE_DEMO_RESET=true при APP_ENV=production.',
                hint=(
                    'Это действие безвозвратно удаляет все акты, их историю, '
                    'комментарии и вложения. В production флаг принудительно '
                    'выключен, но сам факт запроса означает ошибку конфигурации.'
                ),
                id='ecosystem.E013',
            )
        ]
    return []


def _check_email():
    problems = []
    if not getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
        # Disabled email needs no SMTP configuration at all.
        return problems

    if getattr(settings, 'EMAIL_BACKEND', '') == CONSOLE_EMAIL_BACKEND:
        problems.append(
            Error(
                'EMAIL_NOTIFICATIONS_ENABLED=true с console email backend.',
                hint='Письма будут печататься в лог вместо отправки получателям.',
                id='ecosystem.E014',
            )
        )
    if getattr(settings, 'EMAIL_USE_TLS', False) and getattr(settings, 'EMAIL_USE_SSL', False):
        problems.append(
            Error(
                'EMAIL_USE_TLS и EMAIL_USE_SSL включены одновременно.',
                hint='Это взаимоисключающие режимы; выберите один.',
                id='ecosystem.E015',
            )
        )
    if not str(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip():
        problems.append(
            Error(
                'DEFAULT_FROM_EMAIL пуст при включённых уведомлениях.',
                hint='Укажите согласованный адрес отправителя.',
                id='ecosystem.E016',
            )
        )
    for name in (
        'EMAIL_TIMEOUT',
        'EMAIL_NOTIFICATION_MAX_ATTEMPTS',
        'EMAIL_NOTIFICATION_RETRY_DELAY_SECONDS',
        'EMAIL_NOTIFICATION_BATCH_SIZE',
        'EMAIL_NOTIFICATION_PROCESSING_TIMEOUT_SECONDS',
    ):
        if int(getattr(settings, name, 0) or 0) <= 0:
            problems.append(
                Error(
                    f'{name} должен быть положительным.',
                    hint='Ноль или отрицательное значение останавливает очередь писем.',
                    id='ecosystem.E017',
                )
            )
    if not str(getattr(settings, 'EMAIL_HOST', '') or '').strip():
        problems.append(
            Error(
                'EMAIL_HOST пуст при включённых уведомлениях.',
                hint=(
                    'Укажите адрес SMTP/Exchange relay. EMAIL_HOST_USER и '
                    'EMAIL_HOST_PASSWORD остаются необязательными — relay может '
                    'работать по IP allow-list без аутентификации.'
                ),
                id='ecosystem.E020',
            )
        )
    port = getattr(settings, 'EMAIL_PORT', None)
    try:
        port_is_valid = 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        port_is_valid = False
    if not port_is_valid:
        problems.append(
            Error(
                'EMAIL_PORT вне допустимого диапазона 1-65535.',
                hint='Укажите порт SMTP/Exchange relay, обычно 25, 465 или 587.',
                id='ecosystem.E021',
            )
        )
    return problems


def _check_realtime():
    problems = []
    if not getattr(settings, 'REALTIME_ENABLED', False):
        # Real-time off is a valid production configuration.
        return problems

    if getattr(settings, 'REALTIME_PUBLISHER_BACKEND', NOOP_PUBLISHER) == NOOP_PUBLISHER:
        problems.append(
            Error(
                'REALTIME_ENABLED=true с publisher без транспорта.',
                hint=f'Укажите REALTIME_PUBLISHER_BACKEND={REDIS_PUBLISHER}.',
                id='ecosystem.E018',
            )
        )
    if not str(getattr(settings, 'ASGI_APPLICATION', '') or '').strip():
        problems.append(
            Error(
                'ASGI_APPLICATION не задан, а SSE требует ASGI.',
                hint='WSGI не умеет держать длительный поток.',
                id='ecosystem.E019',
            )
        )
    scheme = urlsplit(str(getattr(settings, 'REALTIME_REDIS_URL', '') or '')).scheme
    if scheme == 'redis' and not getattr(settings, 'REDIS_NETWORK_IS_TRUSTED', False):
        problems.append(
            Warning(
                'Redis используется по незашифрованному redis://.',
                hint=(
                    'Используйте rediss:// либо подтвердите закрытую внутреннюю '
                    'сеть через REDIS_NETWORK_IS_TRUSTED=true.'
                ),
                id='ecosystem.W005',
            )
        )
    return problems


def _check_backup():
    if not getattr(settings, 'BACKUP_POLICY_ACKNOWLEDGED', False):
        return [
            Warning(
                'BACKUP_POLICY_ACKNOWLEDGED=false.',
                hint=(
                    'Это административное подтверждение, а не доказательство '
                    'существования резервных копий. Перед пилотом должен быть '
                    'выполнен реальный тест восстановления — см. docs/backup_restore.md.'
                ),
                id='ecosystem.W006',
            )
        ]
    return []
