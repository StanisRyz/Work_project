# Автоматическая обработка email-очереди

## Как работает очередь

Пользовательское действие, внутренняя запись `Notification` и соответствующая
`NotificationDelivery` создаются в транзакции бизнес-операции. SMTP в HTTP-запросе
не вызывается. Отдельное серверное задание раз в минуту запускает одну пачку команды:

```text
python manage.py process_notification_deliveries --batch-size 100
```

Команда восстанавливает зависшие `processing`, выбирает не более 100 готовых
`pending`-доставок и завершает работу после этой пачки. Условный атомарный перевод
`pending -> processing` даёт право отправки только одному пересекающемуся процессу.
Статусы `sent`, `failed` и `skipped` повторно не выбираются. После сбоя процесса между
ответом SMTP и фиксацией `sent` абсолютная гарантия exactly-once невозможна без
идемпотентности SMTP-провайдера; интервал восстановления должен заведомо превышать
SMTP timeout и максимальную нормальную длительность отправки.

## Linux: systemd

1. Создайте отдельного непривилегированного пользователя и разверните проект с
   виртуальным окружением. В unit-файле измените `User` и `Group` на эту учётную запись.
2. Скопируйте пример окружения, задайте фактические пути и SMTP-параметры:

```bash
sudo install -d -m 0750 /etc/quality-ecosystem
sudo install -m 0600 deploy/systemd/email-queue.env.example /etc/quality-ecosystem/email-queue.env
sudo editor /etc/quality-ecosystem/email-queue.env
```

3. Установите и активируйте units:

```bash
sudo install -m 0644 deploy/systemd/quality-email-queue.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/quality-email-queue.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quality-email-queue.timer
```

`WantedBy=timers.target` включает таймер после перезагрузки, `Persistent=true`
выполняет пропущенный запуск после простоя. systemd не создаёт второй экземпляр уже
активного oneshot-service.

Проверка и журналы:

```bash
systemctl status quality-email-queue.timer
systemctl list-timers quality-email-queue.timer
sudo systemctl start quality-email-queue.service
journalctl -u quality-email-queue.service -n 100 --no-pager
journalctl -u quality-email-queue.service -f
```

Безопасное отключение и удаление:

```bash
sudo systemctl disable --now quality-email-queue.timer
sudo systemctl stop quality-email-queue.service
# После проверки состояния при необходимости удалите оба unit-файла и выполните:
sudo systemctl daemon-reload
```

## Windows Server: Task Scheduler

Запустите PowerShell от имени администратора. Используйте отдельную служебную
учётную запись, дайте ей чтение проекта, выполнение Python и доступ к БД/журналам.
SMTP-переменные задайте в окружении этой учётной записи либо как машинные переменные;
пароли не передавайте аргументами задания и не храните в репозитории.

```powershell
$credential = Get-Credential 'DOMAIN\quality-service'
.\deploy\windows\Register-EmailQueueTask.ps1 `
  -ProjectPath 'D:\Apps\QualityEcosystem' `
  -PythonPath 'D:\Apps\QualityEcosystem\.venv\Scripts\python.exe' `
  -TaskName 'QualityEcosystem-EmailQueue' `
  -LogDirectory 'D:\Logs\QualityEcosystem' `
  -Credential $credential
```

Пароль передаётся API Task Scheduler и не записывается в скрипт. Задание работает
без открытого терминала. `MultipleInstances=IgnoreNew` не запускает второй экземпляр,
если предыдущий ещё выполняется.

Проверка, ручной старт и история:

```powershell
Get-ScheduledTask -TaskName 'QualityEcosystem-EmailQueue' | Format-List *
Get-ScheduledTaskInfo -TaskName 'QualityEcosystem-EmailQueue'
Start-ScheduledTask -TaskName 'QualityEcosystem-EmailQueue'
Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 100
```

Вывод management-команды записывается в ежедневные файлы
`email-queue-YYYY-MM-DD.log` в `-LogDirectory` (по умолчанию `var\log` проекта), а
запуски и коды задания — в журнал Task Scheduler. Настройте штатную ротацию/сбор
этих файлов; не добавляйте SMTP-пароли в командную строку или журналы.

Безопасное удаление с подтверждением:

```powershell
# Сначала можно остановить и отключить автоматизацию, сохранив задание:
Stop-ScheduledTask -TaskName 'QualityEcosystem-EmailQueue' -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName 'QualityEcosystem-EmailQueue'

# Для полного удаления используйте отдельный скрипт:
.\deploy\windows\Unregister-EmailQueueTask.ps1 -TaskName 'QualityEcosystem-EmailQueue'
```

## Ручная проверка и изменение SMTP

Сначала оставьте `EMAIL_NOTIFICATIONS_ENABLED=false`, проверьте миграции и Django:

```text
python manage.py migrate
python manage.py check --deploy
python manage.py process_notification_deliveries --batch-size 100
```

После получения SMTP-параметров оставьте таймер/задание отключённым, заполните внешний
environment-файл/окружение служебной учётной записи и установите
`EMAIL_NOTIFICATIONS_ENABLED=true`. Создайте одну новую тестовую доставку на тестовый
ящик, один раз выполните management-команду вручную, проверьте статус `sent` и журналы
и только затем включите таймер/задание. После каждого изменения SMTP повторите ручную
обработку одной новой тестовой доставки; oneshot-service не требует постоянного
restart, а следующее выполнение прочитает новое окружение.

Важно: при `EMAIL_NOTIFICATIONS_ENABLED=false` новые email-доставки создаются сразу
со статусом `skipped`. Они намеренно не становятся старой рассылочной очередью после
включения SMTP и автоматически отправлены не будут.

Celery, Redis, APScheduler и планировщик внутри WSGI/ASGI не используются. Добавленные
units и скрипты — конфигурация репозитория; фактическая автоматизация начнёт работать
только после установки и активации на выбранном сервере.
