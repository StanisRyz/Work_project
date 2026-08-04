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

Транспорт RT-2 не включает браузерную часть — она добавлена в RT-3 ниже и
охватывает только уведомления.

---

# RT-3: браузер, колокольчик и toast

## 21. Архитектура RT-3

```
Redis Pub/Sub
     │  event: notification.created / notification.read
     ▼
GET /realtime/events/  (SSE, персональный канал user:<id>)
     │
     ▼  «что-то изменилось» — только сигнал, без данных
static/js/realtime.js
     │  debounce 150 мс, один активный запрос
     ▼
GET /notifications/header-fragment/   (обычный авторизованный Django endpoint)
     │  unread_count + items_html, отрисованный Django
     ▼
[data-notification-items] ← innerHTML     toast region ← toast из этой же разметки
```

Главный принцип: **SSE — сигнал, а не источник данных.** Событие сообщает
только факт изменения. Весь видимый текст и все ссылки приходят из обычного
Django-endpoint с полной проверкой прав. Payload события никогда не попадает в
DOM.

## 22. Общий сервис состояния колокольчика

`notifications.services.get_notification_header_state(user)` возвращает
`unread_count`, до пяти последних непрочитанных (`HEADER_NOTIFICATION_LIMIT`) с
нужными `select_related` и `latest_notification_id`. Его используют и context
processor (полная загрузка страницы), и fragment endpoint, поэтому ORM-запрос
не дублируется, а два пути не могут разойтись. Анонимный пользователь не
выполняет ни одного запроса.

Разметка элементов вынесена в
`templates/notifications/includes/header_items.html` и подключается и в
`header.html`, и в endpoint через `render_to_string`.

## 23. Fragment endpoint

```
GET /notifications/header-fragment/
```

| Свойство | Значение |
| --- | --- |
| получатель | всегда `request.user`; параметр пользователя не принимается |
| метод | только GET, состояние не изменяется |
| ответ | `unread_count`, `items_html`, `generated_at`, `latest_notification_id` |
| кэширование | `Cache-Control: no-cache, no-store, must-revalidate, private`, `Vary: Cookie` |

`items_html` формирует Django из общего partial. JavaScript не собирает
разметку уведомлений.

## 24. Конфигурация клиента

`realtime.context_processors.realtime_client_config` отдаёт в шаблон только:
флаг `enabled`, URL SSE-endpoint, URL fragment-endpoint и URL страницы всех
уведомлений — все три через `reverse()`. Ни Redis URL, ни имени канала, ни
credentials, ни user id.

`base.html` выводит скрытый элемент `[data-realtime-config]` и подключает
`static/js/realtime.js` **только** авторизованному пользователю и **только**
при `REALTIME_ENABLED=true`. При выключенном real-time ни конфигурации, ни
скрипта на странице нет, и `EventSource` не создаётся.

## 25. Жизненный цикл EventSource

1. Клиент находит серверную конфигурацию; при её отсутствии, при
   `data-realtime-enabled != "true"` или при отсутствии `window.EventSource`
   ничего не делает.
2. Повторная инициализация исключена флагом на `window`.
3. Открывается **один** `EventSource` на `/realtime/events/` без каких-либо
   параметров: ни user id, ни target, ни channel.
4. Слушаются только `notification.created` и `notification.read`; остальные
   типы игнорируются молча.
5. Собственного reconnect-цикла нет — переподключением занимается сам
   `EventSource`.
6. На `pagehide` клиент закрывает поток.

### Refresh после каждого open

Любой `open` — первый и каждый последующий после переподключения — ставит в
очередь обновление fragment. Это и есть сверка состояния после пропущенных
событий, офлайна или перезапуска Redis. Toast при этом не показывается:
очередь toast'ов на open пуста, поэтому уже существующие уведомления не
всплывают.

## 26. Обработка событий

**`notification.created`** — валидируется минимальная структура
(`event_type`, `resource_type == "notification"`, положительный целый
`resource_id`), берётся `event.lastEventId` или `event_id` из payload,
ставится в очередь обновление fragment с пометкой «показать toast для этого
`resource_id`». После получения fragment обновляются счётчик и разметка, затем
показывается toast.

