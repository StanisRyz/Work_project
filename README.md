# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D9 act attachments.

D9 adds protected file attachments to the existing acts module without adding new business modules:

- `Act` remains the MVP model for acts of operational control.
- `ActHistoryEvent` stores append-only history events for act creation, workflow transitions, comments, and attachment actions.
- `ActComment` stores manual user notes on an act.
- `ActAttachment` stores files uploaded to acts under `MEDIA_ROOT/acts/attachments/<act_id>/`.
- Allowed attachment types: `.pdf`, `.doc`, `.docx`, `.xls`, `.xlsx`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.txt`.
- Maximum attachment size: 10 MB.
- Attachment downloads go through access-checked Django views, not direct media links.
- Users who can view an act can upload and download its attachments.
- Attachment deletion is limited to the attachment author, manager, or admin, and still requires act visibility.
- Attachment upload and delete actions record history events when D8 history models are available.
- Act list, create, detail, KO decision, TO analysis, add-comment, and attachment routes stay server-rendered.
- Workflow transitions are centralized in `acts/services.py`.
- Role and action checks are centralized in `acts/permissions.py`.
- Backend visibility remains strict for regular processing roles:
  - ОТК sees only own `CREATED_OTK` acts.
  - КО sees only `KO_REVIEW` acts.
  - ТО sees only `TO_ANALYSIS` acts.
  - Руководитель and Администратор see all acts.
- `ACTIONS_ASSIGNED`, `CLOSED`, and `CANCELLED` are visible only to manager/admin until task and closing ownership is implemented.
- The act detail page shows history, comments, attachments, status transitions, authors, timestamps, and service-provided available actions.
- The simple route remains: ОТК -> КО -> ТО -> мероприятия.
- Workflow logic uses `ActStatus.code`, not Russian status names.

## Manual Validation Checklist

- Upload an allowed file to a visible act.
- Verify the attachment appears on the act detail page.
- Download the attachment through the act detail page.
- Try an unsupported extension and verify the validation error.
- Try a file larger than 10 MB and verify the validation error.
- Delete your own attachment.
- Verify a regular user cannot delete another user’s attachment.
- Verify manager/admin can delete attachments.
- Verify a hidden act attachment URL cannot be downloaded by a user without act access.
- Verify `ATTACHMENT_ADDED` and `ATTACHMENT_DELETED` history events appear.

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

## Next Planned Stages

- D10 act closing and print view.
