# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

Internal notifications and a prepared asynchronous email-delivery channel are complete. Repository configuration is available for minute-by-minute processing through Linux systemd or Windows Task Scheduler; it must still be installed and activated on the deployment server. Celery and Redis are not used. The full-width act-card redesign is deferred; the current act page and its permission model remain unchanged by this stage.

The `notifications` app stores durable in-app notifications separately from delivery attempts. The top bar shows an unread counter and up to five recent unread events; `/notifications/` provides paginated `Все` and `Непрочитанные` views plus protected POST actions for marking one or all items read. Opening the bell menu marks the unread notifications currently shown in it as read, via an authenticated, CSRF-protected, owner-scoped POST that updates the counter and menu in place without a page reload: the marked items are removed from the menu (not just unhighlighted), and `Новых уведомлений нет.` is shown once none remain. This never marks the rest of the recipient's notification history, which stays a separate `Отметить все прочитанными` action. Every query and update is scoped to the authenticated recipient. Related-act links still pass through the normal act visibility checks.

`Notification` stores recipient, actor, event type, safe event text, related act, deduplication key, creation time, and independent read state. `NotificationDelivery` stores the email channel state (`pending`, `processing`, `sent`, `failed`, or `skipped`), attempts, timestamps, retry availability, and a sanitized error. Event and recipient uniqueness prevents duplicate notification fan-out.

### Notification routing

| Event | Recipients | Email eligible |
| --- | --- | --- |
| OTK sends to KO | All active users with the KO role | Yes |
| KO sends to TO | All active users with the TO role | Yes |
| TO sends to OTK verification | The act author | Yes |
| Return to OTK / KO / TO | Act author / all active KO users / all active TO users | Yes |
| Corrective action assignment | Only its active assignees; duplicates are removed | Yes |
| Act approval | Active act participants except the actor | No, in-app only |
| Normal comment | Relevant participants who can currently view the act, except the author | No, in-app only |

Return comments do not create a second comment notification: the recipient receives the more specific return event. A queue actor is excluded from their own event where applicable; a self-assigned employee still receives the individual assignment notification. Notification creation and workflow data share one database transaction. Email is processed later by a separate server task, so SMTP downtime cannot roll back an act transition, comment, assignment, or its internal notification. SMTP is never called from the user HTTP request.

### Email delivery configuration

Email is disabled by default. Eligible notifications created while it is disabled receive a `skipped` delivery and are never released as an old backlog after SMTP is enabled. With email enabled, recipients without an address are also recorded as `skipped`. Messages contain only the event, act number, required action, actor, date, and protected act URL—never defect data, attachments, or comment text.

Environment variables:

- `EMAIL_NOTIFICATIONS_ENABLED` — `false` by default.
- `EMAIL_BACKEND` — defaults to `django.core.mail.backends.console.EmailBackend`; use the locmem backend in tests.
- `APP_BASE_URL`, `DEFAULT_FROM_EMAIL` — public application root and approved sender.
- `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL` — SMTP endpoint and transport security.
- `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` — credentials supplied only through the deployment environment.
- `EMAIL_TIMEOUT` — SMTP timeout in seconds.
- `EMAIL_NOTIFICATION_MAX_ATTEMPTS`, `EMAIL_NOTIFICATION_RETRY_DELAY_SECONDS` — retry policy.
- `EMAIL_NOTIFICATION_BATCH_SIZE`, `EMAIL_NOTIFICATION_PROCESSING_TIMEOUT_SECONDS` — worker batch and interrupted-processing recovery limits.

One invocation processes one batch and exits normally. The default batch size is 100:

```powershell
python manage.py process_notification_deliveries --batch-size 100
```

Overlapping workers use an atomic `pending -> processing` claim, so only one may send a given delivery. Successfully sent and skipped deliveries are not selected again; interrupted `processing` deliveries and retryable failures retain the existing recovery policy. The provided systemd timer and Windows scheduled task run every minute and also suppress a second active instance.

Installation, activation, status checks, logs, manual testing, SMTP-change procedure, and safe removal are documented in [Automatic email queue processing](docs/email_queue_automation.md). The repository only provides configuration and instructions: email processing is not active on a production server until an administrator installs the appropriate scheduler configuration, supplies an external environment, and enables it.

Before enabling corporate delivery, IT must provide the SMTP/Exchange host, port, TLS or SSL mode, supported authentication method, service username/password if SMTP AUTH is allowed, approved `DEFAULT_FROM_EMAIL`, relay/IP allow-list requirements, CA certificate requirements, and outbound firewall permission. Set `APP_BASE_URL` to the external HTTPS origin, apply migrations, test with one newly created delivery and one mailbox, install the scheduler, and only then set `EMAIL_NOTIFICATIONS_ENABLED=true`. Deliveries created while disabled remain `skipped` and are deliberately not sent later. Exchange OAuth-only delivery would require a compatible custom email backend and remains a deployment integration step.

D28 — mandatory defect workshop/supplier selection.

D28 adds a required `Цех/поставщик` choice to every act defect: `Цех МП` or `Цех трансформаторов`. `ActDefect.workshop` remains optional at the database level so existing defects are never assigned an invented value; `ActDefectForm` requires a real selection for every new or edited defect. In the create and edit act forms, each defect row shows only its `Цех/поставщик` dropdown, positioned above `Номер ЗНП`; the remaining defect fields (`Номер ЗНП`, `Номер партии`, `Вид дефекта`, `Операция`, `Тип МП`, `Дата обнаружения`, `Проверено`, `С отклонением`, `Описание дефекта`) stay hidden until a value is chosen, then appear immediately without a page reload and without losing already-entered data. This applies independently to the first defect row, rows added with `Добавить ещё дефект`, existing rows when editing, and rows redisplayed after a validation error. Hidden fields use the HTML `hidden` attribute so the browser never blocks submission on a field the user cannot see; server-side validation remains the authoritative check. Both workshop choices currently share identical fields, validation, and workflow; no workshop-specific business logic exists yet. The saved value is shown on the act detail defects table and the print view; legacy defects saved before this field existed display a placeholder instead of an invented value.

