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
| `ecosystem` | settings, URLconf, ASGI/WSGI, deployment checks, health, logging, middleware, working-day arithmetic (`workdays.py`). No models |
| `accounts` | `Department`, `UserProfile` (role, department), login, landing target (`accounts/navigation.py`). No user-facing pages beyond login/logout — user/department management is Django Admin only |
| `references` | operations, defect types, act/task statuses, priorities; `seed_references`. No user-facing pages — reference management is Django Admin only |
| `acts` | acts, defects, root analyses, corrective actions, history, comments, attachments, workflow, permissions |
| `tasks` | tasks created by act and protocol workflows, their assignees, completion and optional attachments |
| `protocols` | meeting protocols: `ProtocolType`, `Protocol`, participants, agenda, «Слушали», `ProtocolAction`, `ProtocolApproval`, history; the pages under `/quality/protocols/`; numbering, the approval workflow and every other mutation in `protocols/services.py`. Independent from `acts` |
| `calculator` | winding-time calculator and the shared «Проработка» journal: `WindingEntry`, the JSON endpoints under `/calculators/winding/`, the `.xlsx` export and `import_calculator_json` |
| `plate_cutting` | Калькулятор рубки пластин: the page at `/calculators/plate-cutting/`, the agreed coefficients in `plate_cutting/constants.py`, and the saved package sets (`PlateCuttingPreset`, `PlateCuttingPresetPackage`) written only through `plate_cutting/services.py` |
| `documents` | the documentation library at `/documents/`: `DocumentFolder` (self-referencing tree), `Document` + `DocumentVersion` + `DocumentHistoryEvent` + `DocumentFavorite` (corporate documents, files under `media/documents/library/`), the read-only `DocumentReference` projection of act/protocol/task attachments in `documents/references.py`, the unified search layer in `documents/search/`, the file browser, and every mutation in `documents/services.py` |
| `smk` | СМК audit records: `SmkSource` (внешний/внутренний аудит, `audit_date`, `status` ACTIVE/ARCHIVED), `SmkNonConformity`, `SmkCorrectiveAction` + assignees, `SmkHistoryEvent`, the registry/form/record pages under `/quality/smk/`, and two write paths in `smk/services.py` — `create_smk_source()`, which stores the record and creates one real `tasks.Task` per мероприятие in the same transaction (reached only through the confirmation step in `smk/views.py`), and `archive_smk_source()`, the record's only state change. No task system of its own |
| `notifications` | in-app notifications, routing, deduplication, email delivery queue |
| `realtime` | event contract, targets, channels, publisher, SSE endpoint, sync revisions. No models, no migrations |
| `maintenance` | technical read-only commands and transfer tooling. No models, no migrations |

The user-facing sections are Акты (`/quality/acts/`), Задачи
(`/quality/tasks/`), Протоколы (`/quality/protocols/`), СМК (`/quality/smk/`),
Калькулятор времени навивки (`/calculators/winding/`), Калькулятор рубки пластин
(`/calculators/plate-cutting/`) and Документация (`/documents/`). Под «Качество»
пункты идут Акты · Протоколы · СМК · Задачи; the СМК form is reachable both from
its own registry and, as before, from «Задачи» through `tasks:create`. `/` redirects to
`/quality/acts/` and so does the login fallback, for every role including
superusers. Django Admin (`/admin/`) is reached directly, not from the sidebar.

Public URLs follow the two-level convention `/quality/<module>/` and
`/calculators/<module>/`, mirroring the navigation; a new user-facing module is
mounted the same way. Infrastructure stays outside it: `/accounts/`,
`/notifications/`, `/realtime/`, `/health/`, `/admin/`. The path is public
routing only — app names, Python packages and URL namespaces are unchanged
(`acts:`, `tasks:`, `protocols:`, `calculator:`, `plate_cutting:`), so links
keep coming from `{% url %}`/`reverse()` and never from a hard-coded path. The pre-hierarchy
paths `/acts/`, `/tasks/` and `/calculator/` remain temporarily as unnamed
aliases in `ecosystem/legacy_urls.py`: a 307 to the canonical location, which
preserves the method of a POST, only for routes that exist there, and never
generated by Django itself.

The navigation panel is two levels deep: the click-controlled top-level
categories Качество (Акты, Задачи, Протоколы) and Калькуляторы (Калькулятор
времени навивки, Калькулятор рубки пластин), each opening its own vertical submenu,
plus Документация — a first-level item that is a plain link to `/documents/` and
has no submenu. Only leaf items are links; categories are buttons, one submenu open at a time,
and the panel and the profile menu are never open together. All of that state
lives in `static/js/app.js`.

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
  `acts.views.act_download_attachment`,
  `protocols.views.protocol_download_attachment` and
  `tasks.views.task_download_attachment`, each with a per-request
  permission check. `MEDIA_ROOT` is never published by the web server and is a
  different directory from `STATIC_ROOT`.
- Django Templates and vanilla JavaScript only: no framework, bundler or npm.
- **A live-replaced block and its initial server render share one markup
  contract.** The client swaps the whole contents of the container, so the
  fragment view must render the same outer partial the page does —
  `[data-live-act-history]` gets `history_timeline.html` (card, «История акта»,
  «Все события») from both, with `history_content.html` reused as the inner
  event list. A fragment rendering only the inner partial makes the wrapper
  disappear on the first refresh.
- **A live-replaced fragment never owns a listener bound at page load.**
  `[data-live-act-work]` is swapped wholesale, so its markup is wired by a
  delegated `document` listener or a `window.qualityFragments` initialiser; a
  one-shot `querySelector(…).addEventListener` dies on the first refresh.
- One confirmation modal, no browser dialogs: `includes/confirm_modal.html` with
  `static/js/confirm_modal.js`, driven by `data-confirm-*` on the trigger —
  `-url` posts the modal's own CSRF-protected form, `-form` submits an existing
  page form, `-comment="required"` adds the mandatory comment. That attribute is
  the only switch: a **return** («Вернуть ОТК/КО/ТО») confirms with the required
  «Комментарий к возврату», a **forward** action («Передать в КО/ТО», the ТО
  analysis submission, «Утвердить») confirms with title and text only. Hiding
  needs `.app-modal__comment[hidden] { display: none; }` in `components.css` —
  the author `display: grid` otherwise beats the user-agent `[hidden]` rule and
  the textarea stays visible on forward actions. Never branch on an action name
  in JavaScript, and never treat the hidden field as validation: the server
  still rejects an empty return comment.
- One button system: `.link-button` fixes font, size, height, padding, radius and
  states; a modifier (`--secondary`, `--warning`, `--danger`, `--success`,
  `--compact`) changes only colour or density.
- One text system: `static/css/text.css`, loaded **last** in `base.html` (after
  `{% block extra_head %}`) and in both print templates. It is the floor under
  every other stylesheet — never add a one-off `overflow-wrap`, `word-break` or
  `min-width: 0` to a module file to fix a long string. Four layers: `:root`
  inherits `overflow-wrap: break-word`; the layout containers and card surfaces
  are pinned with `min-width: 0` / `max-width: 100%`; table cells get
  `overflow-wrap: anywhere` (the only value that shrinks a column's *min-content*
  width, so an auto-layout column cannot grow without bound) with the sideways
  scroll confined to `.table-card` / `*-table-wrap`; and the opt-in classes.
  - Mark every element rendering free-form text with **`.user-text`** — defect
    descriptions, КО comments, корневые причины, protocol agenda/speeches/
    decisions, task text and execution notes, comments, document and folder
    names, СМК findings, saved-set names. It also forces `white-space: normal`,
    so it beats a `nowrap` its column set for dates or sizes.
  - `.user-text--pre` for a raw value not run through `|linebreaksbr`;
    `.text-clamp-1|2|3` for a display limit (always pair it with the full text
    on `title`); `.text-ellipsis` for a one-line cut in a dense row.
  - Page-level horizontal scrolling is off: `body { overflow-x: clip }` on
    `body` alone, so the value propagates to the viewport and `body` never
    becomes a scroll container — that is what keeps the sticky `.topbar` stuck
    to the window.
  - Print: `.print-section table` is `table-layout: fixed`. Paper has no
    scrollbar, and an auto-layout «Описание» column always asks for more than
    A4. Cover is `ecosystem/test_text_rendering.py`.
- `accounts.navigation.get_default_landing_url()` is the one answer to where a
  user belongs: `/quality/acts/` for everyone, including administrators and
  superusers.

## Domain invariants

- Every authenticated user may read every act: `all` contains all non-archived
  acts, `archive` contains all archived acts, and their detail pages are
  read-only outside the user's working scope. `my` is the working queue: ОТК
  gets own `CREATED_OTK` acts **plus every `OTK_REVIEW` act**, КО gets
  `KO_REVIEW`, ТО gets `TO_ANALYSIS` plus own `ACTIONS_ASSIGNED`; managers and
  administrators get all active acts. `OTK_REVIEW` is the department's queue,
  not the author's: `can_review_otk()` — and therefore `can_return_to_to()`
  and `can_approve_act()` — is «any active ОТК employee», so an act is never
  stranded because its creator is away. `CREATED_OTK` stays the creator's own
  act **while the creator is still an eligible ОТК employee**; once they are
  not — deactivated account, deactivated profile or another role — any active
  ОТК employee may work on it, because an act returned from КО would otherwise
  be unreachable. `can_work_on_created_otk_act()` is the single rule, shared by
  `can_contribute_to_act()`, `can_edit_act()`, `can_send_to_ko()` and the `my`
  queryset, and `_move_act_workflow_task()` routes the `OTK_REWORK` entry the
  same way, so whoever receives the task may act on it; it never widens access
  while the author is still there. Notification routing follows the same two
  rules and imports `creator_is_eligible_otk()` rather than restating them:
  `ACT_SENT_TO_OTK` (final `OTK_REVIEW`) goes to **all** active ОТК employees,
  and `ACT_RETURNED_TO_OTK` (`KO → CREATED_OTK`) to the creator alone while
  they are still eligible, falling back to ОТК at large when they are not.
  Manager and administrator behaviour is unchanged, and the UI reads the
  same helpers through `get_available_act_actions()`. Global read access never grants comments, uploads, workflow
  actions or editing; editing remains limited to authorised `CREATED_OTK` acts.
- Every return transition requires a non-whitespace comment saved atomically
  with it, and must not emit a duplicate ordinary-comment notification. With
  several defects, КО must decide on **every** defect before the act may leave
  `KO_REVIEW`; legacy decision values stay readable and must not be rewritten by
  a data migration.
- Every defect requires a workshop/supplier choice on the form, while the model
  field stays `blank=True` so existing rows keep no invented value. Revealing
  the remaining fields must never clear already-entered values.
