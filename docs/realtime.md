# Real-time события (RT-1 и RT-2)

**RT-1** добавил транспортно-независимый фундамент: бизнес-сервисы формируют
единообразные события после успешной фиксации транзакции и ничего не знают о
Redis, SSE или WebSocket.

**RT-2** добавил сам транспорт: публикацию событий в Redis Pub/Sub и
защищённый персональный SSE endpoint под ASGI. Пользовательский интерфейс на
этом этапе не менялся — реализована и проверена только цепочка
**Publisher → Redis → SSE stream**.

По умолчанию всё выключено: `REALTIME_ENABLED=false`, backend — `Noop`.
Проект работает ровно так же, как до появления приложения `realtime`, и
**не требует Redis**.

## 1. Архитектурные принципы

1. **Событие — это факт, а не содержимое.** Оно говорит «уведомление
   создано», а не «вот его текст». Актуальные данные клиент забирает обычными
   Django-endpoint'ами, где действуют штатные проверки прав.
2. **PostgreSQL остаётся источником истины.** События не хранятся, не
   переигрываются и ничего не подтверждают. Потерянное событие — это
   пропущенное обновление UI, а не потерянные данные.
3. **Публикация явная.** События публикуются из бизнес-сервисов, а не из общих
   `post_save`-сигналов.
4. **Публикация после commit.** Откат транзакции не должен порождать событие.
5. **Targets формирует сервер.** Клиентские параметры никогда не участвуют в
   выборе получателей, и targets не входят в публичный payload.
6. **Отказ транспорта не ломает бизнес-операцию.** К моменту публикации
   транзакция уже зафиксирована.

### Почему не post_save signals

Общий `post_save` выглядит удобнее, но именно поэтому он и не подходит:

- он срабатывает при загрузке fixture и импорте миграционного пакета;
- он реагирует на технические `save()` (пересчёт полей, служебные апдейты);
- он публикует до того, как готовы зависимые объекты (задача без исполнителей);
- он создаёт скрытые дубли, когда одну запись сохраняют несколько раз за
  операцию.

Явный вызов в сервисе решает все четыре проблемы: событие возникает ровно там,
где бизнес-факт действительно случился.

## 2. Контракт RealtimeEvent

`realtime/events.py`. Неизменяемый frozen dataclass.

| Поле | Тип | Описание |
| --- | --- | --- |
| `schema_version` | int | Версия контракта, начинается с 1 |
| `event_id` | UUID | Уникален для каждого фактического события |
| `event_type` | `RealtimeEventType` | Стабильное строковое значение |
| `occurred_at` | datetime | Обязательно timezone-aware |
| `resource_type` | str | `act`, `comment`, `notification`, `task`, `user` |
| `resource_id` | int | Положительное целое |
| `data` | dict | Минимальные JSON-safe метаданные |

Сериализация: `as_dict()` и `as_json()` (детерминированная, `sort_keys=True`).

```json
{
  "schema_version": 1,
  "event_id": "803be2b1-0570-400e-a175-59ac0407007a",
  "event_type": "comment.created",
  "occurred_at": "2026-08-04T09:18:54.380277+00:00",
  "resource_type": "comment",
  "resource_id": 7,
  "data": {"act_id": 23, "author_id": 5}
}
```

### Валидация

Событие невозможно создать некорректным:

- `schema_version` — целое, не меньше 1;
- `event_id` — настоящий `UUID`;
- `occurred_at` — datetime с timezone;
- `resource_type` — непустой и из известного списка;
- `resource_id` — положительное целое (`0`, отрицательное, строка, `float` и
  `bool` отклоняются);
- `data` — словарь со строковыми ключами, значения только `str`/`int`/`float`/
  `bool`/`None`/`list`/`dict`; `datetime`, `set`, `bytes`, модели, `NaN` и
  `Infinity` отклоняются; ограничены глубина (3) и число ключей (20);
- `targets` в контракте отсутствует как поле и не появляется в payload.