D27 — compact acts registry.

D27 simplifies `/acts/` without changing workflow or access rules. The topbar title is `Акты`; the introductory, duplicate role/access, and administrator-mode notices are removed. The compact filter panel keeps search, current workflow statuses, act type, and a due-date filter. Act types are `Операционный контроль` and the prepared future `Входной контроль`; existing acts default to operational control. `Просроченные` means a deadline strictly before the current local date; today and future dates are `Не просроченные`. The fixed-height, scrollable registry table shows only number, creation date, type, status, and due date; on the `Архив` tab the creation-date column is replaced by the archiving date. The number remains a protected detail link. Archived acts appear only on the `Архив` tab, including for full-access users. Permitted `Создать АКТ` plus the dedicated administrator-only cleanup action are below it. The operation filter is removed from the registry only; operation data remains in act creation and details.

At `На рассмотрении КО`, each defect uses the same ordered decision list: prohibit use; allow use with a deviation and no rework; allow use with a deviation and rework; allow use without a deviation and rework. Each valid choice keeps the existing transition to TO analysis.

In TO analysis, an employee selector is disabled until the department selector in the same assignee row is filled. It then offers only active employees from that department. Every additional assignee has an independent department and employee selector; server validation enforces the same department match. A selected employee is unavailable in the other assignee rows of the same corrective action, while remaining available in other actions.

D26 — task execution card and mandatory result.

D26 turns task details into a compact card with registry-style metadata, vertical assignees, root cause, and task text. An assigned executor or administrator must enter a non-empty execution result to complete a shared task. Completion saves the result, executor, and timestamp atomically; the task becomes visible only in `Архив` and redirects there filtered by its number. Managers remain view-only unless assigned.

D25 — working task registry.

D25 adds `Мои задачи`, `Все задачи`, and `Архив` tabs to `/tasks/`. Filters for task number, source act, registry status, and due-date state, plus due-date sorting, are kept in the URL and combine with AND logic. `Сбросить` retains the selected tab. Active tasks keep overdue-first default ordering; explicit sorting overrides it, while completed archive tasks are never marked overdue. Existing task visibility remains authoritative for every tab.

D24 — compact task registry and cross-department assignees.

D24 keeps the corrective action department as the department responsible for the action, while its active assignees may belong to different departments. The TO form selects employees directly and shows each employee's actual department; no temporary per-assignee department data is used, so assignments are preserved when OTK returns an act to TO.

The `/tasks/` registry now has only `№ задачи`, `Статус`, `Источник`, and `Срок`. The task primary key is the clickable number, the registry status is always `По акту`, and the source act is linked. Overdue tasks remain first and visibly marked; technical `IN_PROGRESS` and `COMPLETED` statuses remain in task execution and detail pages.

D23 — shared corrective-action tasks with multiple assignees.

D23 replaces the single responsible employee with `ActCorrectiveActionAssignee` and `TaskAssignee`. Every corrective action has one or more unique active employees, including employees from other departments. OTK approval creates exactly one shared `Task` per action and creates all its assignee records in the same transaction as approval and archival. Existing single responsible users are copied into the new relations by migrations.

An ordinary employee can view and complete a task only when assigned to it; managers and administrators retain full visibility. Completion changes the single shared task to `COMPLETED` (`Выполнено`) atomically, records who completed it and when, and is immediately visible to every assignee. A completed task cannot be completed again. Archived acts remain read-only and show assignees, linked tasks, and completion metadata.

D22 — tasks from approved corrective actions.

D22 introduced one executable task for every corrective action during the atomic OTK approval transaction. D23 later made these tasks shared by multiple assignees.

D21 — OTK review, approval, and registry scopes.

D21 lets the authorized OTK reviewer return an `OTK_REVIEW` act to TO with a mandatory comment or approve it to terminal `ARCHIVED`, recording the approver and date. D22 extends approval by creating linked executable tasks. The registry has `Мои акты`, `Все акты`, and `Архив` scopes that preserve server-side visibility rules.

D20 — TO analysis is routed to OTK review.

D20 adds the `OTK_REVIEW` (`Проверка ОТК`) stage. From `TO_ANALYSIS`, an authorized user may return the act to KO with a mandatory comment, or submit a fully validated structured analysis for OTK review. The initial analysis structure does not render delete controls; add controls are green and delete controls are red.

D19 — structured TO analysis is embedded on the act detail page.

D19 replaces the separate TO analysis page with a `Корневая проработка` form on `Проработка`. Each root cause contains one or more corrective actions with department, assignees, and due date. The subsequent D20 workflow sends successful analysis to `OTK_REVIEW`; legacy summaries remain compatible and the submitted structure is read-only outside TO correction. Outside `TO_ANALYSIS` (OTK review, archived acts, and any other read-only view of the same table), every assignee is listed as plain text — never the editable department/assignee selects — with a `—` placeholder when an action has none.

D18 — comments moved to the attachments tab and KO return-to-OTK rationale is required.

D18 renames the detail tab to `Вложения и комментарии`, placing attachments first and normal comments below them. KO users must provide a non-empty return comment in the return dialog; the comment, its history event, and the transition from `KO_REVIEW` to `CREATED_OTK` are saved atomically.

D17 — corrected visual layout of the `CREATED_OTK` act detail page.

D17 removes duplicate top-level detection-date metadata and uses responsive party-data and defect-card grids without changing act behavior.

D16 — improved CREATED_OTK act detail page and controlled editing before transfer to KO.

D16 moves the act number to the top header, keeps work data in the prescribed sequence, and allows authorized users to edit party data and defects only while an act remains in `CREATED_OTK`.

