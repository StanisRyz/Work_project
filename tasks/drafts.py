"""The half-written «Выполнение» text, parked in the session for one redirect.

Uploading or removing a file is its own request and its own round trip, and
before this the trip threw the comment away: the user came back to an empty
textarea and retyped what they had already written. The draft is not a
comment and never becomes one — `complete_task()` still saves only what is
posted to it — it is only what the next page puts back into the field. Keyed
by task, so a draft can never surface on a different task, and popped on first
read, so it survives one navigation and no more.

Only the task page reads it. A `BOARD` task has no «Выполнение» field on its
board — its result is typed into «Завершить» in the card drawer's heading —
so nothing there parks or takes a draft.
"""

EXECUTION_DRAFT_SESSION_KEY = 'task_execution_draft'


def remember_execution_draft(request, task, execution_comment):
    """Carry an unsaved execution comment across the redirect."""
    text = (execution_comment or '').strip()
    if not text:
        # Nothing typed: clear any stale draft rather than keeping the old one
        # alive, so an emptied textarea stays empty after the upload.
        request.session.pop(EXECUTION_DRAFT_SESSION_KEY, None)
        return
    request.session[EXECUTION_DRAFT_SESSION_KEY] = {'task': task.pk, 'text': text}


def take_execution_draft(request, task):
    """Pop this task's parked draft, if the previous request left one."""
    draft = request.session.get(EXECUTION_DRAFT_SESSION_KEY)
    if not isinstance(draft, dict) or draft.get('task') != task.pk:
        return ''
    del request.session[EXECUTION_DRAFT_SESSION_KEY]
    return draft.get('text') or ''
