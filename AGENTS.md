# Agent Notes

## Project Direction

- Preserve the project concept: the first system is a quality ecosystem.
- Treat digital OTK and micro-MES as future stages.
- Use Russian UI labels by default.
- New major business areas should be separate Django apps.
- Use the standard Django User model until explicitly asked otherwise.
- Do not create a custom user model in later patches without explicit approval.
- `UserProfile.role` is the source for MVP role awareness.
- Reference data belongs in the `references` app.
- Use reference models instead of free-text fields in future business models where possible.
- Acts belong in the `acts` app.
- Act history events belong in the `acts` module.
- Act attachments belong in the `acts` module.
- Use `references.Operation`, `DefectType`, `ActStatus`, and `Priority` in act models/forms.
- `UserProfile.role` is used for MVP act visibility.
- Regular users see only acts currently assigned to their processing stage.
- Act visibility must be enforced in backend permissions, not only templates.
- Act workflow transitions belong in `acts/services.py`.
- Act closing belongs in `acts/services.py`.
- Workflow services should record history when act status changes.
- Comments are manual user notes and should not replace workflow history.
- Act history should be append-only from normal UI flow.
- Attachment actions should record history when history models are available.
- Act role and action checks belong in `acts/permissions.py`.
- Closing permissions belong in `acts/permissions.py`.

## Patch Rules

- Keep patches small and staged.
- D2 is structural only; do not add business models or workflows here.
- Do not implement business logic before the planned stage.
- Demo users and demo passwords are for local development only.
- Seed commands must be idempotent and safe to run multiple times.
- Do not implement a real access-control matrix before the relevant business module patch.
- Do not add custom CRUD for references until explicitly requested.
- Do not implement task objects inside acts; tasks belong to a later `tasks` module patch.
- Keep act workflow simple until tasks/protocols are implemented.
- Views must not duplicate act workflow business logic.
- Act UI must use service-provided available actions.
- Act templates must not duplicate role/status permission logic.
- UI patches must not change workflow transitions unless explicitly requested.
- Downloads for protected act files must go through access-checked Django views.
- Direct media links should not be used for protected act files.
- Upload validation must check file size and extension.
- Attachment delete permission is author, manager, or admin.
- Closed acts are read-only in the normal workflow.
- Print view is HTML/browser-print only until an explicit export request.
- PDF/Word export must not be added without explicit request.
- D8 intentionally uses manual validation instead of automated tests.
- D10 intentionally uses manual validation instead of automated tests.
- Templates must not decide act permissions directly.
- All act workflow actions must be covered by tests.
- Do not add backend complexity before it is needed.
- Do not add frontend frameworks.
- Keep navigation server-rendered unless a later patch asks for frontend behavior.
- Do not add realtime or WebSocket features yet.
- Do not add PostgreSQL configuration until the database stage is requested.
- Keep local development beginner-friendly.