- **`Act` owns document and workflow data; `ActDefect` is the only source of
  defect data.** The act keeps its number, creator, customer, order,
  nomenclature, КД designation, type, status/priority, the КО/ТО/approval/
  closing workflow and the timestamps. Workshop, ЗНП, party, defect type,
  operation, МП type, description, detected date, quantities and the per-defect
  КО decision exist only on the defect — the act carries no summary of them and
  no code may reconstruct one. Never assume the first defect represents the act:
  an act may mix workshops, and reordering its defects changes nothing. An act
  with no defects renders the neutral empty state; it never gets invented data.
- **`acts/workshops.py` owns the workshop rules** — which fields apply, which
  are required, what is cleared when not applicable, the allowed defect type
  codes and the presentation metadata. `ActDefectForm` validates against the
  profile and is the authority; adding a workshop means a new `Workshop` choice
  plus one profile, not new `if workshop == …` branches. `MP_SHOP` keeps its
  full field set; `PIR_SHOP` («Цех ПиР») collects only workshop, ЗНП, defect
  type, detected date and the two quantities, and two `ActDefect` check
  constraints guard the quantities and the absence of МП-only data on a ПиР row.
- **Frontend workshop behaviour is presentation only.** `static/js/act_create.js`
  reads the one `client_config()` JSON the form renders and never restates a
  rule; it shows/hides fields, sets `required`, filters the dropdown and moves
  the detected date between groups for UX. Hiding needs the
  `display: none !important` rules in `acts.css`: an author `display` on a
  label beats the user-agent `[hidden]`, so the attribute alone is not enough.
- **`Act.due_date` is the act's creation date plus three working days**, and
  nothing else. `acts.models.calculate_act_due_date()` is the single
  implementation, and it delegates the weekday arithmetic to
  `ecosystem.workdays.add_working_days()` — Monday–Friday, no holiday
  calendar, the creation day itself not counted, so Monday → Thursday,
  Thursday → Tuesday, Friday → Wednesday. It is written **once**, when the act
  is created, from `timezone.localdate()`: `Act.created_at` is `auto_now_add`
  and therefore unknown until the row exists. Editing an act — including a
  defect's `detected_at` — carries the stored deadline over unchanged; no code
  re-derives it from defect data, and `ActDefect.detected_at` keeps its own
  meaning.
- An act must have at least one `ActDefect`: `manage.py audit_legacy_act_defects`
  reports any that do not, and migration `0024` refuses to run while one exists.
  Never infer a workshop from old data or fabricate a defect to get past it.
- Never write a placeholder such as `"-"` in place of missing business data;
  read-only views render the neutral `—`.
- **A person is written by name, never by their login.** `{{ user }}` renders
  Django's `User.__str__()`, which is the account name, so every template that
  names an employee uses `accounts/templatetags/people.py` —
  `{{ user|person_name }}` (`get_full_name() or get_username()`) and
  `{{ user|person_initials }}` for the avatar, derived from the displayed name
  so the circle and the label cannot disagree. One filter, not a
  `get_full_name|default:username` expression copied around, and presentation
  only: authentication usernames are unchanged.
- Structured TO analysis is atomic and read-only after submission; each
  corrective action needs text, a department, a due date and at least one active
  assignee. Approval revalidates it all under lock and creates the tasks.
- **A corrective action is executed shared or split, and `Task.source_action`
  is a foreign key because of it.** `ActCorrectiveAction.split_for_assignees`
  is off by default and approval then behaves as it always has: one task
  carrying every assignee, which any one of them completes for all. Turned on
  for an action with two or more assignees, approval creates one independent
  task per assignee instead — same act, root analysis, corrective action,
  wording, department and deadline, one `TaskAssignee` each, completed
  separately. Splitting a single assignee means nothing, so
  `apply_structured_to_analysis()` stores the flag normalized off below two
  assignees; that is the single write point, which is why the editor's matching
  behaviour is presentation only. The flag is execution metadata: the root
  analysis, the corrective action row and the printed act are unchanged by it,
  and «Анализ ТО» stays one line per corrective action — a shared one shows its
  task's status, a split one shows «2 из 5 выполнено», and the individual tasks
  are listed in «Связанные мероприятия».
- **`tasks.services.create_act_action_task()` owns the act task, not the
  decision to make one.** It reads the act, the root analysis, the wording, the
  department and the deadline off the corrective action, validates, saves,
  attaches assignees, emits `task.created` and logs — the same shape as
  `create_protocol_action_task()`. How many to create, and whether the act may
  be approved at all, stay in `acts/services.py`, inside its approval
  transaction, so a refusal on one individual task rolls back its siblings and
  the approval together.
- A shared task is completed **once** by any assignee and requires a
  non-whitespace execution result; assignee changes go only through
  `tasks.services.replace_task_assignees()`. Every authenticated user may read
  every task through `all`, `archive` and task detail; only active assigned
  tasks appear in `my`, and read access never grants completion rights.
- **A task's origin is `source_type`, never a nullable relation.** Six values
  exist — `ACT`, `ACT_WORKFLOW`, `ACT_REJECTION`, `PROTOCOL_APPROVAL`,
  `PROTOCOL_ACTION`, `SMK` — and exactly one relation shape is valid for each,
  enforced by `Task.clean()` and by the
  `task_source_relations_match_source_type` check constraint:

  | `source_type` | required | must be NULL / empty |
  | --- | --- | --- |
  | `ACT` | `act`, `root_analysis`, `source_action`, `department` | `protocol`, `protocol_action`, `workflow_stage` |
  | `ACT_WORKFLOW` | `act`, `workflow_stage` | `root_analysis`, `source_action`, `protocol`, `protocol_action`, `individual_assignee` |
  | `ACT_REJECTION` | `act`, `department` | `root_analysis`, `source_action`, `protocol`, `protocol_action`, `individual_assignee`, `workflow_stage` |
  | `PROTOCOL_APPROVAL` | `protocol`, `department` | `act`, `root_analysis`, `source_action`, `protocol_action`, `individual_assignee`, `workflow_stage` |
  | `PROTOCOL_ACTION` | `protocol`, `protocol_action`, `department` | `act`, `root_analysis`, `source_action`, `workflow_stage` |
  | `SMK` | `smk_source`, `smk_action`, `department` | `act`, `root_analysis`, `source_action`, `protocol`, `protocol_action`, `individual_assignee`, `workflow_stage` |

  The act relations are nullable *only* so the other shapes can exist; for an
  `ACT` task all three stay required. `department` is nullable for the same
  reason and for one source only — an `ACT_WORKFLOW` entry belongs to a *role*,
  which has no single department — so the constraint states
  `department IS NOT NULL` explicitly on the other branches rather than
  leaving it to the column. `smk_source`/`smk_action` are stated `IS NULL` on
  every non-`SMK` branch for the same reason: a relation outside a shape must
  be provably absent, not merely unmentioned. `Task.clean()` adds them to
  `forbidden` in one place instead of restating them in five tuples.
- **`ACT_WORKFLOW` is the act's route made visible in «Задачи», and is not an
  `ACT` task.** An `ACT` task is a corrective action somebody performs; an
  `ACT_WORKFLOW` task is a work-queue entry saying which stage the act is
  waiting on. The two never merge: `get_related_tasks()` («Связанные
  мероприятия») filters `source_type=ACT`, and the corrective-action behaviour,
  its split mode and its constraints are untouched.
  `Task.workflow_stage` — `KO_REVIEW`, `TO_ANALYSIS`, `OTK_REVIEW`,
  `OTK_REWORK` — is **persisted**, not read back off `Act.status`: a closed
  entry has to keep saying what it stood for long after the act moved on.
- **The `ACT_WORKFLOW` lifecycle is driven by `acts/services.py`, exactly as
  the approval queue is driven by the protocol workflow.**
  `tasks.services.move_act_workflow_task()` closes every active routing task of
  the act and opens the next stage's; `acts/services._move_act_workflow_task()`
  is the only caller, once per transition, inside the transition's own
  `atomic()` block under the act row lock already taken. That lock is also what
  makes «at most one active routing task per act» true without a database
  constraint — two transitions of one act cannot run at once — and a
  rolled-back transition leaves no task behind. Send to КО → `KO_REVIEW`; КО
  decision → `TO_ANALYSIS`; ТО analysis → `OTK_REVIEW`; approval closes the
  last one and opens nothing. Returns keep the queue with the act: КО → ОТК
  opens `OTK_REWORK`, ТО → КО opens `KO_REVIEW`, ОТК → ТО opens `TO_ANALYSIS`.
  **Creating an act creates no routing task** — `CREATED_OTK` is the creator's
  own work and they already hold the act.
- **`ACT_REJECTION` is ordinary executable work, not a routing entry.** It
  appears in the registry and in `my`, opens as a normal task page (never a
  redirect), takes attachments and is completed by its assignee with the usual
  execution comment; the administrative fallback applies as to any task. It is
  created **only** inside the successful `KO_REVIEW → TO_ANALYSIS` transition,
  from the defects as they were just saved: a defect qualifies when
  `workshop == MP_SHOP` **and** `ko_decision == PROHIBIT_USE`, so ПиР defects
  and every permitting decision produce nothing. **One shared task per act**,
  not per defect — `describe_rejected_defect()` writes one sentence per
  qualifying defect on its own line, in the act's order, and quantities are
  never summed across ЗНП rows. The deadline is the act's `due_date` (today as
  a legacy fallback) and `created_by` is the КО user.
  **Recipients are resolved by department, never by role**: active users with
  an active profile in the active `Department.code == 'PDO'`, whatever role
  each holds. A missing, inactive or empty ПДО department skips the task and
  logs the reason — it must never block the act. `unique_act_rejection_task`
  (partial, on `source_type='ACT_REJECTION'`) is what makes a retry or a
  concurrent transition unable to duplicate it; the service's existence check
  is only the readable path to the same answer. It is *not* an `ACT` task and
  never appears in «Связанные мероприятия».
- **`SMK` is ordinary executable work with a source document of its own.** It
  appears in the registry and in `my`, opens as a normal task page, takes
  attachments and is completed by its assignee with the usual execution
  comment. It is created **only** by `smk.services.create_smk_source()`, which
  writes the `SmkSource`, its findings, its `SmkCorrectiveAction` rows and one
  task per measure inside a single `atomic()` block — a record whose measures
  reached nobody is never left behind. **One task per мероприятие**, carrying
  every исполнитель: an СМК measure is never split, so
  `tasks.services.create_smk_action_task()` takes no `individual_assignee` and
  the branch forbids it. `unique_smk_action_task` is what makes a retried or
  concurrent submission unable to duplicate it. Who may create one is
  `smk.permissions.can_create_smk_task()` — the СМК role, руководитель or
  администратор — re-checked inside the service, not only in the view;
  completion rights are `tasks.permissions.can_complete_task()`, unchanged.
  `SmkCorrectiveAction.requires_attachment` («Требуется вложение») is copied
  onto the task exactly as the protocol and act variants copy theirs, and is
  enforced only by `complete_task()`: СМК adds no attachment rule of its own.