### Что запрещено класть в data

Текст комментариев, описания дефектов, email-адреса, имена файлов, данные
авторизации, password hashes, permissions, полные модели. Только
идентификаторы и безопасные технические метаданные — коды статусов, счётчики,
списки id.

## 3. Event types

Централизованный enum `RealtimeEventType`; строковые значения — часть
контракта. Литералы в бизнес-коде запрещены.

| Значение | Resource | Когда публикуется |
| --- | --- | --- |
| `notification.created` | `notification` | Создана запись `Notification` |
| `notification.read` | `user` | Уведомления пользователя отмечены прочитанными |
| `task.created` | `task` | Задача и все её исполнители сохранены |
| `task.updated` | `task` | Значимое изменение сохранённой задачи |
| `task.completed` | `task` | Задача успешно завершена |
| `act.updated` | `act` | Акт успешно отредактирован |
| `act.status_changed` | `act` | Любой успешный workflow-переход |
| `comment.created` | `comment` | Создан комментарий к акту |

Отдельного типа на каждый статус нет и не будет: переход описывается
`act.status_changed` с `from_status_code` и `to_status_code`.

`task.updated` реализован и покрыт тестами, но **пока не имеет call site**: ни
одна существующая бизнес-операция не изменяет сохранённую задачу иначе, чем
завершением. Будущий сервис редактирования задач обязан вызвать
`emit_task_updated`, а не изобретать своё событие.

## 4. Targets

`realtime/targets.py`. `RealtimeTarget(kind, identifier)`, ключ вида
`<kind>:<id>`.

- `user:<id>` — авторитетный получатель;
- `act:<id>` — дополнительная маршрутная подсказка для событий по акту.

Правила: пустые, нулевые и отрицательные ID запрещены; `None` игнорируется;
дубли удаляются; порядок детерминированный (`(kind, identifier)`).

Функции получателей (`realtime/recipients.py`) **переиспользуют существующую
маршрутизацию внутренних уведомлений** и не добавляют новых бизнес-правил:

| Событие | Получатели |
| --- | --- |
| `notification.*` | только сам recipient (уведомление приватно) |
| `task.*` | текущие активные исполнители задачи + `act:<id>` |
| `act.updated` | участники акта (`get_act_participants`) + `act:<id>` |
| `act.status_changed` | адресаты уведомления этого перехода (`get_recipients_for_history_event`) + участники акта + `act:<id>` |
| `comment.created` | участники по правилам комментариев (`get_comment_participants`) + `act:<id>` |

Неактивные пользователи и неактивные профили отбрасываются — так же, как в
`create_notifications`. Посторонний пользователь получателем не становится.

> **Важно для RT-2.** `act:<id>` — только подсказка маршрутизации. Транспорт,
> который откроет подписку на «комнату» акта, обязан авторизовать её через
> `acts.permissions.can_view_act`; сам по себе target прав не даёт.

## 5. Publisher abstraction

`realtime/backends.py` — единый интерфейс `publish(event, targets)`:

- `NoopRealtimePublisher` — по умолчанию, ничего не отправляет;
- `CaptureRealtimePublisher` — сохраняет события и targets для тестов;
- `FailingRealtimePublisher` — намеренно бросает исключение.

Backend выбирается настройкой и загружается по dotted path
(`django.utils.module_loading.import_string`), экземпляр кэшируется. В тестах
его можно подменить `set_publisher()` / `reset_publisher()` или контекстными
менеджерами из `realtime/testing.py`. `override_settings` на
`REALTIME_PUBLISHER_BACKEND` сбрасывает кэш автоматически.

Бизнес-код не импортирует Redis-клиенты и ничего не знает о будущем SSE.

## 6. Настройки

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `REALTIME_ENABLED` | `false` | Полный выключатель |
| `REALTIME_PUBLISHER_BACKEND` | `realtime.backends.NoopRealtimePublisher` | Dotted path backend'а |
| `REALTIME_FAIL_SILENTLY` | `true` | Логировать ошибку публикации вместо исключения |
| `REALTIME_LOG_LEVEL` | `INFO` | Уровень логгера `realtime` |

