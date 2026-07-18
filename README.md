# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D11 act create form redesign for OTK.

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

- D12 OTK act detail review and send-to-KO polish.