D15 — validation for product fields and detected dates in the act creation form.

D15 preserves the existing act creation workflow and adds the following protections:

- `Наименование продукции` accepts Russian letters, digits, spaces, dots, and hyphens; `Обозначение по КД` accepts the same characters except spaces. Django server-side validation remains authoritative.
- Every defect row initially uses the current local date, with that date also set as the latest selectable date.
- Future defect detection dates are rejected by the server.

D14 — структура проработки акта, вкладка вложений и обновлённые решения КО.

D14 обновляет представление и схему решений КО:

- Детальная страница имеет вкладки `Проработка`, `История акта` и `Вложения`.
- Вкладка `Проработка` содержит последовательность данных партии, дефектов, решения КО, анализа ТО и комментариев.
- Вложения отображаются только на одноимённой вкладке.
- Для каждого дефекта требуется отдельное новое решение КО; после заполнения всех решений акт передаётся из `KO_REVIEW` в `TO_ANALYSIS`.
- Старые значения решений КО и исторические события сохранены без преобразования и продолжают отображаться.

D11A makes administrator access explicit and reliable throughout the acts module:

- `admin_user` / `demo12345` is seeded as an `ADMIN` user in `Руководство`, with Django `is_staff=True` and `is_superuser=True`.
- An `ADMIN` profile, or a Django superuser without a usable profile, has full visibility of all acts at every workflow stage.
- Administrators can use every action valid for the act's current stage, including comments, protected attachments, and the print view.
- Administrator access never bypasses invalid status transitions; for example, a KO decision remains unavailable until an act is in `KO_REVIEW`.
- OTK, KO, and TO visibility remains restricted to their normal workflow scope.

D11 keeps the existing `/acts/create/` server-rendered route and reshapes act creation around the production OTK form:

- `Act` remains the MVP model for acts of operational control.
- `Act` now stores optional party/order fields: `customer`, `order_number`, and `znp_number`.
- Existing `Act.defect_type`, `Act.description`, and `Act.due_date` remain summary compatibility fields.
- `ActDefect` stores one or more defect rows for an act: defect type, description, and detected date.
- When a new act is created, the first `ActDefect` row is copied into the summary fields on `Act`.
- The create form has a `Данные партии` block with two-column rows:
  - `Заказчик` + `Номер заказа`
  - `Номер ЗНП` + `Номер партии`
  - `Номенклатура` + `Операция`
- The `Вид дефекта` block supports one or more defect rows using a Django formset.
- The create form operation dropdown is limited to `Операционный контроль` and `Выпускной контроль`.
- The create form defect dropdown is limited to the D11 production defect list.
- `order_number`, `znp_number`, and `party_number` accept only digits, hyphen, and slash.
- `ActHistoryEvent` stores append-only history events for act creation, workflow transitions, comments, attachments, and `ACT_CLOSED`.
- `ActComment` stores manual user notes on an act.
- `ActAttachment` stores protected files uploaded to acts under `MEDIA_ROOT/acts/attachments/<act_id>/`.
- Attachment downloads go through access-checked Django views, not direct media links.
- Role and action checks are centralized in `acts/permissions.py`.
- Workflow transitions and closing logic are centralized in `acts/services.py`.
- Workflow logic uses `ActStatus.code`, not Russian status names.

## Manual Validation Checklist

### D28

- Open `/acts/create/` and verify each defect row shows only the `Цех/поставщик` dropdown, positioned above `Номер ЗНП`, with the remaining defect fields hidden.
- Select `Цех МП` and verify the rest of the fields appear immediately without a page reload; switch to `Цех трансформаторов` and verify already-entered values are preserved. Verify the same behavior for a row added with `Добавить ещё дефект`.
- Submit the form with no `Цех/поставщик` selected and verify a server-side validation error; verify the browser does not block submission due to a hidden required field.
- Create an act with two defects using different workshop values and verify both are saved independently.
- Edit an existing `CREATED_OTK` act and verify its saved `Цех/поставщик` value is preselected, the other fields are visible, and changing the value saves correctly; verify existing quantity validation still rejects an invalid edit.
- Open an act with a legacy defect saved before this field existed and verify it displays a placeholder instead of an error or an invented value, on both the detail page and the print view.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D22

- Approve an `OTK_REVIEW` act with one or more valid corrective actions and verify one `Новая` task per action, then verify the archived-act task links.
- Try approval with an inactive assignee, blank action text, or a past due date; verify a clear error, no tasks, and unchanged `OTK_REVIEW` status.
- Open `/tasks/` as an assigned employee, another employee, manager, and administrator; verify protected visibility, overdue highlighting, sort order, and read-only details.
- Verify an approved/archived act cannot create duplicate tasks.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D23

- In TO analysis select two active employees, including employees from different departments; verify both stay selected after returning the act from OTK to TO. Try no employee or a duplicate employee; saving must be rejected.
- Approve the act and verify one task—not two—with both employees shown in the task list, detail page, and archived act.
- Open the shared task as each assigned employee, an unrelated employee, manager, and administrator; only assignees and full-access roles may view it.
- Complete it as one assignee. Verify `Выполнено`, the completing employee and date for both assignees and in the archived act; verify a second completion is unavailable/rejected.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D24

- Add employees from different departments to one corrective action, return the act from OTK to TO, and verify the employees and their displayed departments are preserved.
- Open `/tasks/` and verify exactly `№ задачи`, `Статус`, `Источник`, and `Срок`; task text and assignees must be absent from the table.
- Verify each task number and source act number are protected links; an unrelated employee must receive 404 at a direct task-detail URL.
- Verify `По акту`, overdue highlighting, and overdue-first/nearest-due-date ordering. Complete a shared task and confirm its technical status remains visible in details for all assignees.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D25

