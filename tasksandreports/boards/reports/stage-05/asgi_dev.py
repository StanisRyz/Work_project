"""Uvicorn for the browser check: the project's ASGI app plus static files.

`ecosystem.asgi` serves no static files (production has its own web server),
and `runserver` is WSGI, which does not hold an SSE stream. This wrapper adds
Django's own static handler for the local check only:

    python -m uvicorn --app-dir tasksandreports/boards/reports/stage-05 asgi_dev:application
"""

from ecosystem.asgi import application as project_application  # sets Django up first

from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402

application = ASGIStaticFilesHandler(project_application)
