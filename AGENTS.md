# Agent Notes

## Project Direction

- Preserve the project concept: the first system is a quality ecosystem.
- Treat digital OTK and micro-MES as future stages.
- Use Russian UI labels by default.
- New major business areas should be separate Django apps.

## Patch Rules

- Keep patches small and staged.
- D2 is structural only; do not add business models or workflows here.
- Do not implement business logic before the planned stage.
- Do not add backend complexity before it is needed.
- Do not add frontend frameworks.
- Keep navigation server-rendered unless a later patch asks for frontend behavior.
- Do not add realtime or WebSocket features yet.
- Do not add PostgreSQL configuration until the database stage is requested.
- Keep local development beginner-friendly.