- Open `/tasks/` as an assignee, manager, and administrator. Verify `Мои задачи`, `Все задачи`, and `Архив` preserve backend visibility.
- Combine task number, source, `По акту`, and due-date filters; verify the URL retains all state and `Сбросить` retains the selected tab.
- Verify default active-task ordering is overdue first, then nearest date; check both explicit sorting options and no overdue highlight in `Архив`.
- Verify task numbers and source acts are linked and an unrelated employee cannot open a task by a direct URL.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D26

- Open a task from a filtered registry and verify the return button preserves the selected list URL.
- Verify status, source act, due date, assignees with departments, root cause, and task text on the card.
- Submit an empty or whitespace-only execution result and verify it is rejected without completing the task.
- Complete a shared task as one assignee; verify its result, executor/date, redirect to filtered `Архив`, and absence from active tabs. Verify an unassigned manager cannot complete it.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D27

- Open `/acts/` as OTK, KO, manager, and the dedicated administrator. Verify the topbar title `Акты`, the three registry tabs, KPI cards, and that introductory/role/administrator-mode text is absent.
- Verify search, status, act-type, and due-date filters combine correctly; `Сбросить` retains the selected tab. Confirm that only dates before today are overdue, while today and future dates are not overdue. Confirm there is no operation filter, while operation remains visible in the registry table and act forms/details.
- Verify `Создать АКТ` is visible only to roles already allowed to create acts. Verify `Очистить акты` appears only for `admin_user` and still requires confirmation; direct access remains denied for other users.
- Verify archived acts, direct act links, and OTK/KO/TO visibility remain unchanged.
- At `На анализе ТО`, the initial root analysis and corrective action never have delete buttons. After adding entries, only the last added root analysis and the last added corrective action have red `×` delete buttons beside their respective cause/action fields; adding another item moves the corresponding delete button to that new last item.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D21

- Open an `OTK_REVIEW` act as its OTK author and verify `Вернуть ТО` and `Утвердить` appear in the bottom action panel; verify they are unavailable to unauthorized users.
- Return the act with an empty and then valid comment; verify server rejection, comment/history, transition to `TO_ANALYSIS`, and prefilled TO structure.
- Approve an `OTK_REVIEW` act; verify `ARCHIVED`, approver/date, approval history, read-only workflow actions, and print/detail fields.
- Check `Мои акты`, `Все акты`, and `Архив` for OTK, TO, KO, manager, and administrator accounts; verify scopes do not reveal inaccessible acts.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D20

- Open a `TO_ANALYSIS` act: verify the initial root cause and action do not show delete buttons; add and remove items to verify buttons appear only when removable.
- Verify green add buttons and red delete buttons, including hover and keyboard-focus states.
- Return to KO with an empty or whitespace-only comment and verify rejection; submit a valid comment and verify the act moves to `KO_REVIEW` with comment and two history events.
- Submit an invalid analysis for OTK review and verify errors, unchanged status, and no saved partial structure.
- Submit a valid analysis with `На проверку ОТК`; verify `OTK_REVIEW`, saved data, legacy summaries, and the TO history event.
- Log in as the OTK author and verify the `OTK_REVIEW` act is visible in the OTK queue.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D19

- Open a `TO_ANALYSIS` act as a TO user and verify the editable `Анализ ТО` form appears directly on `Проработка`.
- Add and remove root analyses and corrective actions; verify the last root analysis and last action cannot be removed.
- Verify employee choices are filtered after selecting a department and submit mismatched department/user, blank text, and past-date values to confirm server-side errors and data preservation.
- Submit valid data with multiple roots/actions; verify the status becomes `OTK_REVIEW`, the first root/action populate legacy summaries, and the saved structure is read-only outside TO correction.
- Open an old act with only legacy TO values and verify its fallback display.
- Run `python manage.py makemigrations`, `python manage.py migrate`, `python manage.py test`, and `python manage.py check`.

### D18

- Open an act and verify comments are absent from `Проработка` and appear below attachments on `Вложения и комментарии`.
- Add a normal comment and verify the response redirects to `?tab=attachments`.
- As KO, attempt `Вернуть ОТК` with an empty or whitespace-only comment and verify the dialog shows an error without changing the act.
- Return the act with a valid comment; verify the comment, comment-added history event, return-to-OTK history event, and status change to `CREATED_OTK`.
- Log in as the OTK author and verify the returned act appears in the OTK queue.
- Run `python manage.py makemigrations`, `python manage.py migrate`, and `python manage.py check`.

### D17

- Open a `CREATED_OTK` act on a desktop viewport: verify no top-level `Дата обнаружения`, party-data labels remain on one line, and Defects/KO remain side by side.
- Verify each defect card has the approved five rows, with full-width defect type and description.
- Resize to a narrow viewport and verify the outer sections and defect-card fields stack without clipping.
- Verify an act without `ActDefect` rows still renders its legacy defect fallback.
- Run `python manage.py check`.

### D16

- Open a `CREATED_OTK` act and verify its number is in the top header and the work-tab order is party data, defects/KO decision, TO analysis, comments, then actions.
- Verify every defect card, including the legacy no-defect fallback, shows the required fields in order.
- Edit party data and defects; add and delete a defect, and verify D15 product, KD, quantity, and detected-date validation remains active.
- Verify only the OTK author, manager, or administrator can edit a `CREATED_OTK` act; verify later statuses cannot be edited.
- Transfer an edited act to KO and verify the edit action is unavailable.
- Run `python manage.py makemigrations`, `python manage.py migrate`, and `python manage.py check`.

### D15

- Open `/acts/create/` and submit valid Russian-only product and KD values, for example `Катушка-1` and `КД-12.3`.
- Verify today is prefilled for the initial defect date and for every row added with `Добавить дефект`; verify a past date can be selected.
- Verify future dates cannot be selected in the browser calendar and are rejected by a direct POST request.
- Verify `Product-1`, `КД/12`, and `Катушка А` show validation errors for the product/KD fields.
- Run `python manage.py check`.

