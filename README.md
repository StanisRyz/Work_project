# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D10 act closing and print view.

D10 completes the MVP lifecycle for the existing acts module without adding new business modules:

- `Act` remains the MVP model for acts of operational control.
- Act closing fields are stored on `Act`: `closed_by`, `closed_at`, and `closing_comment`.
- `ActHistoryEvent` stores append-only history events for act creation, workflow transitions, comments, attachments, and `ACT_CLOSED`.
- `ActComment` stores manual user notes on an act.
- `ActAttachment` stores protected files uploaded to acts under `MEDIA_ROOT/acts/attachments/<act_id>/`.
- Closing is allowed only for `ACTIONS_ASSIGNED` acts that completed the MVP route.
- Closing validation requires KO decision data and TO analysis data to be filled before the act can move to `CLOSED`.
- Closing permissions:
  - Руководитель and Администратор can close any `ACTIONS_ASSIGNED` act.
  - ТО can close only `ACTIONS_ASSIGNED` acts where that user performed the TO analysis.
  - ОТК, КО, and users without a profile cannot close acts.
- Closed acts are visible only to Руководитель and Администратор in the normal workflow.
- The print view is an HTML/browser-print page only; no PDF or Word export is generated.
- Attachment downloads go through access-checked Django views, not direct media links.
- Role and action checks are centralized in `acts/permissions.py`.
- Workflow transitions and closing logic are centralized in `acts/services.py`.
- The simple route is now complete: ОТК -> КО -> ТО -> мероприятия -> закрытие.
- Workflow logic uses `ActStatus.code`, not Russian status names.

## Manual Validation Checklist

- Create or open an act that reached `ACTIONS_ASSIGNED`.
- Verify the ТО user who performed analysis can close the act.
- Verify an unrelated ТО user cannot close the act.
- Verify manager/admin can close the act.
- Verify ОТК/КО cannot close the act.
- Verify an act cannot close before KO decision data is filled.
- Verify an act cannot close before TO analysis data is filled.
- Verify `closed_by`, `closed_at`, and `closing_comment` are saved.
- Verify `ACT_CLOSED` appears in act history.
- Verify a closed act shows the closed block.
- Verify the print view opens.
- Verify the print view contains main act data, KO, TO, attachments, history, and closing data.
- Verify print CSS hides navigation and action buttons when printing.

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

- D11 act module review and stabilization.
