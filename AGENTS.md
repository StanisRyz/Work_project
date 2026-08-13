# Agent Notes

Rules for changing this codebase. Every statement here is backed by current
code. Product and operational detail lives in `docs/` — do not duplicate it.

## Project snapshot

Quality-management system for production acts (АОК). An act travels
ОТК → КО → ТО → ОТК; approving it creates shared tasks for every corrective
action. Django monolith, server-rendered templates plus vanilla JavaScript,
ASGI (Uvicorn), PostgreSQL in production and SQLite in development, Redis
Pub/Sub → SSE for live updates, corporate SMTP relay for email. Russian UI
labels by default; new major business areas become separate Django apps.
Standard Django `User` plus `accounts.UserProfile` for roles — no custom user
model without explicit approval.

## App ownership

| App | Owns |
| --- | --- |
| `ecosystem` | settings, URLconf, ASGI/WSGI, deployment checks, health, logging, middleware. No models |
| `accounts` | `Department`, `UserProfile` (role, department), login, landing target (`accounts/navigation.py`) |
| `references` | operations, defect types, act/task statuses, priorities; `seed_references` |
| `acts` | acts, defects, root analyses, corrective actions, history, comments, attachments, workflow, permissions |
| `tasks` | tasks created on approval, their assignees and completion |
| `notifications` | in-app notifications, routing, deduplication, email delivery queue |
| `realtime` | event contract, targets, channels, publisher, SSE endpoint, sync revisions. No models, no migrations |
| `maintenance` | technical read-only commands and transfer tooling. No models, no migrations |
| `dashboard` | administrator landing page; redirects everyone else to `/acts/` |

Reference data belongs in `references`, never as free text on a business model;
tasks never live inside `acts`.

## Non-negotiable architecture

- **PostgreSQL is the source of truth.** Redis is a best-effort transport:
  events are not stored, replayed or acknowledged, and a lost message means a
  delayed UI update, never lost data.
- **Workflow changes only through services.** `acts/services.py` and
  `tasks/services.py` are the single place state moves; views parse the request,
  call permissions and services, and render. Templates decide nothing.
- **Permissions are backend-enforced.** `acts/permissions.py` and
  `tasks/permissions.py` are the only definition of who sees and may do what;
  `realtime` reuses them instead of restating visibility.
- **Events are published explicitly after commit**, from services, never from
  `post_save`. A signal would fire on fixture loads, on technical saves, and
  before dependent objects exist.
- **SSE payload is never a source of HTML or of permissions.** An event says
  what changed; the client refetches through ordinary permission-checked
  endpoints. A full page and its fragment go through the same state builder and
  the same partials.
- **Attachments are protected media**, served only by
  `acts.views.act_download_attachment` with a per-request permission check.
  `MEDIA_ROOT` is never published by the web server and is a different
  directory from `STATIC_ROOT`.
- Django Templates and vanilla JavaScript only: no framework, bundler or npm.
- **A live-replaced fragment never owns a listener bound at page load.**
  `[data-live-act-work]` is swapped wholesale, so its markup is wired by a
  delegated `document` listener or a `window.qualityFragments` initialiser; a
  one-shot `querySelector(…).addEventListener` dies on the first refresh.
- One confirmation modal, no browser dialogs: `includes/confirm_modal.html` with
  `static/js/confirm_modal.js`, driven by `data-confirm-*` on the trigger —
  `-url` posts the modal's own CSRF-protected form, `-form` submits an existing
  page form, `-comment="required"` adds the mandatory comment.
- One button system: `.link-button` fixes font, size, height, padding, radius and
  states; a modifier (`--secondary`, `--warning`, `--danger`, `--success`,
  `--compact`) changes only colour or density.
- `accounts.navigation.get_default_landing_url()` is the one answer to where a
  user belongs: dashboard for an administrator, `/acts/` for everyone else.

## Domain invariants