**`notification.read`** — toast не показывается; обновляются только fragment и
счётчик, чем и синхронизируются остальные вкладки. `notification_ids`
намеренно не используются: при `scope=all` их в payload нет, и единственным
источником истины остаётся fragment.

## 27. Debounce, отмена и защита от устаревших ответов

- debounce 150 мс: серия событий схлопывается в один запрос fragment;
- одновременно активен максимум один запрос — предыдущий отменяется через
  `AbortController`;
- каждый запрос получает номер поколения; ответ с устаревшим номером
  отбрасывается и не перезаписывает более свежее состояние;
- ошибка запроса не удаляет текущую разметку и не сбрасывает счётчик.

## 28. Дедупликация

`event_id` хранятся в ограниченном FIFO на 100 значений в памяти вкладки.
Повторное событие не показывает второй toast, но может инициировать безопасную
сверку состояния. Коллекция не растёт бесконечно.

## 29. Toast

Заголовок, сообщение и ссылка берутся **из обновлённой серверной разметки** по
`data-notification-id`, а не из события. Если уведомление не попало в последние
пять, показывается универсальный toast со ссылкой на страницу уведомлений.
Весь текст вставляется через `textContent`.

Доступность: регион `aria-live="polite"`, `aria-relevant="additions"`, каждый
toast — `role="status"`; закрытие кнопкой с `aria-label` и клавишей `Escape`;
автозакрытие через 8 с с паузой при hover и focus; максимум три видимых toast;
видимый focus-outline; поддержка `prefers-reduced-motion`. Регион расположен
справа снизу и не перекрывает колокольчик, профиль и основные кнопки.

## 30. Совместимость с отметкой прочитанным

Логика открытия колокольчика не изменилась: показанные непрочитанные
уведомления отмечаются существующим POST с CSRF и `credentials`, текущая
вкладка обновляется сразу, остальные — по событию `notification.read`.

Заменяется только **содержимое** контейнера `[data-notification-items]`, а не
сам контейнер и не `<details>`, поэтому единственный обработчик `toggle`
продолжает находить новые элементы и не регистрируется повторно. Общие правила
DOM колокольчика живут в `app.js` и публикуются как
`window.qualityNotificationMenu`, чтобы `realtime.js` не дублировал их.

## 31. Ошибки

- ошибка SSE не показывается пользователю и не удаляет текущий UI;
- переподключение выполняет сам браузер, поверх него ничего не строится;
- при следующем `open` выполняется сверка;
- при 401/403 клиент останавливается: без повторной авторизации и без
  бесконечного цикла, обычный logout работает как раньше;
- fallback polling в RT-3 не добавляется.

## 32. Границы RT-3

RT-3 охватывал только уведомления. Задачи, реестр актов и открытый акт
добавлены в RT-4 ниже.

## 33. Проверка клиента

JavaScript проверяется без npm и Jest: `realtime/tests/js/dom_harness.js` —
самописный минимальный DOM, таймеры, `EventSource` и `fetch`, а
`realtime/tests/js/realtime_client_test.js` прогоняет через них реальный
`static/js/realtime.js`. Запускается на обычном Node и участвует в
`manage.py test` через `realtime/tests/test_js_client.py`; при отсутствии Node
тест пропускается.

---

# RT-4: задачи, реестр актов и открытый акт

## 34. Общий browser event bus

```
                    ┌──────────────────────────────┐
   один EventSource │  ядро realtime.js            │
   /realtime/events/│  • lifecycle и reconnect     │
        ────────────▶  • дедупликация event_id     │
                    │  • bus: subscribe / onOpen   │
                    └───────┬──────────────────────┘
                            │  (модули не создают EventSource)
        ┌───────────────┬───┴────────────┬──────────────────┐
        ▼               ▼                ▼                  ▼
  notifications     task list       act registry       act detail
  колокольчик+toast   таблица       KPI + таблица   summary/history/
                                                    comments/activities
```

Ядро владеет единственным `EventSource`, ограниченной дедупликацией
`event_id` (100 значений) и переподключением. Функциональные модули
подписываются через `subscribe(eventType, handler)` и `onOpen(handler)` и
никогда не обращаются к транспорту. Повторное подключение скрипта не создаёт
второй поток (флаг на `window`).