- Log in as `ko_user` / `demo12345` and verify `Проработка`, `История акта`, and `Вложения`; verify invalid or missing `tab` opens `Проработка`.
- On `Вложения`, upload a permitted file, download it, and delete it only with an allowed user; submit an invalid file and verify the tab remains active.
- Submit each new KO decision on a separate `KO_REVIEW` act and verify every act moves to `TO_ANALYSIS`, leaves the KO queue, appears in the TO queue, and has KO and TO-transfer history events.
- Log in as `to_user` and verify each transferred act is available for the existing TO analysis workflow.
- Open an existing act with a legacy KO decision and verify its stored label still displays correctly.
- Run `python manage.py makemigrations`, `python manage.py migrate`, and `python manage.py check`.

- Log in as `ko_user` / `demo12345` and open an act in `KO_REVIEW`.
- Verify that `Проработка` opens by default, both tabs switch with `tab=work` and `tab=history`, and an invalid tab value opens `Проработка`.
- Verify the comments sidebar is visible beside both tabs and becomes a lower block on a narrow screen.
- On `Проработка`, verify the embedded KO form and its explanation of all three outcomes.
- Submit `Вернуть ОТК на уточнение`, `Пропустить`, and `Не пропускать` on separate acts; verify the resulting queue visibility and history event for each option.
- Submit an invalid KO form and verify validation errors remain in the embedded form.
- Open `/acts/<id>/ko-decision/` with GET and verify redirect to `?tab=work`; verify POST still saves the decision.
- Verify OTK, KO, TO, manager, and administrator visibility restrictions remain unchanged.
- Run `python manage.py check`.

- Log in as `otk_user` / `demo12345` and open an act in `CREATED_OTK` created by this user.
- Verify that customer, order number, ZNP number, party number, nomenclature, and operation are shown in `Данные партии`.
- Verify that the legacy summary defect fields are not duplicated in `Основные данные`.
- Verify every defect is shown with its type, description, and detection date; create an act with multiple defects and verify their numbering.
- Open an old act without `ActDefect` records and verify that its compatible defect fields are shown without an error.
- Verify `Передать в КО` is the primary OTK action, contains the queue warning, and the browser asks for confirmation.
- Cancel the confirmation and verify the act remains in `CREATED_OTK`.
- Confirm the transfer; verify the success message, redirect to `/acts/`, and absence of the act from the OTK queue.
- Log in as `ko_user` and verify the transferred act is visible in the KO queue.
- Run `python manage.py check`.

- Run `python manage.py seed_demo_accounts`.
- Log in as `admin_user` / `demo12345`.
- Verify `/acts/` shows all acts and the administrator-mode notice.
- Verify the administrator can open acts in `CREATED_OTK`, `KO_REVIEW`, `TO_ANALYSIS`, `ACTIONS_ASSIGNED`, and `CLOSED`.
- Verify current-stage action buttons are shown for the administrator.
- Verify the administrator can process `CREATED_OTK` to `KO_REVIEW`.
- Verify the administrator can process `KO_REVIEW` to `TO_ANALYSIS` or return it to `CREATED_OTK`.
- Verify the administrator can process `TO_ANALYSIS` to `ACTIONS_ASSIGNED`.
- Verify the administrator can close an `ACTIONS_ASSIGNED` act.
- Verify the administrator can download and delete attachments, and open the print view for any act.
- Verify normal OTK, KO, and TO users still have strict visibility.
- Open `/acts/create/`.
- Verify the `Данные партии` block layout.
- Verify `Заказчик` + `Номер заказа` are on one row.
- Verify `Номер ЗНП` + `Номер партии` are on one row.
- Verify `Номенклатура` + `Операция` are on one row.
- Verify the operation dropdown contains only `Операционный контроль` and `Выпускной контроль`.
- Verify the defect dropdown contains the required D11 defect list.
- Verify invalid order number characters are rejected.
- Verify invalid ZNP number characters are rejected.
- Verify invalid party number characters are rejected.
- Create an act with one defect.
- Create an act with two or more defects using `Добавить дефект`.
- Verify the created act opens correctly.
- Verify all defects appear on the detail page.
- Verify old act records still display without errors.

## Create and Activate a Virtual Environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If script execution is restricted in PowerShell, use:

```powershell
.\.venv\Scripts\python.exe --version
```

## Install Dependencies

```powershell
python -m pip install -r requirements.txt
```

## Database Configuration

SQLite remains the default backend and needs no environment variables; local
setup and testing are unchanged. `DATABASE_ENGINE` switches to PostgreSQL
(`ENGINE = django.db.backends.postgresql`, driver: `psycopg[binary]`) when
set to `postgresql`. Required in that mode: `DB_NAME`, `DB_USER`,
`DB_PASSWORD`. Optional, with defaults: `DB_HOST` (`127.0.0.1`), `DB_PORT`
(`5432`), `DB_CONN_MAX_AGE` (`0`), `DB_CONN_HEALTH_CHECKS` (`false`). A
missing required variable or an unsupported `DATABASE_ENGINE` value fails
`manage.py check`/startup with a clear `ImproperlyConfigured` error and never
falls back to SQLite silently. The PostgreSQL server, database, and role are
installed and created separately; this project does not provision them, and
existing SQLite data is not migrated by this configuration. See
[Preparing for PostgreSQL](docs/postgresql_preparation.md) for variables,
error behavior, and a PowerShell example. A template without real secrets is
in `.env.example`.

Two optional path overrides exist for transfer work only, and both keep their
current defaults when unset: `SQLITE_DB_PATH` points the SQLite backend at a
specific file (a stopped copy), and `MEDIA_ROOT_PATH` points `MEDIA_ROOT` at a
specific directory (a rehearsal target). Relative values resolve against
`BASE_DIR`.

