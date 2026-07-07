# Единая цифровая экосистема управления качеством

## Purpose

Internal web application foundation for the future quality management ecosystem.
The first product direction is a quality ecosystem. Digital OTK and micro-MES
are planned as future stages, not part of the current implementation.

## Current Stage

D7 act UI improvements and strict in-progress visibility.

D7 improves the existing acts module without adding new business modules:

- `Act` remains the MVP model for acts of operational control.
- Act list, create, detail, KO decision, and TO analysis routes stay stable.
- Workflow transitions are centralized in `acts/services.py`.
- Role and action checks are centralized in `acts/permissions.py`.
- Backend visibility is strict for regular processing roles:
  - ОТК sees only own `CREATED_OTK` acts.
  - КО sees only `KO_REVIEW` acts.
  - ТО sees only `TO_ANALYSIS` acts.
  - Руководитель and Администратор see all acts.
- `ACTIONS_ASSIGNED`, `CLOSED`, and `CANCELLED` are visible only to manager/admin until task and closing ownership is implemented.
- Workflow actions redirect regular users back to the list when an act leaves their visible stage.
- The act list includes visible-act KPI cards, filters, reset action, status/priority badges, overdue highlighting, and empty states.
- The detail page includes a clearer header, badges, route visualization, grouped sections, placeholders, and service-provided available actions.
- Create, KO decision, and TO analysis forms are grouped and show act context where needed.
- The simple route remains: ОТК -> КО -> ТО -> мероприятия.
- Workflow logic uses `ActStatus.code`, not Russian status names.
- Tests cover the main ОТК -> КО -> ТО route, blocked roles, strict visibility, redirects, view actions, and KPI counters.

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
python manage.py test acts
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
- File attachments.
- Word/PDF export.
- PostgreSQL configuration.
- REST API or realtime features.
- Frontend frameworks.

## Next Planned Stages

- D8 act history and comments.
- D9 act attachments.
- D10 act closing and print view.