- Every authenticated user may read every act: `all` contains all non-archived
  acts, `archive` contains all archived acts, and their detail pages are
  read-only outside the user's working scope. `my` is the working queue: ОТК
  gets own `CREATED_OTK`/`OTK_REVIEW` acts, КО gets `KO_REVIEW`, ТО gets
  `TO_ANALYSIS` plus own `ACTIONS_ASSIGNED`; managers and administrators get
  all active acts. Global read access never grants comments, uploads, workflow
  actions or editing; editing remains limited to authorised `CREATED_OTK` acts.
- Every return transition requires a non-whitespace comment saved atomically
  with it, and must not emit a duplicate ordinary-comment notification. With
  several defects, КО must decide on **every** defect before the act may leave
  `KO_REVIEW`; legacy decision values stay readable and must not be rewritten by
  a data migration.
- Every defect requires a workshop/supplier choice on the form, while the model
  field stays `blank=True` so existing rows keep no invented value. Revealing
  the remaining fields must never clear already-entered values.
- Structured TO analysis is atomic and read-only after submission; each
  corrective action needs text, a department, a due date and at least one active
  assignee. Approval revalidates it all under lock and creates exactly one
  `tasks.Task` per corrective action.
- A shared task is completed **once** by any assignee and requires a
  non-whitespace execution result; assignee changes go only through
  `tasks.services.replace_task_assignees()`. Every authenticated user may read
  every task through `all`, `archive` and task detail; only active assigned
  tasks appear in `my`, and read access never grants completion rights.
- `ActNumberSequence` is the single source of automatic `АОК-YYYY-NNN`
  numbering: one row per year, locked while a number is issued. An explicit
  `Act.number` is preserved; only the administrator full cleanup resets it.
- `ActHistoryEvent` is the business audit trail, append-only from the normal UI.
  Comments are manual notes and never replace history. Do not add an `AuditLog`
  model — logs are diagnostics and may be rotated away.
- Notifications are created in the same transaction as their business event,
  deduplicated per recipient by a stable source key, and routed in one place:
  `notifications/services.py`.

## Security and permissions

- Never rely on a template check, a username, or a URL not being guessed: the
  `/acts/clear-all/` route is not registered unless `ENABLE_DEMO_RESET` is on,
  and production forces it off. Notification pages and POST actions always
  scope objects to `request.user`.
- The SSE endpoint and `/realtime/sync/` derive identity from the session only:
  no query string, path or body may influence the subscription. Technical
  endpoints use `realtime.auth.realtime_login_required` (a JSON 401), not the
  HTML `login_required` redirect.
- Secrets — `SECRET_KEY`, `DB_PASSWORD`, `EMAIL_HOST_PASSWORD`, the Redis URL —
  come only from the environment and never appear in an error message, a check
  message, the browser config or a log line.
- Upload validation checks size and extension; attachment deletion is limited to
  the uploader, a manager or an administrator.
- An inactive `UserProfile` grants no application role; only Django's genuine
  `is_superuser` fallback remains independent of the profile.
- Business models are read-only diagnostics in Django Admin. Workflow state is
  changed only through application services.
- Browser tab coordination is isolated by an opaque per-session epoch. That
  client-visible identifier coordinates tabs only and never authorizes server access.

## Transactions and locking

- Every critical act transition — and the POST branch of act editing — opens
  `transaction.atomic()`, re-loads and row-locks the act via
  `lock_act_for_update()`, and only then re-checks permission and status. Always
  act on the returned locked instance; call sites must reassign it.
- Lock order is fixed everywhere: act → its defects / root analyses /
  corrective actions → tasks → history and notification records. Lock queries
  avoid `select_related()` so a joined `SELECT … FOR UPDATE` does not lock
  shared reference rows.
- A second parallel or outdated request fails with a controlled
  `ActWorkflowError` / `TaskWorkflowError` — never an unhandled `IntegrityError`
  or a 500 — and creates no duplicate history events, tasks, assignees,
  notifications or deliveries.
- Any service changing a saved `Task`'s visible state must list `updated_at` in
  `save(update_fields=[…])`: Django skips an `auto_now` field that is not listed,
  leaving the sync revision derived from it stale. Fix the explicit call, never
  paper over it with a signal.