**Гонки инициализации нет:** клиент стартует по `DOMContentLoaded` (скрипт
подключён с `defer`, поэтому на момент выполнения `readyState` ещё
`interactive`), а все модули регистрируются до открытия потока. Событие,
пришедшее сразу после connect, всегда находит обработчик.

## 35. Координатор обновлений

`createRefreshCoordinator({url, apply, onDenied, onError})` — по одному на
каждый live-блок:

- debounce 150 мс — серия событий схлопывается в один запрос;
- одновременно активен максимум один запрос, предыдущий отменяется через
  `AbortController`;
- generation counter: ответ с устаревшим номером отбрасывается и не
  перезаписывает более свежее состояние;
- ошибка запроса не очищает разметку и не сбрасывает счётчики;
- `401/403/404` останавливает координатор.

## 36. Список задач

`tasks.selectors.build_task_list_state(user, query_params)` — единственный
источник вкладки (`my`/`all`/`archive`), валидации фильтров, номера, источника,
срока, сортировки, `tab_urls`, `reset_url`, `sort_url`, `today` и итогового
queryset. Его используют и обычный `task_list`, и fragment endpoint.

`templates/tasks/includes/list_results.html` — только таблица, строки, overdue
и пустое состояние. Вкладки и форма фильтров живут снаружи и при live refresh
не заменяются.

```
GET /tasks/list-fragment/?<те же параметры, что у списка>
→ {results_html, tab, task_ids, generated_at}
```

Контейнер `data-live-task-list` хранит `data-fragment-url`, а клиент
дописывает текущий query string страницы, поэтому вкладка, фильтры и сортировка
сохраняются. События `task.created`, `task.updated`, `task.completed` вызывают
refresh; подходит ли задача пользователю и фильтру, решает Django queryset.

## 37. Реестр актов

`acts.selectors.build_act_list_state(user, query_params)` — scope, права,
фильтры по статусу/типу/сроку, поиск, KPI, queryset, `has_visible_acts`,
`has_filters` и выбранные значения.

Partials: `acts/includes/registry_kpis.html` и
`acts/includes/registry_results.html`. Вкладки и фильтры не заменяются.

```
GET /acts/list-fragment/?<те же параметры, что у реестра>
→ {kpis_html, results_html, act_ids, generated_at}
```

События `act.updated` и `act.status_changed` обновляют KPI и результаты;
scope, фильтры, поиск, фокус и позиция прокрутки сохраняются.

## 38. Открытый акт

Общие серверные helpers (`acts/selectors.py`): `build_route_steps`,
`get_history_events` + `group_history_events`, `get_act_comments`,
`get_related_tasks`. Полная страница и fragments используют их одинаково.

Безопасные partials — только read-only, без форм:

| Partial | Содержимое |
| --- | --- |
| `live_summary.html` | номер, status badge, маршрут |
| `history_content.html` | только история |
| `comments_list.html` | только список комментариев, без формы |
| `activities_content.html` | связанные мероприятия и задачи |

Endpoints (каждый заново загружает акт, проверяет `can_view_act`, не принимает
user_id, ничего не изменяет и запрещает кэширование):

```
GET /acts/<pk>/live-summary-fragment/
GET /acts/<pk>/history-fragment/
GET /acts/<pk>/comments-fragment/
GET /acts/<pk>/activities-fragment/
→ {html, generated_at}   (+ status_code у summary)
```

URL формируются на сервере в `[data-live-act-config]` — клиент не собирает их
по ID. Контейнеры: `data-live-act-summary`, `data-live-act-history`,
`data-live-act-comments`, `data-live-act-activities`.

Маршрутизация событий:

| Событие | Обновляется |
| --- | --- |
| `act.updated` | summary |
| `act.status_changed` | summary + history |
| `comment.created` | список комментариев |
| `task.created` / `task.updated` / `task.completed` | связанные мероприятия |

Обновляются только блоки, присутствующие на текущей вкладке. Открытие другой
вкладки — обычная серверная загрузка, то есть данные и так актуальны.

## 39. Что не заменяется

Форма решения КО, форма анализа ТО и её динамические строки, textarea нового
комментария, формы возврата, форма вложений и любые выбранные исполнители
находятся **вне** заменяемых partials и не трогаются никогда.

## 40. Dirty-state, conflict banner и устаревшие действия

