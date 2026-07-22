# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D16 — improved CREATED_OTK act detail page and controlled editing before transfer to KO.

D16 moves the act number to the top header, keeps work data in the prescribed sequence, and allows authorized users to edit party data and defects only while an act remains in `CREATED_OTK`.

D15 — validation for product fields and detected dates in the act creation form.

D15 preserves the existing act creation workflow and adds the following protections:

- `Наименование продукции` and `Обозначение по КД` accept only Russian letters, digits, dots, and hyphens; Django server-side validation remains authoritative.
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

- Task objects or corrective action tasks.
- Protocols.
- Nonconformities.
- Reports.
- Word/PDF export.
- PostgreSQL configuration.
- REST API or realtime features.
- Frontend frameworks.

## Next Planned Stage

- To be defined after D16 manual validation.