Redis URL, heartbeat и SSE-настройки сюда **не** добавляются — это RT-2.

При `REALTIME_ENABLED=false` эмиттеры выходят до вычисления получателей,
поэтому лишних запросов к базе нет, `on_commit`-callback не регистрируется и
внешняя инфраструктура не требуется.

## 7. Правила after-commit

```python
from realtime.emitters import emit_comment_created

with transaction.atomic():
    comment = ActComment.objects.create(...)
    emit_comment_created(comment)   # регистрирует публикацию через on_commit
# событие уходит здесь, после успешного commit
```

- `publish_after_commit(event, targets)` нормализует targets **внутри**
  транзакции (некорректный target падает в месте вызова, а не в callback'е) и
  регистрирует `transaction.on_commit()`;
- `dispatch_event(event, targets)` публикует немедленно — для транспорта и
  диагностики;
- откат транзакции означает, что callback не выполнится и событие не уйдёт;
- порядок нескольких `on_commit` сохраняется.

В тестах используйте `TestCase.captureOnCommitCallbacks(execute=True)` — без
него callbacks не выполняются, потому что тестовая транзакция не коммитится.

## 8. Обработка ошибок

Публикация происходит **после** commit, поэтому откатывать уже нечего.

- `REALTIME_FAIL_SILENTLY=true` (по умолчанию): ошибка пишется в logger
  `realtime` уровнем `ERROR`, сохранённая операция не страдает;
- `REALTIME_FAIL_SILENTLY=false`: исключение поднимается — для тестов и
  диагностики.

Лог содержит `event_id`, `event_type`, `resource_type:resource_id`, количество
targets, backend и тип исключения. Payload и персональные данные в лог не
попадают.

## 9. Границы RT-1

Приложение `realtime` не имеет моделей и миграций. В RT-1 у него не было и URL,
views и шаблонов; URL и async view появились в RT-2 (моделей и миграций
по-прежнему нет).

---

# RT-2: транспорт

## 10. Архитектура RT-2

```
бизнес-сервис
  └─ emit_*()                     realtime/emitters.py
      └─ publish_after_commit()   realtime/publisher.py
          └─ on_commit ──────────► dispatch_event()
                                     └─ RedisRealtimePublisher   realtime/backends.py
                                         └─ PUBLISH <prefix>:<target.key>
                                              │
                                            Redis Pub/Sub
                                              │
                                         SUBSCRIBE <prefix>:user:<id>
                                     ┌───────┴────────┐
                                     │ event_stream() │            realtime/sse.py
                                     └───────┬────────┘
                                     GET /realtime/events/         realtime/views.py
                                              │
                                        браузер (RT-3)
```

Ключевые принципы RT-1 не изменились:

- бизнес-сервисы **не импортируют Redis**; клиент создают только
  `realtime/backends.py`, `realtime/transport.py`, `realtime/sse.py` и
  диагностическая команда, причём импорт `redis` ленивый;
- публикация по-прежнему идёт через `publish_after_commit()`;
- **PostgreSQL остаётся источником истины.** Redis — краткоживущий транспорт:
  события не хранятся, не переигрываются и не подтверждаются. Потерянное
  событие означает пропущенное обновление UI, а не потерянные данные;
- отказ Redis не откатывает и не ломает уже сохранённую операцию;
- `Noop`, `Capture` и `Failing` publishers продолжают работать.

## 11. Redis channel namespace

Имя канала строится **только** на сервере и только из валидного
`RealtimeTarget`:

```
<REALTIME_CHANNEL_PREFIX>:<target.kind>:<target.identifier>

quality-ecosystem:realtime:user:7
quality-ecosystem:realtime:act:23
quality-ecosystem:realtime:diagnostic:<uuid4-hex>
```

`realtime/channels.py` нормализует префикс (только латиница, цифры и `. _ : -`,
до 64 символов), запрещает пустой префикс, пробелы и управляющие символы и
отклоняет любое значение, не являющееся `RealtimeTarget`. Функции, принимающей
произвольную строку в качестве имени канала, не существует — клиент не может
назвать канал.

`act:<id>` публикуется как маршрутная подсказка, но **подписаться на него в
RT-2 нельзя**: комнате акта нужна авторизация через `can_view_act`, это RT-3.

## 12. RedisRealtimePublisher

```
REALTIME_PUBLISHER_BACKEND=realtime.backends.RedisRealtimePublisher
```

- синхронный клиент из общего `ConnectionPool` (`realtime/transport.py`) —
  вызывается из `on_commit`, то есть внутри пользовательского запроса;
- событие сериализуется **один раз** (`as_compact_json()`), одинаковый payload
  уходит во все каналы нормализованных targets;
- дубли targets не приводят к повторной публикации;
- ноль подписчиков — нормальное состояние, не ошибка;
- длительных retry внутри запроса нет: работают короткие socket-таймауты;
- ошибка оборачивается в `RealtimePublisherError` и попадает в существующий
  механизм `REALTIME_FAIL_SILENTLY`;
- oversized событие не публикуется вовсе — пишется безопасное предупреждение.

## 13. Десериализация и лимиты размера

`RealtimeEvent.from_dict()` и `from_json()` заново валидируют полученное
сообщение как недоверенный ввод: `schema_version`, `event_id` (UUID),
`event_type`, `occurred_at` (timezone-aware), `resource_type`, `resource_id`,
JSON-safe `data` и максимальный размер. Набор полей на проводе фиксирован —
лишние поля, включая подсунутые `targets` или имя канала, **отклоняются**, а не
игнорируются.

`REALTIME_MAX_EVENT_BYTES` (по умолчанию 16384) проверяется дважды: перед
публикацией и перед записью в SSE stream. Слишком большое сообщение не
передаётся, пишется безопасное предупреждение, и stream из-за него не рвётся.

Payload `notification.read` ограничен: всегда присутствуют `changed_count`,
`unread_count` и `scope`, а явный список `notification_ids` добавляется только
для небольших операций (не `scope=all` и не более 20 записей).

## 14. Формат SSE frames

Начальный frame — период переподключения:

```
retry: 3000

```

Бизнес-событие:

```
id: 803be2b1-0570-400e-a175-59ac0407007a
event: notification.created
data: {"data":{"act_id":3,"actor_id":null,"recipient_id":5},"event_id":"803be2b1-...","event_type":"notification.created","occurred_at":"2026-08-04T09:18:54.380277+00:00","resource_id":11,"resource_type":"notification","schema_version":1}

```

- `id` — `event_id`;
- `event` — значение `RealtimeEventType`;
- `data` — компактный JSON в одну строку, UTF-8;
- каждый frame завершается **двумя** переводами строки;
- targets и имя Redis-канала в payload отсутствуют.

Heartbeat — SSE-комментарий:

```
: heartbeat

```

## 15. Endpoint и аутентификация

```
GET /realtime/events/
```

| Условие | Ответ |
| --- | --- |
| анонимный запрос | `401` |
| `REALTIME_ENABLED=false` | `204`, к Redis не обращаемся |
| Redis недоступен (короткий preflight PING) | `503` |
| метод не GET | `405` |
| всё в порядке | `200`, `StreamingHttpResponse` с async iterator |

Пользователь определяется **только** через Django session (`request.auser()`).
Подписка — ровно один канал `user:<request.user.pk>`. Query string, path и тело
запроса на выбор канала не влияют; act-каналы не подписываются.

Заголовки успешного ответа:

```
Content-Type: text/event-stream
Cache-Control: no-cache, no-store, no-transform
X-Accel-Buffering: no
Vary: Cookie
```

Hop-by-hop заголовки (`Connection`, `Upgrade`) вручную не выставляются — ими
управляет ASGI-сервер.

## 16. Heartbeat и очистка ресурсов

Подписчик ждёт сообщение с таймаутом `REALTIME_HEARTBEAT_SECONDS`. При таймауте
выдаётся heartbeat-комментарий, поэтому прокси и клиент отличают живое
соединение от мёртвого. Служебные `subscribe`/`unsubscribe`-сообщения
игнорируются.

При отключении клиента (закрытая вкладка) обрабатывается
`asyncio.CancelledError`, выполняется `unsubscribe`, закрывается `PubSub` и
освобождается клиент. Обычное закрытие вкладки пишется уровнем `DEBUG`, а не
как инцидент. Неожиданный disconnect Redis завершает stream контролируемо:
предупреждение в лог, ресурсы освобождены, клиент переподключится через
объявленный `retry`. Каждый шаг очистки защищён отдельно, поэтому сбой в
`unsubscribe` не мешает закрыть остальное.

## 17. Диагностическая команда

```powershell
python manage.py check_realtime_transport [--timeout 5]
```

Команда печатает настройки (адрес Redis — **без учётных данных**), выполняет
`PING`, создаёт уникальный одноразовый канал
`<prefix>:diagnostic:<uuid4-hex>`, подписывается, публикует случайный token,
получает его обратно, сравнивает, измеряет round trip и корректно закрывает
ресурсы. Любой сбой — `CommandError`. Бизнес-объекты не создаются,
пользовательские каналы не используются, credentials не выводятся.

## 18. Безопасность учётных данных

`realtime/transport.py` — единственное место, где известен полный
`REALTIME_REDIS_URL`. Наружу отдаются только:

- `safe_redis_location()` → `redis://host:port/db` без имени пользователя и
  пароля;
- `sanitize()` → текст с вырезанными URL, username и password;
- `describe_failure()` → `<ТипИсключения>: <очищенный текст> (адрес: <safe>)`.

Хост остаётся виден для диагностики; пароль — нет.

## 19. Локальная проверка через ASGI

```powershell
docker run --rm -p 6379:6379 redis:7

$env:REALTIME_ENABLED = "true"
$env:REALTIME_PUBLISHER_BACKEND = "realtime.backends.RedisRealtimePublisher"
$env:REALTIME_REDIS_URL = "redis://127.0.0.1:6379/0"

python manage.py check_realtime_transport
python -m uvicorn ecosystem.asgi:application --host 127.0.0.1 --port 8000
```

Затем в авторизованной сессии открыть `GET /realtime/events/` и опубликовать
событие в персональный канал пользователя — например, выполнив обычное
бизнес-действие или отправив тестовое событие в
`<prefix>:user:<id>` из `manage.py shell`.

## 20. Границы RT-2

Не входит в этот этап:

- `EventSource` и любой frontend JavaScript;
- toast-уведомления и обновление колокольчика;
- partial endpoints;
- автоматическое обновление задач и актов;
- подписки на act targets;
- sync endpoint и fallback polling;
- `BroadcastChannel`;
- WebSocket и Django Channels;
- production reverse proxy и HTTPS;
- хранение real-time событий в PostgreSQL;
- transactional outbox.

## 21. План RT-3

1. Подключить `EventSource` на фронтенде: по событию перезапрашивать
   соответствующий partial обычным HTTP-запросом, а не доверять payload.
2. Обновлять счётчик колокольчика и реестры точечно, без полной перезагрузки.
3. Добавить авторизуемые подписки на `act:<id>` через `can_view_act`.
4. Развернуть production ASGI за reverse proxy с HTTPS и длительными
   соединениями, отключив буферизацию.
5. При необходимости гарантированной доставки — рассмотреть transactional
   outbox; сейчас потеря события намеренно допустима.

Новые типы событий по-прежнему добавляются только в централизованный enum и
обязательно покрываются тестами контракта, targets и интеграции.