Форма считается изменённой только после реального пользовательского жеста:
`input`/`change` на `input`/`textarea`/`select` либо добавление/удаление
динамической строки. Программная замена fragment не диспатчит событий и не
может дать ложное срабатывание.

При `act.updated` или `act.status_changed` с dirty-формой:

- рабочий редактируемый блок не заменяется;
- безопасные read-only fragments обновляются;
- показывается постоянный баннер «Акт изменён другим пользователем. Перед
  сохранением загрузите актуальную версию» с кнопкой обновления страницы;
- введённый текст не удаляется и не блокируется — его можно скопировать.

Дополнительно при `act.status_changed` отключаются кнопки
`[data-workflow-submit]`, которые работали бы с устаревшим статусом. Обычные
поля и textarea остаются доступными. Серверная блокировка строки и повторная
проверка статуса (PG-3) остаются окончательной защитой от устаревшего POST.

## 41. Потеря доступа

Если fragment вернул `403/404`, клиент останавливает обновление этого акта,
отключает workflow-кнопки и показывает баннер «Акт изменён или больше
недоступен» со ссылкой на реестр. Технический текст ошибки пользователю не
показывается, а сервер не отдаёт никаких данных объекта.

## 42. Reconnect и правила toast

Каждый `open`/reconnect обновляет колокольчик RT-3 **и** все присутствующие
task/act блоки. Toast при этом не показывается, dirty-формы не заменяются.

Toast создаёт **только** `notification.created`. `task.*`, `act.*` и
`comment.created` обновляют блоки молча: пользователь уже получает toast через
соответствующее внутреннее уведомление, если оно предусмотрено бизнес-логикой.

## 43. Границы RT-4

Не входит в этот этап:

- дополнительные SSE-соединения;
- подписка браузера на `act:<id>`;
- `/realtime/sync/`;
- fallback polling;
- `BroadcastChannel` и leader-tab;
- WebSocket и Django Channels;
- React и любой frontend-toolchain (npm, Jest);
- replay и хранение событий;
- production reverse proxy и HTTPS;
- нагрузочное тестирование и Redis monitoring.

## 44. Границы RT-4 (продолжение)

RT-4 не включал восстановление после пропущенных событий и координацию вкладок —
это добавлено в RT-5.

---

# RT-5: восстановление, вкладки и пилотная эксплуатация

## 45. Архитектура RT-5

```
        ┌──────────── вкладка-лидер ────────────┐   ┌── вкладка-последователь ──┐
        │  core.js: EventSource + state machine │   │  core.js: тот же bus      │
        │  sync.js: sync + fallback polling     │   │  модули те же             │
        │  tabs.js: lease в localStorage        │   │                           │
        └───────┬───────────────────────┬───────┘   └──────────▲────────────────┘
                │                       │ BroadcastChannel     │
     /realtime/events/ (SSE)   /realtime/sync/ (revisions) ────┘
```

Одна SSE-подписка на пользователя, а не на вкладку. Ядро едино, модули
физически разделены:

| Файл | Ответственность |
| --- | --- |
| `static/js/realtime/core.js` | EventSource, event bus, дедупликация, state machine, refresh coordinator |
| `static/js/realtime/tabs.js` | BroadcastChannel, leader lease, fallback без этих API |
| `static/js/realtime/sync.js` | `/realtime/sync/`, сравнение revisions, fallback polling |
| `static/js/realtime/notifications.js` | колокольчик и toast |
| `static/js/realtime/tasks.js` | реестр задач |
| `static/js/realtime/acts.js` | реестр актов и открытый акт |
| `static/js/realtime/start.js` | запуск после готовности DOM, degraded-индикатор |

Скрипты подключаются `defer` в этом порядке. Никаких npm, bundler и сторонних
библиотек.

## 46. `act.created`

| Свойство | Значение |
| --- | --- |
| resource | `act` |
| data | `status_code`, `author_id` |
| когда | внутри транзакции создания акта, публикация — после commit |
| откат | событие не публикуется |

Получатели: автор акта и **все активные пользователи с полным доступом** к
актам (менеджеры, администраторы, superusers). КО и ТО намеренно исключены:
текущие права не дают им видеть акт в `CREATED_OTK`, и рассылка сообщила бы им
о его существовании. Неактивные пользователи, неактивные профили и дубли
отбрасываются.