- **`SmkSource.audit_date` is the audit's own date and `created_at` is not.** A
  record is often written up days after the audit it describes, so the page
  shows `audit_date` and never the timestamp. Nullable only because the column
  was added to existing rows, and deliberately *not* backfilled from
  `created_at` — guessing one from the other would store a fabricated fact.
- **`SmkCorrectiveAction.non_conformity` is optional, and the form names it by
  row, not by id.** One measure often answers several findings, so the link is
  a statement the author may make rather than a rule; `SET_NULL` keeps a
  measure when a finding goes. The findings have no primary keys while the form
  is being filled, so the selector posts a *row index*, `SmkSourceForm` maps it
  onto the rows that request actually kept, and `create_smk_source()` resolves
  it against the findings it has just created. A link therefore cannot survive
  the row it pointed at being emptied.
- **A finding carries no status of its own.** It is shown as it was recorded —
  text, «Выявлено» (the audit date) and «Источник» — and nothing else: what is
  being done about it is the state of the measures naming it, and a second
  answer on the finding could only disagree with them.
- **`SmkSource.status` is a shelf, not a workflow.** Two values, `ACTIVE` and
  `ARCHIVED`, and exactly one transition: `smk.services.archive_smk_source()`,
  driven solely by «Архивировать» on the record page. Completing the tasks a
  record produced never moves it — the tasks are tracked in «Задачи» and the
  record is the document they came out of. The transition writes `status`,
  `archived_at` and `archived_by` and nothing else: findings, measures, tasks
  and every link between them stay as they were, and an archived record is
  still opened and read at the same URL. Who may do it is
  `smk.permissions.can_archive_smk_source()` — the same three roles as
  creation, and only while the record is live — asked once by the view for the
  button and re-checked inside the service under `select_for_update()`. The
  registry (`smk:list`, `build_smk_list_state()`) is the two tabs this status
  splits, «Работа» and «Архив», with «Количество задач» annotated in the query
  rather than counted per row; reading it is open to every authenticated user,
  «Создать» is not.