## Data Transfer Tooling

The `maintenance` app provides the management commands that *prepare* a
SQLite → PostgreSQL transfer. They never move the live working database on
their own.

- `check_migration_source [--source-media-root <dir>] [--allow-default-database]`
  — read-only source preflight on SQLite: database file, separate copy (not the
  live `db.sqlite3`), backend, pending migrations, `PRAGMA integrity_check`,
  relational invariants, act-number uniqueness, lagging `ActNumberSequence`,
  attachment path safety, presence of every attachment file in the chosen
  source media, declared `file_size` vs. the real file, and the model / row /
  attachment inventory. Running it on the working `db.sqlite3` requires the
  explicit `--allow-default-database`.
- `export_migration_bundle --output <dir> [--source-media-root <dir>]` — builds
  a migration bundle (`manifest.json`, `data.json`, `media/`) with SHA-256
  checksums and per-model counts, max PKs and deterministic hashes. Run it
  against a **stopped copy** of `db.sqlite3` and a **copy of media**: point
  `SQLITE_DB_PATH` at the database copy and `--source-media-root` at the media
  copy. It refuses a non-empty output directory, a missing attachment file
  (unless `--allow-missing-media`) and any path escaping the chosen media root,
  and publishes the directory only after full success.
- `verify_migration_bundle --input <dir> --validate-only` — re-validates the
  bundle on its own: `source_vendor`, exact `model_order`, and per-model counts,
  max PKs and hashes **recomputed from `data.json`**, so the manifest can never
  vouch for itself.
- `prepare_empty_migration_target [--execute --confirm "<phrase>"]` — narrow,
  PostgreSQL-only cleanup of the rows the data migrations seed in
  `references.ActStatus` / `references.TaskStatus`. Dry-run by default, refuses
  outright if any user data is present, never uses `flush`.
- `check_migration_target [--previous-report <path>]` — target preflight:
  PostgreSQL backend, applied migrations, email disabled, empty transferable
  tables, empty `MEDIA_ROOT`, read/write access proved in a rolled-back
  transaction, required privileges, server version, and no unfinished previous
  import according to the rehearsal report.
- `import_migration_bundle --input <dir> [--dry-run] [--accept-missing-media]` —
  loads a bundle into an **empty** PostgreSQL database. Fixture load, sequence
  reset and `ActNumberSequence` sync run inside one `transaction.atomic()`, so a
  failure in any of them rolls every loaded row back; media is activated only
  after the commit. A partial media activation is reported as `partial` with an
  exact recovery procedure — never as an ordinary success. It never flushes and
  never deletes existing rows.
- `run_postgresql_smoke_checks [--read-only]` — post-import smoke checks on
  PostgreSQL: read-only traversal of the migrated data through the same
  querysets and permission helpers the views use, plus a full write round trip
  inside a transaction that is always rolled back. No email is sent.
- `verify_migration_bundle --input <dir> [--report <path>]` — compares row
  counts, max PKs and data hashes per model, checks every media file by path,
  size and SHA-256, validates `ActNumberSequence` and the key relational
  invariants, and exits non-zero on any difference.

A bundle whose `missing_media` is not empty is **incomplete**: an ordinary
import refuses it, `--accept-missing-media` additionally requires a typed
confirmation and prints the full list, and verification always counts it as a
difference unless the special `--allow-missing-media` mode is used.

Full step-by-step procedure, including how to prepare the target database and
what to do on failure: [Перенос данных из SQLite в PostgreSQL](docs/postgresql_migration.md).
A migration bundle contains real production data and is excluded from Git.

## PostgreSQL Rehearsal

`scripts/run_postgresql_rehearsal.py` runs the whole rehearsal locally, on
copies. Every stage is a separate `manage.py` subprocess, because SQLite and
PostgreSQL are selected by environment variables Django reads once at startup —
one process cannot legitimately be both.

Stages, strictly sequential; the first failure stops everything:

1. source preflight → 2. `export_migration_bundle` → 3. bundle re-validation →
4. target preflight → 5. `import_migration_bundle --dry-run` →
6. `import_migration_bundle` → 7. `verify_migration_bundle` →
8. `run_postgresql_smoke_checks` → 9. the final report.

A **separate copy** of `db.sqlite3` and a **separate copy** of `media` are
mandatory: stop the application, copy both, and rehearse against the copies.
PostgreSQL credentials are read **only** from the environment — the password is
never passed as a command-line argument and never written to a report.

```powershell
$env:DB_NAME = "quality_rehearsal"
$env:DB_USER = "quality_rehearsal"
$env:DB_PASSWORD = "<пароль из окружения>"

python scripts\run_postgresql_rehearsal.py `
  --source-db transfer\db-copy.sqlite3 `
  --source-media transfer\media-copy `
  --bundle transfer\bundle `
  --target-media transfer\target-media `
  --json-report transfer\rehearsal-report.json `
  --markdown-report transfer\rehearsal-report.md
```

Both reports are written on success *and* on failure. They carry the run time,
Git commit, OS, Python/Django/Psycopg/SQLite/PostgreSQL versions, source sizes,
model/row/attachment counts, per-stage durations, final hashes and counts, the
sequence-reset and `ActNumberSequence` results, relational verification, read
and write smoke results, missing media, warnings, the overall status, a minimum
downtime-window estimate and the issues blocking a production move. They never
contain passwords, `SECRET_KEY`, record contents, password hashes, attachment
contents or absolute server paths.

See [Репетиция переноса SQLite → PostgreSQL](docs/postgresql_rehearsal.md) for
the preparation steps, the manual checklist, the downtime estimate, the
rollback plan and the production-readiness criteria.

**The working system has not been switched to PostgreSQL.** It still runs on
SQLite; the production move is a separate, not-yet-performed stage.