На реестре актов событие обновляет KPI и таблицу **молча** — toast не
показывается: пользователь уже получает toast через соответствующее внутреннее
уведомление, если бизнес-логика его предусматривает.

## 47. Revision tokens

`realtime/sync.py`. Пять независимых блоков:

| Ключ | Из чего считается |
| --- | --- |
| `notifications` | unread count, всего записей, максимальные `created_at` и `read_at` |
| `tasks` | число видимых задач, максимальные `created_at`/`updated_at`/`completed_at`, число назначений, разрез по статусам |
| `acts` | число видимых активных и архивных актов, максимальный `updated_at`, разрез по статусам |
| `comments` | counts и максимальные `created_at` комментариев и истории в доступных актах |
| `activities` | видимые связанные задачи, их `updated_at`/`completed_at` и статусы |

Каждый токен — 16 hex-символов SHA-256 от агрегатов. Полные строки моделей не
сериализуются, идентификаторы и тексты в токен не попадают. Всё считается по
тем же visible querysets, что и страницы, поэтому чужой объект не может
сдвинуть токен пользователя.

`Task.updated_at` (`auto_now=True`, миграция `tasks/0004_task_updated_at.py`)
добавлен именно ради дешёвого токена задач.

Число SQL-запросов фиксировано и не растёт вместе с данными — это закреплено
тестом `assertNumQueries`.

## 48. Sync endpoint

```
GET /realtime/sync/
→ {schema_version, generated_at, revisions{…}, unread_notifications}
```

Только авторизованный пользователь; получатель — всегда `request.user`,
параметр пользователя не принимается; GET ничего не изменяет; ответ
`no-cache, no-store, must-revalidate, private` + `Vary: Cookie`. Ни Redis URL,
ни имени канала, ни данных чужих объектов в ответе нет.

## 49. State machine

| Состояние | Смысл |
| --- | --- |
| `idle` | клиент создан, поток ещё не открывался |
| `connecting` | поток открывается или переподключается |
| `live` | поток работает |
| `degraded` | поток не доставляет; работает fallback polling |
| `offline` | браузер offline |
| `stopped` | терминальное: 401/403 или выход |

Переходы: запуск → `connecting`; `open` → `live` (плюс sync и остановка
polling); нет `open` дольше `REALTIME_DEGRADED_AFTER_SECONDS` → `degraded`;
`navigator` offline → `offline`; `online` → `connecting` и немедленный sync;
401/403 от sync или fragment → `stopped`. Собственного reconnect-цикла поверх
`EventSource` нет.

## 50. Fallback polling

Включается, когда `EventSource` недоступен или клиент перешёл в `degraded`.
Использует `/realtime/sync/`: активная вкладка ≈ раз в 30 с, скрытая ≈ раз в
90 с, с небольшим jitter. При `navigator.onLine === false` запросы не идут.
Одновременно активен максимум один sync-запрос. При восстановлении SSE polling
останавливается немедленно.

Одинаковые revisions **не запускают** ни одного fragment-запроса.

## 51. BroadcastChannel и leader lease

- канал `quality-realtime-v1`;
- lease в `localStorage` под ключом `quality-realtime-leader-v1`:
  `{tab_id, expires_at}`;
- лидер продлевает аренду каждые `REALTIME_LEADER_HEARTBEAT_SECONDS`, срок
  жизни — `REALTIME_LEADER_LEASE_SECONDS`;
- при истечении аренды оставшиеся вкладки после небольшой случайной задержки
  пытаются стать лидером; кратковременные два лидера допустимы — дедупликация
  по `event_id` не даст двойного toast;
- при `pagehide` лидер освобождает аренду, если она всё ещё его;
- лидер передаёт последователям нормализованные события и sync snapshots —
  ровно те данные, которые сервер и так отдал бы этому пользователю.

**Без BroadcastChannel или localStorage** каждая вкладка работает
самостоятельно: своё соединение, свой polling. Отсутствие API не отключает
real-time, а исключение `localStorage` не ломает страницу.

## 52. Максимальное время соединения

`REALTIME_MAX_CONNECTION_SECONDS` (по умолчанию 900 с). Сервер закрывает поток
контролируемо: heartbeat работает до самого конца, PubSub и клиент
освобождаются, закрытие пишется как `reason=max_lifetime`, а не как ошибка.
Браузер переподключается штатно, и новый запрос заново проходит session
authentication и проверку прав — поэтому истёкшая сессия не может пережить
соединение.