- Attachment rows and history change atomically in act → attachment lock order;
  duplicate deletion is idempotent, while file cleanup remains explicit and
  best-effort because storage is not transactional with PostgreSQL.

## Redis and SSE rules

- Business code must not import a Redis client: only `realtime/backends.py`,
  `realtime/transport.py`, `realtime/sse.py` and the diagnostic command may, and
  they import it lazily so `REALTIME_ENABLED=false` needs no Redis at all.
- Publication always goes through `publish_after_commit()`; a transport failure
  happens after commit and must never break a saved business operation.
- New event types go only into `RealtimeEventType`, with stable string values,
  and need contract, target and integration tests. Do not add one type per act
  status — transitions use `act.status_changed`. Payloads carry identifiers and
  safe metadata only: never user text, email addresses, file names, permissions
  or whole models.
- Targets are computed server-side from the existing notification routing and
  never travel in a payload. Channel names come only from a validated
  `RealtimeTarget`, and Redis publishes only to kinds a client can subscribe to —
  today `user:<id>`; `act:<id>` needs the authorised subscription first.
- A live refresh never replaces a form holding unsaved input: only read-only
  blocks are swapped, and a dirty form gets the conflict banner with the typed
  text intact. Recovery has one owner per authenticated session — every periodic request is gated
  on the leader tab when tabs are coordinated.

## Logging

- Log identifiers, status codes, counts, `duration_ms` and `outcome` through
  `ecosystem.logging_utils.log_event()` — never user text, customer/party data,
  usernames, email addresses, message subjects or bodies, attachment names or
  paths, serialized events, Redis channel names, or any secret.
- Keep volume bounded: no heartbeat, no fast successful sync, no fragment fetch
  at INFO; repeating successes stay on DEBUG or are aggregated. Standard Python
  `logging` only, no third-party logging framework.

## Change workflow

- Keep changes small and reversible; do not add backend complexity before it is
  needed. Seed commands stay idempotent; demo accounts require
  `APP_ENV=development` and explicit confirmation.
- Production startup must pass `manage.py check` and
  `manage.py check_production_readiness` before Uvicorn starts.
- **Never edit an existing migration.** Add a new one, applying cleanly from
  zero on both SQLite and PostgreSQL.
- New reference values go into `seed_references` in the same change, so a fresh
  installation gets them; `check_fresh_bootstrap` reads required act statuses
  from `acts.models.ACT_STATUS_CODES` — extend the constant, not a second list.
- Preserve existing workflow and permission tests when behaviour changes; the
  PostgreSQL-only concurrency tests must never be weakened to get a green run.
- React, WebSocket/Django Channels, Celery, a bundler, event storage or replay,
  and any other major component are added **only by a separate architectural
  decision**.
- Update the matching `docs/` document in the same change as the behaviour it
  describes. Documentation states the current state; stage history belongs in
  Git, and stage markers are allowed only under `docs/archive/`.

## Validation commands

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python manage.py check_documentation
```

For deployment-facing changes also run the read-only `check_logging`,
`check_realtime_transport`, `check_fresh_bootstrap` and
`check_production_readiness`.

## Documentation map

| Need | Read |
| --- | --- |
| Roles, visibility, statuses, tasks, notifications | [docs/domain.md](docs/domain.md) |
| Layers, dependencies, sources of truth | [docs/architecture.md](docs/architecture.md) |
| Event contract, SSE, sync, tabs | [docs/realtime.md](docs/realtime.md) |
| Local setup, tests, diagnostics | [docs/development.md](docs/development.md) |
| Production, PostgreSQL bootstrap, proxy, Redis, SMTP | [docs/deployment.md](docs/deployment.md) |
| Logging, incidents, health, email worker | [docs/operations.md](docs/operations.md) |
| Backup and restore | [docs/backup_restore.md](docs/backup_restore.md) |
| Full map, including the archive | [docs/index.md](docs/index.md) |

Exact dependency versions live in `requirements.txt`; the environment variable
list lives in `.env.example` and `ecosystem/settings.py`.
