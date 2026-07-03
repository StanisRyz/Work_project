# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D1 base Django setup.

This stage prepares the clean Django foundation: project settings, templates,
static files, a minimal home page, local SQLite database settings, documentation,
and validation commands.

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

## Start the Local Server

```powershell
python manage.py runserver
```

Open http://127.0.0.1:8000/ in a browser.

## Intentionally Not Implemented Yet

- Business apps for acts, tasks, protocols, nonconformities, and reports.
- User, role, department, and permission customization.
- PostgreSQL configuration.
- Docker or production deployment configuration.
- REST API, WebSocket, Channels, or realtime updates.
- Frontend frameworks.
- Real navigation pages beyond placeholders.

## Next Planned Stages

- D2 apps and base layout structure.
- D3 users, roles, departments.
- D4 references.
- D5 acts module.