## Real-time Event Foundation

The `realtime` app is the transport-independent foundation for future live UI
updates. It holds only the event contract, targets, the publisher abstraction
and its backends — no models, no migrations, no URLs, views or templates.

Business services in `acts`, `tasks` and `notifications` publish uniform events
explicitly, after their transaction commits, without knowing anything about
Redis, SSE or WebSocket. The initial event types are:

- `notification.created`, `notification.read`;
- `task.created`, `task.updated`, `task.completed`;
- `act.updated`, `act.status_changed`;
- `comment.created`.

An event carries only identifiers and safe technical metadata — never comment
text, defect descriptions, email addresses, file names or whole models.
PostgreSQL and the ordinary endpoints remain the source of truth; a client that
receives an event refetches through the normal, permission-checked views.

**Real-time is disabled by default.** `REALTIME_ENABLED` is `false` and
`REALTIME_PUBLISHER_BACKEND` points at `NoopRealtimePublisher`, which accepts
everything and sends nothing, so the project behaves exactly as it did before.
With real-time disabled the emitters return before resolving any recipient, so
no extra query runs, no commit callback is registered, and **no Redis server is
needed** — `manage.py check`, `runserver` and the test suite all work without
one. Publication is always registered through `transaction.on_commit()`, so a
rolled-back transaction never produces an event; a backend failure is logged to
the `realtime` logger and never breaks the already saved operation
(`REALTIME_FAIL_SILENTLY`).

### Transport: Redis publisher and personal SSE stream

`RedisRealtimePublisher` serializes an event once and publishes the same
payload into one Pub/Sub channel per target, named `<prefix>:<target.key>`
(for example `quality-ecosystem:realtime:user:7`). Redis is a short-lived
transport only: events are never stored, replayed or acknowledged, and having
no subscriber is a normal state rather than an error.

`GET /realtime/events/` is an async Server-Sent Events endpoint. The user is
taken **only** from the Django session: an anonymous request gets 401, a
disabled configuration gets 204 without touching Redis, an unreachable Redis
gets 503, and a successful request streams `text/event-stream`. A user is
subscribed to `user:<request.user.pk>` and nothing else — query string, path
and body cannot change that, and act channels are not subscribable in this
stage.

New environment variables:

- `REALTIME_REDIS_URL` — `redis://127.0.0.1:6379/0` by default;
- `REALTIME_CHANNEL_PREFIX` — `quality-ecosystem:realtime`;
- `REALTIME_REDIS_CONNECT_TIMEOUT_SECONDS`, `REALTIME_REDIS_SOCKET_TIMEOUT_SECONDS` — `5` each;
- `REALTIME_HEARTBEAT_SECONDS` — `25`, the silence after which the stream emits a keep-alive comment;
- `REALTIME_RECONNECT_DELAY_MS` — `3000`, advertised to the client in the initial `retry:` frame;
- `REALTIME_MAX_EVENT_BYTES` — `16384`, enforced before publishing and again before writing to the stream.

Running it locally requires a Redis server and an ASGI server:

```powershell
# 1. Redis (any local instance; a container is the simplest option)
docker run --rm -p 6379:6379 redis:7

# 2. Point the project at the Redis publisher
$env:REALTIME_ENABLED = "true"
$env:REALTIME_PUBLISHER_BACKEND = "realtime.backends.RedisRealtimePublisher"
$env:REALTIME_REDIS_URL = "redis://127.0.0.1:6379/0"

# 3. Verify the transport before anything else
python manage.py check_realtime_transport

# 4. Serve the async endpoint under ASGI (runserver is WSGI and cannot stream)
python -m uvicorn ecosystem.asgi:application --host 127.0.0.1 --port 8000
```

All three are required together: without a running Redis, `REALTIME_ENABLED=true`
*and* `REALTIME_PUBLISHER_BACKEND` pointing at `RedisRealtimePublisher`, the
bell simply keeps its previous behaviour and the page works exactly as before.

To check the live scenario locally: open the site in two browsers as two
different users, have the first user perform an action that notifies the second
(for example transfer an act to KO), and watch the second user's bell counter,
menu and toast update without a reload. Opening the bell marks the shown items
read, and a second tab of the same user updates on its own.

`check_realtime_transport` validates the settings, runs a Redis `PING`,
publishes a random token into a unique throwaway diagnostic channel, reads it
back, reports the round-trip time and releases every resource. It creates no
business objects, never touches a user channel and never prints credentials.

### Live bell and toasts

With real-time enabled, a new internal notification reaches the recipient
without a page reload: the bell counter updates, the notification appears in
the bell menu, and a single toast is shown in the bottom-right corner with the
notification's title, message and an `Открыть` link. Reading notifications
synchronises the counter and the menu across all of that user's open tabs.

The SSE event is only a *signal*. Every visible string and link is fetched
afterwards from `GET /notifications/header-fragment/`, an authenticated Django
endpoint that renders the same partial the full page uses — so permissions are
re-checked server-side and no event payload is ever inserted into the page. The
client refreshes on every connect and reconnect, so a dropped connection or a
Redis restart cannot leave a stale counter behind.

Toasts auto-dismiss after 8 seconds, pause while hovered or focused, close with
the button or `Escape`, cap at three visible at once, and respect
`prefers-reduced-motion`. The region is `aria-live="polite"`.

`static/js/realtime.js` is loaded only for an authenticated user and only while
`REALTIME_ENABLED=true`; with real-time off no configuration and no client
script are rendered, and no `EventSource` is created.

### Live tasks, registry and open act

The same single stream also keeps the working pages current, without a reload:

| Page | Updated live |
| --- | --- |
| `/tasks/` | the results table — a new task appears, a completed one leaves the active tabs and shows up in `Архив` |
| `/acts/` | the KPI cards and the results table — a status change moves an act between scopes |
| `/acts/<id>/` | the number, status badge and route; the history feed; the comment list; the related activities table |