- **The record page is three tabs.** «Акт аудита» (findings as a
  timeline, measures as one-row cards whose подразделение/исполнитель/срок/
  задача are grid items of the card itself, not a nested grid — that is what
  keeps them on one line), «Связанные мероприятия» (the tasks,
  in the act page's own table shape) and «История», all built from one
  `get_source_detail()` read so they cannot disagree. The heading carries only the identifier — тип
  аудита, дата аудита, автор and the task count live in the information card
  (six of them: тип аудита, дата аудита, автор, дата создания, статус,
  количество задач) and are never repeated, save for «Архивировать» and the
  status badge, which belong to the heading because they act on the record as a
  whole. Styling reuses `acts.css`/`components.css`
  (`act-detail-heading`, `act-detail-tabs`, `detail-section`, `act-badge`,
  `related-activities`, and the whole `history-feed` timeline verbatim);
  `static/css/smk.css` adds only the information card, the findings timeline,
  the measure card and the registry table.
- **«Связанные мероприятия» reports the task and never restates it.** Статус,
  «Требуется вложение» and the attachment count in that table are read from the
  `Task` — `Task.requires_attachment` is the authority once the snapshot is
  taken, and `complete_task()` is the only place the rule is enforced — so the
  record cannot promise something the task would refuse. `_measure_row()` falls
  back to the measure's own flag only when no task exists at all. Acting on the
  work (completing it, attaching a file) happens on the task's page, which the
  № column links to.
- **`SmkHistoryEvent` is a short list of facts, not an audit system.**
  `CREATED`, `TASK_CREATED` and `ARCHIVED` are written by `smk/services.py`
  alone, through `_record()`, inside the same `atomic()` block as the change
  they describe — a rolled-back write takes its event with it, and a refused
  archive writes nothing. `EDITED` is named but written by nothing: the record
  is immutable and `status` has its own event; it exists so a future edit path
  records rather than invents a type. There are no history fragments and no
  filters. `smk.0006` backfills records stored before the trail from facts
  already in the database (`SmkSource.created_at`/`created_by`, each `Task`'s
  own `created_at`/`created_by`), stamping `created_at` after insert because it
  is `auto_now_add`; nothing is invented and no `ARCHIVED` event is backfilled.
- **Creating an СМК record takes two POSTs to `smk:create`, and the flag is the
  server's rule, not the dialog's.** A valid POST without
  `confirmed=1` writes **nothing** — it comes back as the same page carrying
  `build_confirmation_summary()` (источник, counts, исполнители, all built from
  the *validated* structure) with the form redisplayed exactly as it was typed.
  Only a POST carrying the flag reaches `create_smk_source()`. The page's own
  `<dialog>` — SMK-specific because the summary is structured, unlike the
  action-agnostic `includes/confirm_modal.html` — is the fast path to that
  second POST; its «Создать» is a submit button whose `name`/`value` *is* the
  flag, so the step works with JavaScript disabled too.
- **A routing task is never completed by an employee.** `Task.is_routing_task`
  covers `PROTOCOL_APPROVAL` and `ACT_WORKFLOW` alike, and both
  `can_complete_task()` and `complete_task()` refuse it: the real action is
  «внести решение КО», «выполнить анализ ТО», «утвердить акт» or «согласовать
  протокол», and it is taken on the source document. `tasks.views.task_detail`
  therefore redirects such a task to its act or its protocol instead of
  rendering the execution form, and it carries no attachment card.
  Assignees are every active holder of the stage's role
  (`tasks.services.active_users_for_role()`, the same rule notifications route
  by); `OTK_REWORK` goes to the act's author, who is the only one who may send
  it on, falling back to ОТК at large when that account no longer qualifies. A
  stage with no active holder creates **nothing** and logs it: a plant without
  an active КО employee must still be able to send an act to КО.
- **`Task.individual_assignee` is the one field that tells a split task from a
  shared one, for both domains.** NULL is the task everybody shares; set is the
  task split off for that person, whose `TaskAssignee` rows are exactly them.
  It is optional on the two split-capable sources — `ACT` and `PROTOCOL_ACTION`
  — and forbidden on an approval task, which is one person's queue entry and
  has nothing to split. There is no second, act-specific field.
- **How many tasks a source may own is stated by constraints, not by the
  relation.** `source_action` and `protocol_action` are both foreign keys, and
  four unique constraints replace what their one-to-ones used to guarantee:
  `unique_shared_act_action_task` / `unique_shared_protocol_action_task`
  (partial, on `individual_assignee IS NULL`) allow at most one shared task per
  source, and `unique_individual_act_action_task` /
  `unique_individual_protocol_action_task` at most one per `(source, individual
  assignee)`. A repeated or concurrent approval therefore cannot duplicate a
  task, whatever a service check does.
- The rules SQL cannot express stay in `Task.clean()` and must be re-checked in
  any service that writes a task — a single-row check constraint cannot span
  two tables. They are: `protocol_action.protocol == task.protocol`;
  `source_action.root_analysis == task.root_analysis` and
  `root_analysis.act == task.act`; and, for a split task, the individual
  assignee really being an assignee of the source it was split off from.
- Every writer states `source_type` explicitly; the `ACT` default exists only
  so the column could be added to the existing production table and is never a
  substitute for saying so. Read-side code must not assume `task.act` or
  `task.root_analysis` is present — branch on `source_type`.
- **A `TaskAttachment` is optional, and never a precondition of finishing.**
  Uploading is its own endpoint (`tasks:add_attachment`) and its own form, so
  the completion form carries no file field and a task is still completed with
  the execution comment and zero attachments; completion deletes nothing.
  Files reuse the shared policy — `ecosystem.attachments` for size and
  extension, a `tasks/attachments/<task_id>/<uuid>.<ext>` path that never
  contains the browser's name, `MEDIA_ROOT` unpublished — and are served only
  by `tasks.views.task_download_attachment`, which re-loads the row scoped to
  the task in the URL and asks permission again. `can_upload_task_attachment()`
  **is** `can_complete_task()`: an assignee of an active ordinary task plus the
  administrative fallback, which also means a routing task accepts no file at
  all. Reading a task is open to every authenticated user, so downloading is
  too — and read access still grants no upload. There is no deletion, no
  description and no second file-security implementation.
- **`requires_attachment` is a source-domain answer that `Task` snapshots.**
  `ProtocolAction.requires_attachment` and
  `ActCorrectiveAction.requires_attachment` (both `BooleanField(default=False)`)
  are the author's/ТО's choice, stored on the draft row so it survives a
  protocol returned for revision and a ТО analysis returned from ОТК, and
  editable right up until the real task exists. Unlike `split_for_assignees` it
  is stored exactly as answered — a required file means the same for one
  исполнитель as for five, so nothing normalizes it. When the task is created,
  `create_protocol_action_task()` and `create_act_action_task()` copy it once
  into `Task.requires_attachment`; that copy is authoritative and is never read
  back through the relation, so editing the source row afterwards cannot change
  a live or completed task. Shared execution gives the one task the
  requirement, satisfied by any single attachment on it; split execution gives
  *every* generated task the same requirement, and each исполнитель satisfies
  it on their own task — a colleague's separate task never counts.
  `PROTOCOL_APPROVAL`, `ACT_WORKFLOW` and `ACT_REJECTION` are never given the
  flag. **The rule is enforced in `complete_task()` and nowhere else**: after
  the routing, permission and execution-comment checks and before the task
  becomes COMPLETED, a task with `requires_attachment` and zero
  `TaskAttachment` rows is refused with «Для выполнения этой задачи необходимо
  добавить вложение.» (logged as `missing_required_attachment`). The task page
  only announces the requirement beside «Вложения» — the file input is never
  HTML-required and the button is never hidden or disabled, because uploading
  and completing are deliberately separate requests. The approved, read-only
  «Анализ ТО» table and the printed act deliberately show nothing about it: it
  controls the generated task, not how the document reads.
- **`PROTOCOL_APPROVAL` is never completed through the normal task workflow.**
  `can_complete_task()` and `complete_task()` both refuse it: agreeing to a
  protocol is its own decision, and closing it with an execution comment would
  silently approve a document. Such tasks are closed only by the protocol
  approval workflow, through the dedicated services listed below.
- Schema changes to `Task` migrate the existing production table in place:
  add first, classify existing rows in a separate data migration, relax
  nullability, then add constraints. Never delete, recreate or renumber task
  rows.
- **One migration generates tasks, and only because the acts already in flight
  had no way to acquire them.** `tasks.0013` recomputes `Act.due_date` for
  **active** acts (`CREATED_OTK`, `KO_REVIEW`, `TO_ANALYSIS`, `OTK_REVIEW`)
  from their own `created_at` plus three working days, and creates the one
  `ACT_WORKFLOW` entry their current stage implies. Archived acts are history
  and are never rewritten; `CREATED_OTK` gets no task; an act that already has
  an active routing task, or a stage whose role has no eligible user, is
  skipped, so a second run changes nothing. It emits no notification and no
  realtime event — a migration is not a workflow transition — touches no
  corrective-action or protocol task, and reads only historical models through
  `apps.get_model`. This is the exception, not a licence: normal work still
  never generates tasks from existing rows in a migration.
- `Act.number` is a **business identifier, never the identity**. The user types
  a suffix of up to `ACT_NUMBER_SUFFIX_LENGTH` (5) arbitrary characters and
  `acts.models.format_act_number()` builds `АОК-{year}-{zero-padded suffix}` on
  the server from `timezone.localdate().year`. It has no uniqueness constraint:
  two acts may share a number, and `Act.pk` stays the only unique key for
  relations, URLs and rows. There is no automatic numbering and no counter
  table — do not reintroduce either. Editing keeps the existing number
  (including a legacy one) unless the user changes the field, and no migration
  rewrites historical numbers.
- `ActHistoryEvent` is the business audit trail, append-only from the normal UI.
  Comments are manual notes and never replace history. Do not add an `AuditLog`
  model — logs are diagnostics and may be rotated away.
- Notifications are created in the same transaction as their business event,
  deduplicated per recipient by a stable source key, and routed in one place:
  `notifications/services.py`. Never create a `Notification` from a view, a
  template, a model signal or JavaScript.
- **A notification's origin is `source_type`, never a nullable relation.** Three
  values exist — `ACT`, `PROTOCOL`, `TASK` — and exactly one relation shape is
  valid for each, enforced by `Notification.clean()` and by the
  `notification_source_relations_match_source_type` check constraint:

  | `source_type` | required | must be NULL |
  | --- | --- | --- |
  | `ACT` | `related_act` | `related_protocol`, `related_task` |
  | `PROTOCOL` | `related_protocol` | `related_act`, `related_task` |
  | `TASK` | `related_task` | `related_act`, `related_protocol` |

  `related_act` keeps its name and its meaning; it is nullable *only* so the
  other shapes can exist. `create_notifications()` takes exactly one of
  `act=`/`protocol=`/`task=` and derives `source_type` from it, so the type and
  the stored relation can never disagree. `get_notification_url()` resolves by
  source type through named routes — `acts:detail`, `protocols:detail`,
  `tasks:detail` — from the stored foreign key id, never a hard-coded path.
  Schema changes migrate the existing production table in place: add fields,
  classify existing rows as `ACT` in a data migration, relax nullability, then
  add the constraint. Never recreate notifications or their `NotificationDelivery`
  rows — read state, `read_at`, deduplication keys and delivery state are live
  production data.
- **Protocol notifications are one per business fact.**
  `notify_protocol_approval_required()` (one per `ProtocolApproval`, keyed on
  its pk, so a new revision notifies again), `notify_protocol_returned()` and
  `notify_protocol_approved()` (the author, keyed on protocol + revision) are
  `PROTOCOL`-sourced; `notify_protocol_task_assigned()` is `TASK`-sourced and
  links to the task. Each is called inside the workflow transaction that
  already holds the `Protocol` lock, and only *after* the row it describes
  exists, so a rollback leaves no notification claiming a return, an archive or
  a task that never happened. A **`PROTOCOL_APPROVAL` task creates no
  assignment notification** — the approver already has the approval-required
  one, and `notify_protocol_task_assigned()` refuses any task that is not
  `PROTOCOL_ACTION`. `notify_protocol_approved()` uses `exclude_actor=False` on
  purpose: a protocol nobody had to approve is archived by its own author.
- **The email matrix is a fixed list of business facts, not "every
  notification".** `EMAIL_ELIGIBLE_EVENTS` in `notifications/services.py` is the
  single authority for which events also leave the application by mail:

  | source | email | in-app only |
  | --- | --- | --- |
  | act | `ACT_SENT_TO_KO`, `ACT_SENT_TO_TO`, `ACT_SENT_TO_OTK`, `ACT_RETURNED_TO_OTK`, `ACT_RETURNED_TO_KO`, `ACT_RETURNED_TO_TO`, `ACTION_ASSIGNED`, `ACT_APPROVED` | `COMMENT_ADDED` |
  | protocol | `PROTOCOL_APPROVAL_REQUIRED`, `PROTOCOL_RETURNED_FOR_REVISION`, `PROTOCOL_APPROVED` | — |
  | task | `PROTOCOL_TASK_ASSIGNED`, `ACT_REJECTION_ASSIGNED` | — |

  `COMMENT_ADDED` stays out on purpose: comments are frequent, carry no
  required action of their own and would turn the mailbox into noise.
  `ACT_APPROVED` is informational — its required action is «дополнительных
  действий не требуется», and the link still opens the act.
- **One renderer serves `ACT`, `PROTOCOL` and `TASK`.** `_send_email()` builds a
  single normalized context through `notifications.services` —
  `describe_notification_source()`, `get_required_action()`,
  `get_notification_url(absolute=True)`, `get_notification_open_label()` — and
  never dereferences `related_act`. One SMTP worker, one template pair
  (`notifications/email/notification.{txt,html}`), no per-domain copy. A task
  email names «Задача №<pk>» plus what it came from («Брак по акту …»,
  «Протокол …»), its due date and whether an attachment is required; the stored
  source codes (`ACT_REJECTION`, `PROTOCOL_ACTION`) are never user-facing text.
- **Routing tasks never produce a second email.** `ACT_WORKFLOW` and
  `PROTOCOL_APPROVAL` rows create no notification at all — the act transition
  and `PROTOCOL_APPROVAL_REQUIRED` already tell the same person the same thing.
  Likewise a corrective action sends `ACTION_ASSIGNED` and *not* an additional
  generic "вам назначена задача". One meaningful email per assignment event.
  Email is driven by intentional `Notification` events, never by a generic
  "task created" signal.
- **`ACT_REJECTION` notifies ПДО once.** `ensure_act_rejection_task()` calls
  `notify_act_rejection_task_assigned()` only on the one invocation that really
  creates the task, so its two idempotency guards (the existence check and
  `unique_act_rejection_task`) also guarantee exactly one notification per
  assigned ПДО employee. It is `TASK`-sourced, keyed on `task:<pk>`, uses
  `exclude_actor=False` — a КО employee may work in ПДО and must still get
  their own work item — and is email-eligible.
- **`send_welcome_email <username|ALL>` is an administrative command, not a
  business event.** It mails initial onboarding credentials (login = password =
  username) through the same configured backend, creates no `Notification` and
  no `NotificationDelivery`, and refuses to run while
  `EMAIL_NOTIFICATIONS_ENABLED=false`. It sends only after
  `user.check_password(user.username)` confirms the initial password is still
  in force — a changed password is skipped, never guessed — and `ALL` sends one
  personalized message per active user, never a shared BCC. Failures are
  reported through `sanitize_error()`; passwords, bodies and SMTP credentials
  never reach stdout or the log.
- **SMTP stays provider-independent.** `EMAIL_BACKEND`, `EMAIL_HOST`,
  `EMAIL_PORT`, `EMAIL_USE_TLS`/`EMAIL_USE_SSL`, `EMAIL_HOST_USER`,
  `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`, `EMAIL_TIMEOUT` and the queue
  settings come from the environment alone. Never hard-code a provider's host
  or credentials in settings or code; production picks its relay through
  `.env`. `ecosystem/checks.py` validates the *structure* of that configuration
  when email is enabled and never opens an SMTP connection during startup or a
  readiness check — connectivity is an explicit operational smoke test.
- **`notification.created` carries identifiers only** — recipient, actor,
  `source_type`, the nullable act/protocol/task ids and the event type — and
  never protocol text, a return comment, a task description, a name or an
  address.
- **The protocol lifecycle is `DRAFT → APPROVAL → REVISION → ARCHIVED`, and
  every transition lives in `protocols/services.py` under the protocol row
  lock.** Submission is author-only and opens a *new* revision with a full
  fresh round; an author never approves their own protocol
  (`collect_required_approvers()` subtracts them, as a participant and as a
  decision assignee alike); a return needs a reason, cancels the rest of the
  round and makes the document editable again; the last approval archives it
  in the same transaction. `ARCHIVED` is terminal — not editable, not
  deletable. Numbering stays per `ProtocolType`, smallest free number,
  released by deleting a draft and never by archiving.
- **A protocol decision is not asked for its department.**
  `ProtocolAction.department` is derived by `ProtocolDraftForm` from the
  department chosen next to the decision's *first assignee* — already validated
  against that employee's profile — and it is what becomes `Task.department`
  when the protocol archives. The column stays on the model; only the duplicate
  question was removed, because two selectors for one fact could disagree.
- **Protocol tasks are ordinary tasks with a protocol source.** Archiving
  creates `PROTOCOL_ACTION` tasks from the decisions, and
  `describe_task_source()` links each one back to the protocol. A
  `PROTOCOL_APPROVAL` task is a work-queue entry only: it carries no decision,
  is never a second assignment, redirects from the task page to the protocol,
  and is closed by the approval decision rather than by «Завершить». Act task
  behaviour is untouched.
- **A decision is executed shared or split, and `Task.protocol_action` is a
  foreign key because of it.** `ProtocolAction.split_for_assignees` is off by
  default and archiving then behaves as it always has: one task carrying every
  assignee, which any one of them completes for all. Turned on for a decision
  with two or more assignees, archiving creates one independent task per
  assignee instead — same wording, department, deadline, protocol,
  `protocol_action` and author, one `TaskAssignee` each, completed separately.
  `Task.individual_assignee` is the only thing that tells the two apart: NULL
  for the shared task, the person for a split one. Splitting a single assignee
  means nothing, so `_apply_actions()` stores the flag normalized off below two
  assignees — the single write point, which is why the editor's matching
  behaviour is presentation only.
- **How many tasks a decision may own is stated by constraints, not by the
  relation.** `unique_shared_protocol_action_task` (partial, on
  `individual_assignee IS NULL`) allows at most one shared task per decision —
  what the dropped one-to-one used to guarantee — and
  `unique_individual_protocol_action_task` at most one per `(decision,
  individual assignee)`. A repeated or concurrent finalization therefore cannot
  duplicate a task, whatever a service check does. The two rules SQL cannot
  express stay in `Task.clean()` and `create_protocol_action_task()`: the
  individual must really be an assignee of that decision, and a split task's
  assignee list must be exactly that one person.
- **The split flag changes execution and nothing else.** Required approvers are
  still `participants requiring approval ∪ every ProtocolAction assignee −
  author`, so five assignees mean five signatures either way and no extra
  `ProtocolApproval` row exists because execution was split. The decision stays
  one `ProtocolAction`, one card on the protocol page and one row in the
  printable form and the PDF, however many tasks it produced; only the sections
  that deliberately list *real generated tasks* show them separately.
- **The protocol page is an accordion of `<details>` sections.** A document
  card that never collapses (identity, status, three-step workflow indicator
  from `describe_protocol_workflow()`), then Участники, Повестка, Слушали,
  Задачи and Согласование, each a section whose header carries a count —
  «4/5» for approval. Native `<details>` on purpose: collapsing needs no
  JavaScript, survives a live fragment replacement and stays keyboard
  accessible. Sections open by default, because a `required` field the browser
  cannot focus blocks submission silently; `protocol_editor.js` re-opens a
  section on its field's `invalid` event as the safety net, keeps the count
  badges in step, and suppresses the summary toggle when the header's add
  button is clicked. The editor keeps every existing hook — `[data-block]`,
  `[data-row-list]`, `[data-row-template]`, the `*-TOTAL_FORMS` names — so the
  restructuring changed markup only, never the form contract or a rule.
- **The editor never clears a selection the author did not clear.** The
  department selector next to a person filters the employee list, but
  `syncPair()` in `protocol_editor.js` leaves an already stored employee
  visible, enabled and selected however badly it matches — an employee moved
  to another department after the draft was saved, or a department since
  deactivated and therefore absent from `get_editor_directory()`. The row
  raises `[data-pair-warning]` instead, and changing or removing the person is
  the author's own explicit action. This covers both `[data-employee-pair]`
  blocks — участники and the исполнители of a protocol decision — because a
  redraw that empties a row is a save that silently deletes that participant.
- **The official document has one description.**
  `selectors.build_protocol_document()` returns plain data — header,
  participants, agenda, «Слушали», decisions, generated tasks, the approval
  block with dates and the final state, history — and both targets render it:
  `templates/protocols/print.html` (act print styling) and `protocols/pdf.py`
  (ReportLab). Neither restates a business rule, and the page and the file
  cannot drift apart. Both lay it out as the plant's paper protocol —
  «Протокол», date and «№ N / тип» on one line, Присутствовали, Повестка,
  Слушали, Решили, signature lines and «Подготовил» — flowing serif text on
  white, no cards, badges, borders or workflow controls. The approval block
  prints «Согласовано: ДД.ММ.ГГГГ» beside a participant whose
  `ProtocolApproval` is `APPROVED`, and a blank signature line for everyone
  else — in **both** targets, the printable page and the PDF, from the same
  `is_approved` / `decided_at` fields, because an already-signed document must
  not ask for that signature again.
  `pdf.approval_mark()` is the single wording of that marker and
  reads the stored `decided_at` — never a recomputed date — and a pending,
  returned or cancelled row gets no marker although it carries a date of its
  own. No new field: `selectors.build_protocol_document()` simply publishes
  `status`/`is_approved` alongside `decided_at`. The red line of «Повестка»,
  «Слушали» and «Решили» is `pdf.BODY_FIRST_LINE_INDENT` (30 pt) and its CSS
  counterpart in `print.css` (40 px); numbered items move `leftIndent` and
  `bulletIndent` together, so «1.» stays with its text.
  ReportLab is deliberately pure Python — the application is deployed on
  Windows too — and Cyrillic needs a real TTF, resolved from
  `PROTOCOL_PDF_FONT_PATH` or the usual Times/Liberation/DejaVu locations,
  serif preferred. A missing renderer or font is a 503 with a readable message
  and a production `manage.py check` error (`ecosystem.E028`), never a broken
  file.
- **Protocol realtime reuses the act architecture, not a second one.** Five
  event types (`protocol.created`, `protocol.updated`, `protocol.deleted`,
  `protocol.status_changed`, `protocol.approval_changed`) are emitted from
  `protocols/services.py` inside the workflow transaction; recipients come from
  `realtime.recipients.protocol_targets()`, which follows `can_view_protocol`
  («любой аутентифицированный») rather than inventing a rule. **An event
  describes committed observable state:** a submission that requires nobody and
  archives itself in the same transaction is one `DRAFT → ARCHIVED` event, never
  a pair naming an `APPROVAL` no reader could see, and a save that stored
  nothing new emits nothing. `/realtime/sync/` carries a `protocols` revision
  built from three aggregates — protocol totals, the status mix and the approval
  rows — because approving one position leaves the protocol row untouched. The
  approval and decision tasks the workflow creates keep emitting their own
  `task.*` events from `tasks/services.py`; that behaviour is unchanged.
- **A protocol's collaboration is its own two tables, not the act's.**
  `ProtocolComment` and `ProtocolAttachment` are real foreign keys to
  `Protocol` — no `GenericForeignKey`, no shared table with `ActComment` /
  `ActAttachment` — and files live under `protocols/attachments/<protocol_id>/
  <uuid>.<ext>`, a UUID path that never contains the name the browser sent.
  Every file is served by `protocols:download_attachment`, which re-loads the
  row scoped to the protocol in the URL and asks `can_view_protocol` again; a
  denial and a missing file are the same 404. `protocols/services.py` owns both
  mutations, in protocol → attachment lock order, and writes the file before
  the row so a failed insert leaves no orphan.
- **Contributing stops at the archive; reading never does.**
  `can_contribute_to_protocol()` is «any authenticated user, unless the
  protocol is `ARCHIVED`» — deliberately wider than `can_edit_protocol()`,
  because commenting is not editing, and deliberately *not* the act's
  department-and-step rule, which the protocol workflow has no counterpart for.
  An archived protocol accepts no comment and no upload and allows no deletion,
  administrator included, but keeps handing out the files it already has.
  Deleting an attachment is the uploader's or an administrator's.
- **A return for revision writes the reason three times, on purpose.**
  `ProtocolApproval.return_comment` is the decision, `ProtocolHistoryEvent` is
  the workflow record, and a `ProtocolComment` created in the same transaction
  is the message the author has to answer. That comment records no
  `COMMENT_ADDED` history event — `RETURNED_FOR_REVISION` already *is* that
  event — and produces no notification, because the approver's return
  notification already exists. A rolled-back return leaves no comment.
  Protocol comments never notify at all: the protocol notification set is the
  approval one.
- **«Связанные мероприятия» is real work only.**
  `get_related_protocol_tasks()` returns `source_type=PROTOCOL_ACTION` through
  `get_readable_tasks_queryset()`, so task access stays centralized;
  `PROTOCOL_APPROVAL` rows are filtered out because a signing-round queue entry
  is not a related activity. A decision split for its assignees appears as the
  several independent tasks it became, one row and one id each — this is the
  one place that deliberately shows generated tasks rather than the decision,
  and the protocol page, the printable form and the PDF still render it as the
  single `ProtocolAction` it is.
- **Калькулятор рубки пластин calculates in the browser and stores inputs
  only.** The seventeen length bands and the `0.95 s` hole coefficient live only
  in `plate_cutting/constants.py`; the view renders them into the `<select>` of
  `templates/plate_cutting/page.html`, and `static/js/plate_cutting.js` reads a
  coefficient off the selected option instead of carrying a copy. One package is
  `range_seconds × plates + 0.95 × holes`, converted to hours (`/ 3600`) only
  afterwards and rounded to two decimals for display alone; `calculatePackage()`
  is the single implementation, and the visible result, the breakdown popup and
  «Итого» all render from what it returns. There is no journal and no Redis
  channel here.
- **A saved package set is inputs and order, never a result.**
  `PlateCuttingPreset` (name, unique normalized `search_name`, `set_quantity`,
  author, timestamps) owns
  an ordered `PlateCuttingPresetPackage` list (`range_value`, `plate_count`,
  `hole_count`, `display_order`); `range_value` is the `<select>` identifier of a
  band from `constants.PLATE_LENGTH_RANGES`, validated against those constants,
  so there is no second cutting-time table. Seconds, hours, totals and the
  expanded formula text are never persisted — a loaded set is rebuilt from the
  page's own package `<template>` and recalculated by the current formula, which
  is what keeps old sets valid when a constant changes. `plate_cutting/services.py`
  is the only write layer. Logical names are trimmed and case-insensitive through
  the unique `search_name`: an ordinary save conflicts without writing,
  overwrite replaces packages and quantity on the locked row, and save-as-new
  creates the first free `_01`, `_02`, … suffix under the database constraint.
  All writes and deletion are transactional.
- **Preset management has one permission rule.**
  `plate_cutting.permissions.can_manage_plate_cutting_presets()` allows active
  PDO/Admin profiles and genuine Django superusers. Everyone authenticated may
  search/load; only managers may create, overwrite, copy or delete, and both the
  JSON endpoints and UI use that helper.
- **Confirmation and set quantity are calculator state.** A new package is
  editable and must be confirmed locally before saving; a loaded package starts
  confirmed, and editing it requires the pencil action. `set_quantity` is
  enabled only while every package is confirmed and is persisted with the
  preset. Package inputs always describe one set: `single_set_total` is the sum
  of base package times, while every displayed package time and the grand total
  are multiplied by `set_quantity` only when all packages are confirmed.
- **Loading a set is confirmed before it overwrites anything.** `applyPreset()`
  replaces every package unconditionally, so the load handler asks
  `confirmReplace()` first: an untouched calculator (one package, no plates,
  the default zero holes) loads in one click, and anything else opens the
  page's own `[data-replace-modal]` `<dialog>` — «Отмена» puts the picker back
  with its list and query intact, never a browser `confirm()`.
- **`plate_count` and `hole_count` are bounded on all three sides.**
  `models.MAX_PLATE_COUNT` / `MAX_HOLE_COUNT` (1 000 000 each) are the single
  source of the limit: `services._integer()` enforces it, the view hands it to
  the `max=` attribute of both `<input type="number">`, and two
  `CheckConstraint`s keep it at the database level beside the existing
  `plate_cutting_preset_package_plates_positive`. Both fields stay
  `PositiveIntegerField`, i.e. PostgreSQL `integer`: an unbounded value does not
  fail validation there, it aborts the whole `INSERT` — and SQLite stores it
  silently, so the defect would only ever appear in production. `_integer()`
  converts with `int()` inside `try/except` rather than pre-checking
  `str.isdigit()`, which accepts strings `int()` refuses (`'--5'`, `'²'`); every
  malformed value is a 400 with the modal's message, never a 500.

- **The calculator was integrated from `StanisRyz/calculate` at commit
  `d32eae0e5d7b66bdd41214cc7ba9601534c4f254`** and this repository is now the
  source of truth for it. `static/js/calculator/rules.js` and
  `calculation.js` are the ported formulas: coefficients, caps, the 0,25
  rounding step, the calibration branch and the hoop-geometry rules must not
  be changed as a side effect of any other work. A deliberate formula change
  bumps `CALCULATION_VERSION` in `calculator/models.py`, which is stamped on
  every new entry so historical rows stay distinguishable.
- Calculator entries live in the ordinary `default` database — no second
  database, no separate connection, no JSON file, no File System Access API.
  «Проработка» is one shared journal for every authenticated user;
  `created_by`/`updated_by` are auditing only and grant nothing.
- **`UserProfile.Role.PDO` («ПДО») is a first-class role**, selectable in Admin
  beside ОТК, КО, ТО, Руководитель and Администратор, and it owns «Проработка»
  together with Администратор.
  `calculator/permissions.py::can_manage_workup()` is the **single** authority
  on journal mutation — **`PDO` OR `ADMIN` on an active profile, OR a genuine
  `is_superuser` fallback**, the same administrative fallback
  `acts.permissions.is_act_admin()` already applies — and every mutation path
  reuses it: the JSON endpoints (`entry_create`, `entry_manual_create`,
  production confirm/unlock, `entry_delete`, all through
  `@workup_manager_required` → JSON 403) and
  `WindingEntryAdmin.has_add/change/delete_permission`. ОТК, КО, ТО, MAS and
  Руководитель stay read-only, and no view, template or script restates the
  rule. It never keys on `department.code`, a username or `is_staff`. The
  department «Планово-диспетчерская служба» (`PDO`, seeded idempotently by
  `accounts.0003`) is organisational only and grants nothing; it is also listed
  in `MIGRATION_SEEDED_ROWS` so a fresh transfer target is still «empty».
- **`UserProfile.Role.MAS` (`mas`, «Мастер производства») is a first-class
  ordinary operational role.** Its active organisational department
  «Мастера производства» (`MAS`) is seeded idempotently by `accounts.0005`, but
  department and role remain separate concepts and no permission checks the
  department code. MAS reads all authenticated-user Act and task registries but
  has no Act working scope, creation, mutation, workflow, manager or administrator
  authority; creates and manages own protocols through the ordinary author and
  assigned-approver rules; completes assigned ordinary tasks but never a
  `PROTOCOL_APPROVAL` task through `complete_task()`; calculates winding and
  reads/searches/exports «Проработка» without mutating it; and has ordinary
  authenticated-user Plate Cutting preset Save/Search/Load. MAS has no Django
  Admin privilege.
- **`UserProfile.Role.SMK` (`smk`, «СМК») is a first-class role, read through
  `acts.permissions.is_smk()` like every other.** It grants exactly two things:
  creating an СМК record and the tasks it produces
  (`smk.permissions.can_create_smk_task()`, which also admits руководитель and
  администратор), and archiving one
  (`smk.permissions.can_archive_smk_source()`, the same three roles). It changes no act, protocol, calculator or admin right, and
  no check keys on the department. The department «Отдел СМК» (`SMK`, seeded
  idempotently by `accounts.0007` and listed in `MIGRATION_SEEDED_ROWS`) is
  organisational only. Руководитель and администратор are shown a task-type
  step at `tasks:create`; an СМК user, having one kind, is redirected straight
  to the form.
- **Reading the journal stays open to every authenticated user**: the calculator
  page, calculations, the entry list, the `d/D-b` search, reload and the
  `.xlsx` export. The template's `can_manage_workup` flag is presentation
  metadata deciding which controls exist — the manual-add row, the production
  inputs, `✓`, `✎`, the trash — and never a permission; the JS must not restate
  the rule. A read-only user's calculation renders its result normally and
  **never** posts to the create endpoint, so it persists nothing and reports
  no error.
- `ВН, с/мм` in the journal and in the export is the stored
  `WindingEntry.standard_coefficient`, displayed; there is no new field and no
  recalculation. Deletion is a hard delete of one primary key
  (`POST /calculator/entries/<pk>/delete/`) with no archive, trash or history,
  confirmed by the calculator's own `<dialog>` — it reuses `.app-modal` and the
  `.link-button` modifiers but not the global modal, which navigates on submit
  instead of awaiting a `fetch()`. The row leaves the table only after the
  server confirms.
- **A journal row's identity is its Django primary key; a *calculation case's*
  identity is `calculation_signature`.** They are not the same thing and no
  UUID is wanted. The signature is `d | D | b | δ | normalized calibration`,
  built only by `calculator.models.build_calculation_signature()` from
  validated numbers — never from the visible `d/D-b` name, never from anything
  the browser sends as a key. Calibration off, missing and `0` are one
  non-calibrated state. `case_key` is now just the normalized name, is not
  unique, and exists for search and legacy data.
- `source` decides whether the signature binds. `CALCULATOR` rows are unique
  per signature through a **partial** unique constraint
  (`calculator_unique_calculator_case`), so concurrent identical calculations
  converge on one row and an existing case is a normal answer, not an error;
  `MANUAL` rows are exempt on purpose and may repeat without limit. Never
  promote that constraint to an unconditional one. `create_entry()` and
  `create_manual_entry()` in `calculator/services.py` are the two doors, and
  manual rows reuse the same ported calculation engine — there is no second
  formula for the journal.
- The «1С» field accepts a number or a small arithmetic expression and is
  parsed by the hand-written evaluator in `calculator/expressions.py` (mirrored
  for preview in `static/js/calculator/oneC.js`). `eval()`, `exec()` and
  `new Function()` are never acceptable here. **The invariant is that
  `one_c_expression` is arithmetic in seconds and `one_c_hours` is the
  server-derived `seconds / 3600`**; the column, the cell after ✓ and the
  export are all hours. The expression is kept so ✎ can hand it back for
  editing, and the stored number is always the server's, never the preview's.
  `employee_name` is typed text, unrelated to `User`; `created_by`/`updated_by`
  stay auditing only.
- `actual_unit_time_hours` is server-derived as
  `actual_batch_time_hours / batch_quantity` in `calculator/services.py`.
  A browser-supplied value is ignored. The `.xlsx` export is built by
  `calculator/export.py` from **every** database row, confirmed or not, never
  from the tab's local state: confirmation is a production state, not an
  export gate. A missing value is written as an empty cell — never `None`,
  `null`, a stand-in `0` or an empty `<v>`.
- **Protocol numbers are per type and reusable.** Each `ProtocolType` owns its
  own series, so «Качество №1» and a future type's «№1» coexist. Deleting a
  draft frees its number, and the next protocol of that type takes the
  **smallest free positive number**, never `max + 1`: with `1, 2, 4, 5` taken
  the answer is `3`. Allocation happens only in
  `protocols.services.create_protocol()`, inside one transaction that row-locks
  the `ProtocolType` first; `unique_protocol_number_per_type` is the final
  guarantee, not the allocator. Never allocate from a signal, a form or a
  `save()` override.
- Every protocol mutation goes through `protocols/services.py`.
  `create_protocol()` is atomic by contract: the number, the `Protocol`, the
  author's `ProtocolParticipant` and the `CREATED` history event exist together
  or not at all. Participant snapshots (`display_name`, `position`,
  `department_name`) are frozen when the participant is added and never follow
  the profile afterwards; `Protocol.author` is the only authority on authorship
  — do not add an `is_author` flag. A `ProtocolSpeech` speaker is a
  `ProtocolParticipant` of the same protocol, checked in the model and in the
  service. Draft deletion is author-only and `DRAFT`-only.
- **The whole draft is one save.** `/quality/protocols/` holds the registry
  («В работе» = `DRAFT`/`APPROVAL`/`REVISION`, «Архив» = `ARCHIVED`), the type
  selection page (built from active `ProtocolType` rows — never a hard-coded
  kind) and one editor page. «Сохранить черновик» posts participants, повестка,
  «Слушали» and the task drafts together: `ProtocolDraftForm` parses and
  validates them, then `save_protocol_draft()` re-reads and row-locks the
  `Protocol`, re-checks status and permission *after* the lock, and writes every
  block in one `transaction.atomic()`. There is no autosave and no per-block
  endpoint; a refusal anywhere persists nothing from that submission. One
  successful save that actually changes stored content adds exactly one `EDITED`
  event — never one per field. «Changes stored content» is decided by
  `_document_snapshot()`, which covers every persisted column of a decision,
  `split_for_assignees` and `requires_attachment` included: a submission that
  moves nothing but an execution flag is still an edit and takes the ordinary
  `updated_at` / `EDITED` / realtime path.
- **A participant who stays keeps their snapshot.** The save reconciles
  participant rows by user: an existing row is updated in place (and re-freezes
  `department_name` only when its department really changed), a removed row is
  deleted, and only a new or re-added user gets a fresh snapshot. Rebuilding
  every row on every save would silently refresh names the archive must keep.
  Speeches are rewritten before the participants they `PROTECT`, and a speech
  whose speaker is no longer among the submitted participants is a form error,
  not an `IntegrityError`.
- Editing rights live in `protocols/permissions.py`: reading is open to every
  authenticated user, and `can_edit_protocol()` answers author-or-Admin for the
  statuses in `EDITABLE_STATUSES` — `DRAFT` and `REVISION`, so a protocol
  returned for revision is edited by exactly the same people as a draft, while
  `APPROVAL` and `ARCHIVED` are read-only. Deletion stays stricter: only the
  author, only a `DRAFT`, confirmed through the application modal.
- **`ProtocolAction` is not `tasks.Task`.** It is the decision as recorded
  inside the protocol — text, department, due date, assignees. The editor's
  «Задачи» block writes `ProtocolAction`/`ProtocolActionAssignee` only; its
  assignees need not be participants, and a protocol may contain none. `Task`
  can now point at a `Protocol` and at a `ProtocolAction` (see the task source
  types above); the real `PROTOCOL_ACTION` task is created only when the
  protocol is archived, by the finalization rule below.
- **The protocol approval state machine.** `DRAFT`/`REVISION` →
  (`send_protocol_for_approval`) → `APPROVAL` → (`approve_protocol`, last one)
  → `ARCHIVED`, or `APPROVAL` → (`return_protocol_for_revision`) → `REVISION`.
  `ARCHIVED` is terminal. Submission is **author-only** — an Admin may edit an
  allowed state but never sends someone else's document — while approving and
  returning belong to whoever holds a `PENDING` approval on the *current*
  revision.
- **Required approvers = participants with `requires_approval` ∪ every
  `ProtocolAction` assignee − the author.** One formula, one function:
  `protocols.services.collect_required_approvers()`. Users are deduplicated
  across all reasons and all actions, and `ProtocolApproval` keeps
  `required_as_participant` / `required_as_action_assignee` so a historical
  revision still says *why* someone had to sign. The author never gets an
  approval row even when assigned to a protocol task — excluded from approving,
  never from doing. `validate_protocol_for_approval()` re-reads the persisted
  protocol first (author participant, an agenda item, a speech, speakers who
  belong to it, complete actions with a usable assignee, approvers who are
  still active and have a resolvable department) and refuses with a
  `ProtocolWorkflowError` rather than writing anything partial.
- **A revision is a whole new round.** Every submission increments
  `Protocol.revision` (first submission: `0 → 1`) and creates entirely new
  `ProtocolApproval` rows and approval tasks from the *current* content.
  Approvals and tasks from earlier revisions are never deleted, never reused
  and never count towards the new round: someone who approved revision 1 signs
  revision 2 again. History is `SENT_FOR_APPROVAL` on the first submission and
  `RESENT_FOR_APPROVAL` afterwards; every workflow event carries the revision
  it belongs to, and the return comment is preserved in
  `RETURNED_FOR_REVISION`. Do not add field-by-field audit events.
- **Approval deadlines are `+2` working days**, Saturday and Sunday being the
  only non-working days — Thursday → Monday, Friday/Saturday/Sunday → Tuesday.
  It lives once, in `ecosystem.workdays.add_working_days()`; there is
  deliberately no holiday calendar, and no caller re-derives `weekday()`
  arithmetic.
- **Lock order is `Protocol.select_for_update()` first**, then approvals, then
  tasks — in every transition, without exception. Each service re-reads the
  authoritative status, actor and content *after* the lock, so stale tabs,
  double clicks and two approvers pressing at once serialize instead of racing:
  only one request can ever observe that it closed the last pending approval.
- **Approval-task lifecycle is driven only by the protocol workflow.** Tasks
  are written through `tasks.services.create_protocol_approval_task()`,
  `create_protocol_action_task()`, `complete_protocol_approval_task()` and
  `cancel_protocol_approval_task()` — never from a view, a model or a signal,
  and never directly from `protocols/services.py`. An approver's task is closed
  as `COMPLETED` with them as `completed_by` and no execution comment; the
  cancelled ones of a returned round are closed with `completed_by` left NULL,
  because nobody is going to pretend those people approved. `complete_task()`
  is unchanged and still refuses `PROTOCOL_APPROVAL`.
- **Finalization is atomic and lock-bound.** `_finalize_protocol()` is internal:
  it takes no lock of its own and may only be called while the caller already
  holds the `Protocol` lock inside the workflow transaction. It confirms no
  current-revision approval is still pending, archives the protocol, records
  `ARCHIVED`, creates exactly one `PROTOCOL_ACTION` task per `ProtocolAction`
  (text, department, due date and assignees copied from the action,
  `created_by` = the protocol author, status `IN_PROGRESS`) and records
  `TASKS_CREATED` when it created any. Any failure rolls the whole transition
  back — the protocol does not stay archived, the final approval is not
  half-committed, and no partial set of tasks survives. The `protocol_action`
  one-to-one is the database-level guarantee against a duplicate task; the
  service turns it into a controlled refusal rather than relying on the
  `IntegrityError`. A protocol nobody must approve is finalized by the
  submission itself, in that same transaction, so it never parks in `APPROVAL`.
- **The workflow endpoints are thin and POST-only.** Three routes exist —
  `protocols:send_for_approval`, `protocols:approve`, `protocols:return_for_revision`
  — one per transition, each a wrapper around `send_protocol_for_approval()`,
  `approve_protocol()` and `return_protocol_for_revision()`. A GET on any of
  them redirects to the protocol and mutates nothing; a `ProtocolWorkflowError`
  is rendered back on the protocol page. `protocols/permissions.py` only decides
  what is *rendered* — `can_send_protocol_for_approval()` for the author's
  «Отправить на согласование», `can_decide_protocol_approval()` for the two
  approver buttons — and the services stay authoritative under the row lock.
  Never restate a state-machine rule in a view, a template or JavaScript.
- **Submission posts the editor form itself.** «Отправить на согласование» posts
  the *same* fields as «Сохранить черновик» to the submission endpoint, and the
  view runs `ProtocolDraftForm` → `save_protocol_draft()` → `send_protocol_for_approval()`
  in that order, so what goes for approval is exactly what was on screen. An
  invalid form submits nothing; a draft that saves but is refused by
  `validate_protocol_for_approval()` leaves the protocol editable with the error
  shown. Do not merge the two calls, and do not add an autosave.
- **The approval UI reads through the selectors.** `get_approval_progress()`,
  `get_current_approval_rows()` and `get_approval_revision_groups()` are what the
  panel and the by-revision history render; templates never rebuild an approval
  query. Everything shown comes from `ProtocolApproval` snapshots and the stored
  `required_as_*` flags, never from the profile as it stands today, and a
  previous revision stays visible without being presented as the live round.
  The editor's «согласует как исполнитель задачи» hint is derived in
  `protocol_editor.js` for presentation only: it never writes `requires_approval`,
  which stays the author's own manual value, and `collect_required_approvers()`
  remains the only authority on who must sign.
- **An approval task always opens the protocol.** `PROTOCOL_APPROVAL` is a
  work-queue entry, so `tasks:detail` redirects it to `protocols:detail` and the
  ordinary completion UI is never rendered for it — matching `can_complete_task()`
  and `complete_task()`, which already refuse it. `PROTOCOL_ACTION` stays a
  normal task with the normal detail and completion flow, minus the act-only
  sections. Act-only blocks branch on `source_type`, never on a relation being
  non-NULL.
- **Task source presentation lives in `tasks/presentation.py`.** `describe_task_source()`
  builds the label and the `reverse()`d link («Качество №7» → the protocol, an
  act number → the act), and `describe_task_state()` answers with the
  `ProtocolApproval` decision for an approval queue entry — a task closed because
  someone else returned the protocol is `COMPLETED` as a queue row and must never
  be shown as approved. `build_task_list_state()` returns those as `rows`, so the
  full page and the live fragment stay identical. The registry's «Тип задачи»
  filter is `source_type`; the task's own workflow status is a separate column,
  and the two are never conflated again. Never hard-code a public URL in a
  template.

### Documentation library (`documents`)

- **Two models, generically named.** `DocumentFolder` (`name`, `parent` →
  `self`, `code`, `is_system`, `created_by`, timestamps) and `Document`
  (`file`, `name`, `folder`, `original_name`, `file_size`, `content_type`,
  `uploaded_by`, timestamps). Never `UserFile` or `UploadedFile`: the same
  tables are meant to carry the future attachments branch.
- **«Документация» is not a row.** It is the browse root, and it holds exactly
  two branches: «Корпоративные документы» (a system folder, `code='corporate'`)
  and «Вложения» (generated, no rows at all). Nothing else may be created at
  the root — `create_folder()` refuses `parent=None`. The initial folders
  («Инструкции», «Служебные записки», «Нормативные документы», «Обучение»,
  «Шаблоны») live inside «Корпоративные документы» and are created by
  `ensure_default_folders()`, which matches on `code` and is therefore
  idempotent.
- **Storage is its own tree.** `media/documents/library/<folder_id>/<uuid>.<ext>`,
  untouched by and untouching `acts/attachments/`, `protocols/attachments/` and
  task attachments. The stored path carries no user text; files are served only
  through `documents:document_download`, never from a media URL.
- **Two access levels, one helper.** `documents/permissions.py` decides
  everything through `can_manage_documents()`, whose role set is
  `DOCUMENT_MANAGER_ROLES` (currently `{ADMIN}`, plus a genuine superuser).
  Giving the future «Руководство» the same rights over corporate documents is
  adding `MANAGER` to that set and nothing else. Never write a bare
  `is_superuser` check in a view or a template.
- **The server enforces it.** Every management endpoint checks the permission
  *before* the HTTP method, so a typed-in URL answers 403 rather than 405.
  Hiding a button is presentation only.
- **Uploads go through `documents/validators.py`:** blocked executable suffixes
  first, then an allowlist, then 25 MB. Own policy, not
  `ecosystem/attachments.py` — an act attachment and a library document are
  different things and must not drift into one set.

#### Versions and history (corporate documents only)

- **`Document` holds no file.** It is the logical document — name, folder,
  identity. `DocumentVersion` holds the file, one row per uploaded revision,
  with `number`, `is_current`, `comment` and the copied
  `original_name`/`file_size`/`content_type`. `document.current_version` is the
  accessor; a listing must add `Document.current_version_prefetch()` or it pays
  one query per row.
- **Append-only.** `add_document_version()` allocates the next number under
  `select_for_update()` on the document, clears `is_current` on the previous
  row and inserts a new one with its own UUID path — it never overwrites,
  renames or deletes a stored file, so every earlier revision stays
  downloadable. A partial unique constraint (`documents_version_single_current`)
  is the database's own word on «exactly one current version»; a second
  constraint keeps `(document, number)` unique. `restore_document_version()`
  only moves `is_current` — it is not an edit.
- **All version work goes through `documents/services.py`.** Never create a
  `DocumentVersion` in a view or in Admin: the number, `is_current`,
  `Document.updated_at` and the history row are set together, and any one of
  them written alone leaves the document inconsistent. Both are read-only in
  Admin for that reason.
- **`DocumentHistoryEvent`** is four actions, a user, a timestamp and a
  sentence — not an audit framework. Its `document` FK is `SET_NULL` and the
  name is snapshotted, so `DOCUMENT_DELETED` survives the document it records.
  `_record_history()` swallows and logs write failures: history must not roll
  back the upload it describes.
- **The document page has two tabs**, the same `?tab=` pattern and the same
  `.act-detail-tabs` component acts and protocols use: «Документ» (header +
  viewer) and «История» (versions, the upload form, the event log). The version
  table must not return to the first tab. `?version=` selects which revision is
  on screen — a query parameter, never session state, and an unknown value
  falls back to the current version instead of 404ing.
- **Preview is `documents/preview.py`'s decision.** `INLINE_TYPES` maps an
  extension to the content type it may be served inline as; anything absent has
  no preview and the page says so. `document_version_preview` derives the type
  from that map (**never** from the stored `content_type`, which came from the
  browser) and sends `nosniff` + `Content-Security-Policy: sandbox`. No HTML and
  no SVG are ever inline, and no external viewer or JS library is loaded.
- **Where an approval workflow goes:** on `DocumentVersion` (status, approver,
  decision date, signature) — revisions are approved one at a time — plus new
  members of `DocumentHistoryEvent.Action`. Not on `Document`, and not in a new
  table. None of it is implemented.
- `document_download` (the pre-versioning URL) still works and resolves to the
  current version; `documents/migrations/0004`–`0006` created the tables, moved
  every existing document's file *pointer* into a version 1 row without
  touching MEDIA_ROOT, and then dropped the old columns.

#### Corporate documents vs system attachments

- **Corporate documents** are the library's own rows and are writable by a
  document manager. Everything above applies to them.
- **System attachments** («Вложения» → Акты / Протоколы / Задачи) are act,
  protocol and task files shown through `documents/references.py`. **They have
  no versions and no history, and never will**: the file belongs to the act,
  protocol or task that owns it, and versioning it here would fork another
  module's record. `DocumentVersion` is reachable only from `Document`.
  `DocumentReference` is a **frozen dataclass, not a table**: it is built per
  request from `ActAttachment` / `ProtocolAttachment` / `TaskAttachment`, so
  Documentation never holds a second copy of a file, a second row describing
  it, or a `DocumentFolder` for it. Never add a mirror table — a stored copy
  would have to be synchronised with three other apps and would drift on the
  first missed hook. The generated folders are views, not `DocumentFolder`
  rows.
- **Never re-implement another module's rules.** One `AttachmentSource`
  subclass per module answers four questions — readable records, record label,
  record link, download rule — and each delegates: `acts`
  `get_all_visible_acts_queryset()` / `can_download_attachment()`, `protocols`
  `get_readable_protocols_queryset()` / `can_download_protocol_attachment()`,
  `tasks` `get_readable_tasks_queryset()` / `can_download_task_attachment()`.
  A record invisible in the owning module is invisible here in the same
  request. Adding a fourth source is a subclass plus a `SOURCES` entry.
- **System attachments are immutable from Documentation, for everybody.**
  `can_modify_system_attachments()` returns False unconditionally —
  administrators and superusers included, and `can_manage_documents()` is not
  consulted. Upload-, delete- and rename-shaped URLs under `system/` are
  registered onto `system_readonly`, which answers 403 on GET and POST alike,
  so a direct attempt is refused rather than 404'd. A file is changed where it
  was uploaded, so the owning workflow writes its history event. Do not add an
  exemption; a stage that wants one changes that single function in the open.
- The reference carries a stable `(source, attachment_id)` identity and a
  `created_at`, which is the interface a later version chain, audit entry or
  approval flow attaches to. None of those is implemented.

#### Archive conveniences

- **One card for every file.** `documents/cards.py` owns `DocumentCard` (a
  frozen dataclass, not a table) and the two builders — `corporate_card()` from
  a `Document` + its current version, `reference_card()` from a
  `DocumentReference`. `templates/documents/includes/document_card.html` renders
  it in folder listings, search results, «Избранное» and «Недавние документы».
  Its only branch is `is_readonly`; never add a per-source rendering path.
  `file_icon()` picks the emoji (PDF/Word/Excel/image/text), and a system
  attachment always shows 🔒 — read-only matters more than the file type.
- **`DocumentFavorite` is a private join row** (`user`, `document`, unique
  together). Starring is a personal bookmark, not a permission:
  `can_favorite_document()` is «may read the library». `build_favorite_documents()`
  filters on `request.user` with no parameter that could widen it, and
  `DocumentFavorite.ids_for()` resolves a whole listing in one query. Corporate
  documents only — a system attachment has no `Document` row to point at.
- **«Недавние документы» means recently *uploaded/updated*.** There is no
  per-user access log and none is to be added for a shortcut block; ordering is
  `Document.updated_at`, which `add_document_version()` touches.
- **Folder deletion refuses non-empty folders.** `delete_folder()` checks the
  whole subtree and raises `DocumentError` if it holds any document or any
  subfolder. This is an archive: removing a folder must never be a way to
  destroy documents and their version history in one click. System folders are
  still undeletable, and «Вложения» is not a folder at all.
- **Only «Корпоративные документы» is structural.** `is_structural_folder()`
  names the one folder a manager may not rename or delete — it is a branch of
  the browse root, and `create_folder()` refuses `parent=None`, so removing it
  would leave nowhere to store anything. The shipped folders inside it
  («Инструкции», «Шаблоны», …) are *content*: `is_system` marks them so
  `ensure_default_folders()` can recreate them idempotently, and it grants no
  protection. **Never branch on `is_system` in a template or a view** — ask
  `can_rename_folder()`/`can_delete_folder()`; `build_folder_rows()` resolves
  both per row, because the template doing it itself is what once left an
  administrator with no actions on the shipped folders.
- **Creating and renaming a folder both go through the shared confirmation
  modal** (`data-confirm-comment="required"` with `data-confirm-comment-name="name"`).
  There is no inline name field in the toolbar.
- **Uploading is the green «+».** `DocumentUploadForm` uses
  `MultipleFileField`, so a manager picks several files at once; picking them
  *is* the action — `static/js/documents.js` fills the shared confirmation
  modal with the names and submits the form, and there is no name field
  (a selection has no single name). The upload policy is applied **per file**
  by `upload_documents()` and deliberately not in the form: a form error would
  refuse the whole selection, and nine good files should not be lost to one
  bad one. The view reports both halves.
- Inside a folder, documents render as a **four-column table** (name, size,
  date, version) with `table-layout: fixed` so a long name clips with an
  ellipsis; the card is for search results and the personal blocks, where a hit
  has to say where it came from.
- A `<td>` that holds row actions keeps `display: table-cell`; the flex row
  goes on an inner `div.doc-actions`. Putting `display: flex` on the cell
  pulls the column out of the table layout.
- Django's `{# … #}` is **single-line only** — a multi-line one renders as page
  text. Use `{% comment %}` for anything longer.

#### Search (`documents/search/`)

- **A package, in dependency order:** `types.py` (the scope constants;
  `SearchResult` is an alias of `documents.cards.DocumentCard`) →
  `services.py` (what matches, and what is recent) → `selectors.py` (what the
  page renders). `documents/search/__init__.py`
  re-exports the public names; import from `documents.search`, never from a
  submodule. `documents/selectors.py` is separate and belongs to the *browser*
  — breadcrumbs and «Недавние документы» — so a search backend change cannot
  reach the navigation.
- **One search over both halves**, at `/documents/search/`. Corporate
  documents match on document name, stored filename and folder name; system
  attachments match on the filename *or* on the record that owns them, through
  each source's `record_search_filter()` (act number/nomenclature, protocol
  type + number, task id/text) — so an act number finds that act's photographs.
