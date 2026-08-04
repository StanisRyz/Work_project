#!/usr/bin/env python
"""Local load smoke test for the real-time SSE endpoint.

Opens N concurrent SSE connections for a given duration and reports how many
connected, how many reconnected, how many failed, and how long a diagnostic
event took to arrive. It is a smoke test, not a benchmark: it exists to see
whether the stack survives a pilot-sized number of connections and a Redis or
ASGI restart, not to produce throughput numbers.

Safety rules built in:

* it never stores a password and never reads a browser's cookie jar — the
  session cookie or the test credentials must be passed in explicitly, and
  credentials are read from the environment, never from a command line;
* it refuses any host that is not local unless `--i-know-this-is-not-local` is
  given, so it cannot be pointed at production by accident;
* it only ever performs GET requests against the streaming endpoint.

Usage:

    set REALTIME_SMOKE_PASSWORD=...        # test account only
    python scripts/realtime_load_smoke.py --base-url http://127.0.0.1:8000 \
        --username smoke_user --connections 20 --seconds 120
"""

import argparse
import http.cookiejar
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit


LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1', '0.0.0.0'}

PASSWORD_ENV = 'REALTIME_SMOKE_PASSWORD'
COOKIE_ENV = 'REALTIME_SMOKE_SESSION_COOKIE'


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = 0
        self.reconnects = 0
        self.errors = []
        self.heartbeats = 0
        self.events = 0
        self.latencies = []

    def record(self, **kwargs):
        with self.lock:
            for key, value in kwargs.items():
                current = getattr(self, key)
                if isinstance(current, list):
                    current.append(value)
                else:
                    setattr(self, key, current + value)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--username', help='Test account; password comes from the environment.')
    parser.add_argument('--connections', type=int, default=10)
    parser.add_argument('--seconds', type=float, default=60.0)
    parser.add_argument(
        '--i-know-this-is-not-local',
        action='store_true',
        help='Required to target any host other than localhost.',
    )
    return parser


def require_local(base_url, override):
    host = (urlsplit(base_url).hostname or '').lower()
    if host in LOCAL_HOSTS or override:
        return
    raise SystemExit(
        f'Отказ: {host} не является локальным адресом. Нагрузочная проверка не должна '
        'запускаться против рабочей среды. Повторите с --i-know-this-is-not-local, '
        'если это осознанное решение в подготовленной тестовой среде.'
    )


def login(base_url, username):
    """Authenticate a *test* account. The password never appears in argv."""
    password = os.environ.get(PASSWORD_ENV, '')
    if not password:
        raise SystemExit(
            f'Не задан {PASSWORD_ENV}. Пароль передаётся только через окружение и '
            'только для тестовой учётной записи.'
        )
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f'{base_url}/accounts/login/', timeout=20) as response:
        response.read()
    token = next((cookie.value for cookie in jar if cookie.name == 'csrftoken'), '')
    body = urllib.parse.urlencode(
        {'username': username, 'password': password, 'csrfmiddlewaretoken': token}
    ).encode()
    request = urllib.request.Request(f'{base_url}/accounts/login/', data=body)
    request.add_header('Referer', f'{base_url}/accounts/login/')
    with opener.open(request, timeout=20) as response:
        response.read()
    session = next((cookie.value for cookie in jar if cookie.name == 'sessionid'), '')
    if not session:
        raise SystemExit('Вход не выполнен: проверьте учётную запись и адрес.')
    # Only the session id travels on; the password is dropped here.
    return session


def stream(index, base_url, session_cookie, deadline, stats):
    """Hold one SSE connection until the deadline, counting what arrives."""
    while time.time() < deadline:
        request = urllib.request.Request(f'{base_url}/realtime/events/')
        request.add_header('Cookie', f'sessionid={session_cookie}')
        request.add_header('Accept', 'text/event-stream')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    stats.record(errors=f'HTTP {response.status}')
                    return
                stats.record(connected=1)
                buffer = b''
                while time.time() < deadline:
                    chunk = response.read(1)
                    if not chunk:
                        break
                    buffer += chunk
                    if not buffer.endswith(b'\n\n'):
                        continue
                    frame = buffer.decode('utf-8', errors='replace')
                    buffer = b''
                    if frame.startswith(': '):
                        stats.record(heartbeats=1)
                    elif frame.startswith('id: '):
                        stats.record(events=1)
                        stats.record(latencies=_latency_of(frame))
        except urllib.error.HTTPError as exc:
            stats.record(errors=f'HTTP {exc.code}')
            return
        except Exception as exc:  # noqa: BLE001 - a smoke run reports, never crashes
            stats.record(errors=type(exc).__name__)
        if time.time() < deadline:
            # The server closes a stream at its lifetime limit; a browser would
            # reconnect here, so the smoke test does the same.
            stats.record(reconnects=1)
            time.sleep(1)


def _latency_of(frame):
    """Seconds between the event's `occurred_at` and its arrival, if readable."""
    try:
        payload = json.loads(frame.split('data: ', 1)[1].strip())
        from datetime import datetime, timezone

        occurred = datetime.fromisoformat(payload['occurred_at'])
        return max(0.0, (datetime.now(timezone.utc) - occurred).total_seconds())
    except Exception:  # noqa: BLE001 - latency is best-effort
        return 0.0


def main(argv=None):
    options = build_parser().parse_args(argv)
    base_url = options.base_url.rstrip('/')
    require_local(base_url, options.i_know_this_is_not_local)

    session_cookie = os.environ.get(COOKIE_ENV, '').strip()
    if not session_cookie:
        if not options.username:
            raise SystemExit(
                f'Укажите --username (и {PASSWORD_ENV}) или готовый {COOKIE_ENV}.'
            )
        session_cookie = login(base_url, options.username)

    stats = Stats()
    deadline = time.time() + options.seconds
    print(f'Открываем {options.connections} соединений на {options.seconds:.0f} с…')

    threads = [
        threading.Thread(
            target=stream, args=(index, base_url, session_cookie, deadline, stats), daemon=True
        )
        for index in range(options.connections)
    ]
    started = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=options.seconds + 60)
    duration = time.time() - started

    print('\n--- Результат ---')
    print(f'Длительность:        {duration:.1f} с')
    print(f'Успешных соединений: {stats.connected}')
    print(f'Переподключений:     {stats.reconnects}')
    print(f'Heartbeat-кадров:    {stats.heartbeats}')
    print(f'Событий получено:    {stats.events}')
    if stats.latencies:
        print(f'Задержка, медиана:   {statistics.median(stats.latencies) * 1000:.0f} мс')
        print(f'Задержка, максимум:  {max(stats.latencies) * 1000:.0f} мс')
    if stats.errors:
        summary = {}
        for error in stats.errors:
            summary[error] = summary.get(error, 0) + 1
        print(f'Ошибки:              {summary}')
    else:
        print('Ошибки:              нет')
    return 0 if stats.connected else 1


if __name__ == '__main__':
    sys.exit(main())
