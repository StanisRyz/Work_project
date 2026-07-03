# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D4 references foundation.

D4 adds the first managed reference dictionaries:

- `Operation`
- `DefectType`
- `ActStatus`
- `TaskStatus`
- `Priority`

References are managed through Django Admin for now. Custom reference CRUD
screens are intentionally not part of this stage.

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

## Run Migrations

```powershell
python manage.py migrate
```

## Seed Demo Accounts

```powershell
python manage.py seed_demo_accounts
```

Demo accounts for local development only:

- `otk_user` / `demo12345`
- `ko_user` / `demo12345`
- `to_user` / `demo12345`
- `manager_user` / `demo12345`
- `admin_user` / `demo12345`

These demo passwords must not be used for production or shared environments.

## Seed References

```powershell
python manage.py seed_references
```

The command is idempotent and safe to run multiple times.

## Start the Local Server

```powershell
python manage.py runserver
```

Open http://127.0.0.1:8000/ in a browser.

## Intentionally Not Implemented Yet

- Acts model or act workflows.
- Tasks model or task workflows.
- Protocols.
- Nonconformities.
- Reports.
- Custom reference CRUD outside Django Admin.
- Role-based object filtering or access-control matrix.
- PostgreSQL configuration.
- REST API or realtime features.
- Frontend frameworks.

## Next Planned Stages

- D5 acts module.
