# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D2 apps and base layout structure.

D2 adds a modular Django skeleton for the future internal system:

- Core apps: `dashboard`, `accounts`, `references`, `acts`, and `tasks`.
- URL structure for the main sections.
- Base layout with sidebar, header, and content area.
- Server-rendered sidebar navigation.
- Placeholder pages for future modules.

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

- Business models.
- Roles, permissions, departments, or authentication customization.
- Act, task, protocol, nonconformity, or report workflows.
- Act/task CRUD screens or forms.
- References data.
- PostgreSQL configuration.
- REST API.
- WebSocket, Channels, realtime updates, or polling.
- Frontend frameworks.

## Next Planned Stages

- D3 users, roles, departments.
- D4 references.
- D5 acts module.
