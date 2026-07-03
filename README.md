# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D3 users, roles, departments.

D3 adds the first real user foundation for the future internal system:

- `Department` for internal departments.
- `UserProfile` linked one-to-one with the standard Django `User`.
- Role awareness for ОТК, КО, ТО, Руководитель, and Администратор.
- Login and logout with standard Django authentication views.
- Demo seed command for local development accounts.

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
- References data.
- Role-based object filtering or access-control matrix.
- PostgreSQL configuration.
- REST API or realtime features.
- Frontend frameworks.

## Next Planned Stages

- D4 references.
- D5 acts module.