- **Unified results.** `SearchResult` is a frozen dataclass — never a table,
  never an index — carrying title, `document_type`, source, path, `open_url`,
  `download_url` and `can_download`. Both kinds render through
  `templates/documents/includes/result_card.html`, whose only branch is
  `is_readonly`: there is no per-source rendering path, and adding one is a
  regression. The «Недавние документы» block reuses the same card.
- **`DocumentReference` stays the only system-attachment shape.** Search does
  not query `ActAttachment`/`ProtocolAttachment`/`TaskAttachment` directly — it
  calls each source's `search()`/`recent()`, which build references. Nothing is
  mirrored, indexed or copied.
- The search runs **unscoped once** and is narrowed in Python
  (`filter_by_scope()`), because every chip needs a count. Results are capped
  per source (`RESULT_LIMIT`) and a term shorter than `MIN_QUERY_LENGTH` is
  «no search», not «no results». «Недавние» means recently *uploaded*: there is
  no per-user access log and a shortcut block is not a reason to add one.
- **Search grants nothing.** Corporate hits go through `can_view_documents()`,
  system hits through each source's readable queryset, so a result set is
  always a subset of what the same user could reach by clicking. Never add a
  visibility rule here.
- Full-text ranking, a PDF text index, OCR or metadata filters replace
  `_search_corporate()` and each source's `search()` and keep `types.py` and
  the view. Do not push matching into a view.
