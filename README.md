# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D25 — working task registry.

D25 adds `Мои задачи`, `Все задачи`, and `Архив` tabs to `/tasks/`. Filters for task number, source act, registry status, and due-date state, plus due-date sorting, are kept in the URL and combine with AND logic. `Сбросить` retains the selected tab. Active tasks keep overdue-first default ordering; explicit sorting overrides it, while completed archive tasks are never marked overdue. Existing task visibility remains authoritative for every tab.

D24 — compact task registry and cross-department assignees.

D24 keeps the corrective action department as the department responsible for the action, while its active assignees may belong to different departments. The TO form selects employees directly and shows each employee's actual department; no temporary per-assignee department data is used, so assignments are preserved when OTK returns an act to TO.

The `/tasks/` registry now has only `№ задачи`, `Статус`, `Источник`, and `Срок`. The task primary key is the clickable number, the registry status is always `По акту`, and the source act is linked. Overdue tasks remain first and visibly marked; technical `NEW` and `COMPLETED` statuses remain in task execution and detail pages.

D23 — shared corrective-action tasks with multiple assignees.

D23 replaces the single responsible employee with `ActCorrectiveActionAssignee` and `TaskAssignee`. Every corrective action has one or more unique active employees, including employees from other departments. OTK approval creates exactly one shared `Task` per action and creates all its assignee records in the same transaction as approval and archival. Existing single responsible users are copied into the new relations by migrations.

An ordinary employee can view and complete a task only when assigned to it; managers and administrators retain full visibility. Completion changes the single shared task to `COMPLETED` (`Выполнена`) atomically, records who completed it and when, and is immediately visible to every assignee. A completed task cannot be completed again. Archived acts remain read-only and show assignees, linked tasks, and completion metadata.

D22 — tasks from approved corrective actions.

D22 introduced one executable task for every corrective action during the atomic OTK approval transaction. D23 later made these tasks shared by multiple assignees.

D21 — OTK review, approval, and registry scopes.

D21 lets the authorized OTK reviewer return an `OTK_REVIEW` act to TO with a mandatory comment or approve it to terminal `ARCHIVED`, recording the approver and date. D22 extends approval by creating linked executable tasks. The registry has `Мои акты`, `Все акты`, and `Архив` scopes that preserve server-side visibility rules.

D20 — TO analysis is routed to OTK review.

D20 adds the `OTK_REVIEW` (`Проверка ОТК`) stage. From `TO_ANALYSIS`, an authorized user may return the act to KO with a mandatory comment, or submit a fully validated structured analysis for OTK review. The initial analysis structure does not render delete controls; add controls are green and delete controls are red.

D19 — structured TO analysis is embedded on the act detail page.

D19 replaces the separate TO analysis page with a `Корневая проработка` form on `Проработка`. Each root cause contains one or more corrective actions with department, assignees, and due date. The subsequent D20 workflow sends successful analysis to `OTK_REVIEW`; legacy summaries remain compatible and the submitted structure is read-only outside TO correction.

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
- Complete it as one assignee. Verify `Выполнена`, the completing employee and date for both assignees and in the archived act; verify a second completion is unavailable/rejected.
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
```

## Start the Local Server

```powershell
python manage.py runserver
```

Open http://127.0.0.1:8000/ in a browser.

## Intentionally Not Implemented Yet

- Protocols.
- Nonconformities.
- Reports.
- Word/PDF export.
- PostgreSQL configuration.
- REST API or realtime features.
- Frontend frameworks.

## Next Planned Stage

- D26 — protocols and follow-up control, to be defined after D25 manual validation.