Every update is a refetch of a server-rendered fragment through an ordinary
authenticated endpoint, so tab, filters, search, sorting and permissions are
re-evaluated by Django — the browser decides nothing. Scroll position and focus
in the filter form are preserved, because the tabs and the filter panel are
never replaced.

**Unsaved input is never touched.** The KO decision form, the TO analysis form
with its dynamic rows, the new-comment textarea, the return dialogs and the
attachment form all sit outside the replaceable blocks. If somebody else
changes the act while your form has unsaved edits, a banner appears —
*«Акт изменён другим пользователем»* — with a reload button; your text stays
exactly where it is so you can copy it. On a status change the workflow submit
buttons are disabled as well, since they would act on a status that no longer
exists; ordinary fields stay editable. If the act becomes invisible to you, the
page stops updating and offers a link back to the registry.

Only new notifications produce a toast. Task and act updates are silent — you
already get a toast through the matching notification when the workflow defines
one.

Local check with two users: run Redis and uvicorn as above, open `/tasks/` as a
TO user in one browser, and from another account approve an act with corrective
actions. The task appears without F5; completing it moves it to `Архив`. Then
open an act in one browser, change its status from the other, and watch the
badge and route update while any text you have typed survives.

**Still not implemented:** fallback polling, `BroadcastChannel`, WebSocket, act
channel subscriptions and a `/realtime/sync/` endpoint. History, comments and
activities refresh only while their tab is open — switching tabs is an ordinary
server render and therefore already current.

See [Real-time события](docs/realtime.md) for the contract, targets, channel
namespace, SSE frame format, heartbeat, cleanup rules, size limits and the RT-3
plan.

## Concurrent Work and Act Numbering

Act numbers are issued from `ActNumberSequence`, a technical table holding one
counter row per year. `Act.save()` opens a transaction, locks that row with
`select_for_update()`, increments it, and inserts the act — so two simultaneous
creations cannot receive the same `АОК-YYYY-NNN` value. An explicitly supplied
number is always kept as-is, the public number format is unchanged, and the
unique constraint on `Act.number` remains the final safety net. Only the
administrator full cleanup resets the counters; deleting one act never rewinds
them.

Every critical workflow transition — transfer to KO, KO decision, all three
returns, TO analysis, approval, and closing — plus the POST branch of act
editing re-loads and row-locks the act inside one transaction and re-checks the
user's permission and the act's current status before writing. A second,
parallel or outdated request is refused with a controlled error instead of
duplicating history events, return comments, tasks, assignees, or
notifications.

SQLite remains the default backend for local development, but it does **not**
implement `select_for_update()`, so genuine row-level locking is provided only
by PostgreSQL. The dedicated concurrency tests are therefore skipped on SQLite
and run for real in the PostgreSQL CI job. Migrating existing working data to
PostgreSQL and production deployment are still not done.

## Setup Local Data

```powershell
python manage.py migrate
python manage.py seed_demo_accounts
python manage.py seed_references
python manage.py seed_demo_acts
```

Demo accounts for local development only:

- `otk_user` / `demo12345`
- `ko_user` / `demo12345`
- `to_user` / `demo12345`
- `manager_user` / `demo12345`
- `admin_user` / `demo12345`

These demo passwords must not be used for production or shared environments.

## Validation

```powershell
python manage.py check
python manage.py test notifications
python manage.py test
```

## Continuous Integration

`.github/workflows/database-compatibility.yml` runs on every push to `main`
and every pull request, with two independent jobs on `ubuntu-latest` /
Python 3.13:

- **SQLite** — no PostgreSQL variables; `check`, `makemigrations --check
  --dry-run`, `migrate --noinput`, then the full test suite.
- **PostgreSQL** — a disposable PostgreSQL 17 service container
  (`quality_ecosystem_ci` / `quality_ci`, CI-only demo password), gated on a
  `pg_isready` health check; the job first confirms Django actually connected
  with the `postgresql` backend, then runs the same `check` /
  `makemigrations --check` / `migrate` / test sequence, plus
  `showmigrations`.

This CI PostgreSQL container is not a production deployment and does not
migrate or persist any real data — it exists only for the duration of the
workflow run. See [Preparing for PostgreSQL](docs/postgresql_preparation.md)
for details and how to reproduce the PostgreSQL check locally.

## Start the Local Server

```powershell
python manage.py runserver
```

Open http://127.0.0.1:8000/ in a browser.

## Intentionally Not Implemented Yet

- Further full-width act-card redesign and acceptance work (deferred).
- Protocols.
- Nonconformities.
- Reports.
- Word/PDF export.
- PostgreSQL production deployment and migrating existing SQLite data to it (switchable configuration, transfer tooling and a full local rehearsal are prepared; see [Preparing for PostgreSQL](docs/postgresql_preparation.md) and [Репетиция переноса](docs/postgresql_rehearsal.md)).
- Reverse migration PostgreSQL → SQLite, delta synchronisation, and any transfer that does not stop writes.
- Changing the working application address, reverse proxy, HTTPS, production WSGI/ASGI, and permanent backups.
- REST API.
- Live updating of tasks, acts, history and comments; act-channel subscriptions; fallback polling; `BroadcastChannel` and leader-tab election; WebSocket/Django Channels; React; event replay or storage; a production ASGI deployment with reverse proxy and HTTPS. Live notifications (bell and toasts) are implemented — see [Real-time события](docs/realtime.md).
- Frontend frameworks.
- Celery, Redis, APScheduler, or an in-process WSGI/ASGI scheduler.

## Next Planned Stage

- Obtain SMTP parameters, select Linux or Windows Server, install the prepared scheduler configuration, and validate the email channel against the corporate SMTP/Exchange service.
- Resume the deferred full-width act-card redesign only after a separate approved specification.