- `documents/selectors.py:build_breadcrumbs()` is the one trail builder; every
  item is a link, the current one included, and
  `templates/documents/includes/breadcrumbs.html` renders it on all three page
  types.

## Security and permissions

- Never rely on a template check, a username, or a URL not being guessed: the
  `/quality/acts/clear-all/` route is not registered unless `ENABLE_DEMO_RESET` is on,
  and production forces it off. Notification pages and POST actions always
  scope objects to `request.user`.
- The SSE endpoint and `/realtime/sync/` derive identity from the session only:
  no query string, path or body may influence the subscription. Technical
  endpoints use `realtime.auth.realtime_login_required` (a JSON 401), not the
  HTML `login_required` redirect.
- Secrets — `SECRET_KEY`, `DB_PASSWORD`, `EMAIL_HOST_PASSWORD`, the Redis URL —
  come only from the environment and never appear in an error message, a check
  message, the browser config or a log line.
- Upload validation checks size and extension — one policy in
  `ecosystem/attachments.py`, imported by acts and protocols alike, never a
  second copy. Act attachment deletion is limited to the uploader, a manager or
  an administrator; the protocol rule is its own, below.
- An inactive `UserProfile` grants no application role; only Django's genuine
  `is_superuser` fallback remains independent of the profile.