## 53. Системные проверки

`realtime/checks.py`, выполняются `manage.py check`:

| ID | Проблема |
| --- | --- |
| `realtime.E001` | `REALTIME_ENABLED=true` с Noop publisher |
| `realtime.E002` | Redis URL не задан |
| `realtime.E003` | Redis URL некорректен |
| `realtime.E004` | Схема не `redis://` и не `rediss://` |
| `realtime.E005` | Недопустимый `REALTIME_CHANNEL_PREFIX` |
| `realtime.E006` | Не задан `ASGI_APPLICATION` |
| `realtime.E007` | Heartbeat не меньше времени жизни соединения |
| `realtime.E008` | Leader heartbeat не меньше lease |
| `realtime.W001` | Скрытая вкладка опрашивает чаще активной |

При `DEBUG=True` часть проблем — предупреждения; при `DEBUG=False` опасная
конфигурация становится ошибкой. Ни одно сообщение не содержит пароль Redis
или URL с учётными данными — только безопасный адрес.

## 54. Логирование

Logger `realtime`, структурированные события:
`realtime.connection_opened`, `connection_closed`, `connection_cancelled`,
`redis_disconnected`, `sync_completed`, `sync_slow`, `invalid_message`.

В записях: идентификатор соединения, идентификатор пользователя, длительность,
причина закрытия, количество отправленных событий, время сверки. **Нет**
payload, текстов, email и учётных данных.

## 55. Индикатор degraded

Ненавязчивая надпись «Обновления могут приходить с задержкой» появляется
только после перехода в `degraded` (или `offline`) — кратковременная попытка
переподключения её не показывает — и скрывается сама при возврате в `live`.
Регион `role="status"` + `aria-live="polite"`, поэтому screen reader не
получает постоянного спама.

## 56. Тестовые сценарии

Серверные: публикация `act.created` после commit и её отсутствие при откате;
получатели `act.created`; права и актуальность work fragment; revision tokens
и изоляция пользователей; системные проверки; время жизни соединения; новые
настройки; отсутствие credentials в логах и сообщениях.

Клиентские (Node harness, без npm): один EventSource на любой странице;
модули на общем ядре; только лидер держит поток; последователь получает
события через BroadcastChannel; переизбрание лидера; работа без
BroadcastChannel; degraded timeout запускает polling; `open` его останавливает;
одинаковые revisions не дают fragment-запросов; изменённый токен обновляет
только свой блок; скрытая вкладка использует увеличенный интервал;
offline/online меняют состояние; 401 останавливает клиента; dirty form не
заменяется; чистый work fragment обновляется; новый акт обновляет реестр; toast
не дублируется; сценарии RT-3 и RT-4 продолжают работать.

## 57. Границы best-effort доставки

Гарантий доставки нет и не планируется:

- событие, опубликованное при недоступном Redis, теряется;
- клиент без открытого соединения событий не получает;
- порядок между разными блоками не гарантирован.

Это допустимо, потому что **актуальность обеспечивает сверка**, а не поток:
каждый connect/reconnect и каждый poll в degraded-режиме приводят UI в
соответствие с базой. Гарантированная доставка потребовала бы transactional
outbox и подтверждений — сознательно вне этого этапа.

## 58. Границы RT-5

Не входит: GitHub Actions; WebSocket и Django Channels; React и любой
frontend-фреймворк; npm/bundler; Celery; хранение и replay событий;
transactional outbox; подтверждение доставки; чат, presence и typing
indicators; production-развёртывание; реальные reverse proxy, HTTPS и
Redis-конфигурации; рабочие секреты и пароли.

Требования к среде для пилота — в
[Развёртывание real-time](realtime_production.md).

## 59. План RT-6

1. Точечное обновление строк вместо замены таблицы, если объём данных этого
   потребует.
2. Авторизуемые подписки на `act:<id>` через `can_view_act`.
3. Реальное production-развёртывание по `docs/realtime_production.md`.
4. При необходимости гарантированной доставки — transactional outbox.

Новые типы событий по-прежнему добавляются только в централизованный enum и
обязательно покрываются тестами контракта, targets и интеграции.
