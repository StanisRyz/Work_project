# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D5 acts module.

D5 adds the first MVP business module:

- `Act` model for acts of operational control.
- Act list, create, and detail pages.
- Simple route: ОТК -> КО -> ТО -> мероприятия.
- MVP role-based act visibility through `UserProfile.role`.
- Demo acts seed command for local validation.

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
- File attachments.
- Word/PDF export.
- PostgreSQL configuration.
- REST API or realtime features.
- Frontend frameworks.

## Next Planned Stages

- D6 tasks for act corrective actions.