- **A `UserProfile` may be absent, and reading one is always guarded.** The row
  is deletable in Admin on its own while the `User` behind it is held by
  `PROTECT` from `ProtocolActionAssignee`, so `user.userprofile` can raise
  `RelatedObjectDoesNotExist`. Reach it through
  `getattr(user, 'userprofile', None)` — the guard belongs on `userprofile`
  itself, never only on the attribute after it — and treat a missing profile as
  an empty department and no role. A page must degrade to a blank department,
  never to a 500.
- `AUTH_PASSWORD_VALIDATORS` is intentionally empty: this is an internal system
  whose accounts an administrator creates in Django Admin, and a password may
  equal the surname it belongs to. Only *strength* validation is off — hashing,
  `set_password()`, the authentication backend, sessions and CSRF are unchanged,
  passwords are still stored hashed and never logged. Do not re-add a validator
  or work around the policy inside a single Admin form.
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
- «Проработка» mutations emit invalidation-style events (`workup.created`,
  `workup.updated`, `workup.deleted`) from `calculator/services.py`, never from
  signals, and only when a row actually changed — a deduplicated `get_or_create`
  hit emits nothing. The audience is every active account, because every
  authenticated user may read the journal. The client refetches the rows through
  the ordinary `calculator:entry_list` GET and hands them to the calculator
  controller; realtime never renders the table or carries journal values.
- A live refresh never replaces a form holding unsaved input: only read-only
  blocks are swapped, and a dirty form gets the conflict banner with the typed
  text intact. The protocol page follows the act page exactly: `protocols.js`
  guards the content block, and `protocol_editor.js` is a repeatable
  initialiser registered with `qualityFragments` so replaced markup re-binds
  through the same code — no business rule moved into the browser. Recovery has one owner per authenticated session — every periodic request is gated
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
