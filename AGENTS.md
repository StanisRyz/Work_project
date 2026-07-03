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

## Patch Rules

- Keep patches small and staged.
- D2 is structural only; do not add business models or workflows here.
- Do not implement business logic before the planned stage.
- Demo users and demo passwords are for local development only.
- Seed commands must be idempotent and safe to run multiple times.
- Do not implement a real access-control matrix before the relevant business module patch.
- Do not add custom CRUD for references until explicitly requested.
- Do not add backend complexity before it is needed.
- Do not add frontend frameworks.
- Keep navigation server-rendered unless a later patch asks for frontend behavior.
- Do not add realtime or WebSocket features yet.
- Do not add PostgreSQL configuration until the database stage is requested.
- Keep local development beginner-friendly.
