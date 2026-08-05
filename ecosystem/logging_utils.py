"""Operational logging primitives: request context, safe events, redaction.

Three pieces, deliberately small and dependency-free:

* **request context** — a `ContextVar` pair carrying `request_id` and `user_id`
  through whatever the request touches, so a service log line written deep
  inside `acts.services` can be joined to the HTTP request that caused it.
  ContextVars are the only mechanism that works correctly under ASGI, where a
  thread may serve many concurrent requests.
* **`log_event()`** — one line per event, `key=value` fields in the order given,
  every value flattened and escaped. It deliberately refuses nested payloads:
  the moment a dict or a model instance can be logged, business text starts
  leaking into log files.
* **`SensitiveValueRedactionFilter`** — the last line of defence. Everything a
  call site formats still passes through it, so a secret that reaches a log
  record by accident (an exception message quoting a connection URL, a header
  echoed into a warning) is masked before it is written anywhere.

**What must never be logged**, by any call site: `SECRET_KEY`, `DB_PASSWORD`,
`EMAIL_HOST_PASSWORD`, a Redis URL with credentials, session cookies, CSRF
tokens, comment text, defect descriptions, KO decision text, root causes,
customer or party data, recipient email addresses, message subjects and bodies,
uploaded file names and their content. Log identifiers, codes, counts,
durations and outcomes — nothing a person wrote and nothing that authenticates.
"""

import logging
import re
import uuid
from contextvars import ContextVar
from enum import Enum


# --------------------------------------------------------------------------
# Request context
#
# Set by `RequestLoggingMiddleware` and read by `RequestContextFilter`. Every
# setter returns the token its caller must hand back to the matching reset, so
# a nested or concurrent request can never inherit another one's identity.
# --------------------------------------------------------------------------

_request_id_var = ContextVar('ecosystem_request_id', default=None)
_user_id_var = ContextVar('ecosystem_user_id', default=None)

MISSING = '-'


def new_request_id():
    """A fresh internal request id. Never derived from client input."""
    return uuid.uuid4().hex


def set_request_context(*, request_id=None, user_id=None):
    """Bind this context's request id and user id; returns the reset tokens."""
    return (
        _request_id_var.set(request_id),
        _user_id_var.set(user_id),
    )


def reset_request_context(tokens):
    """Restore the previous context. Always call this from a `finally` block."""
    request_token, user_token = tokens
    try:
        _request_id_var.reset(request_token)
    except ValueError:
        # The token belongs to a different context — possible when a caller
        # spans contexts. Falling back to an explicit clear is still correct.
        _request_id_var.set(None)
    try:
        _user_id_var.reset(user_token)
    except ValueError:
        _user_id_var.set(None)


def get_request_id():
    return _request_id_var.get()


def get_user_id():
    return _user_id_var.get()


# --------------------------------------------------------------------------
# Structured single-line events
# --------------------------------------------------------------------------

_CONTROL_CHARACTERS = re.compile(r'[\x00-\x1f\x7f]')

# Anything richer than these would let a whole model, a form payload or a
# rendered fragment into a log file through a single careless call.
_ALLOWED_VALUE_TYPES = (str, int, float, bool, uuid.UUID, Enum, type(None))


def _format_value(value):
    """Flatten one field value to a safe, single-line token."""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        # Before the int branch: bool is a subclass of int.
        return 'true' if value else 'false'
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, uuid.UUID):
        return value.hex
    if isinstance(value, float):
        # Deterministic width; durations are the main float here.
        return f'{value:.1f}'
    if isinstance(value, int):
        return str(value)
    text = str(value)
    # Escape first, then collapse: a literal "\n" in the source text must not
    # be indistinguishable from an escaped newline.
    text = text.replace('\\', '\\\\')
    text = text.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    text = _CONTROL_CHARACTERS.sub('', text)
    if ' ' in text or '"' in text:
        text = '"' + text.replace('"', '\\"') + '"'
    return text or MISSING


def format_event(event, **fields):
    """Render `event key=value ...` deterministically, in the order given."""
    parts = [str(event)]
    for key, value in fields.items():
        if not isinstance(value, _ALLOWED_VALUE_TYPES):
            # A dict, list, model or form would smuggle business content into
            # the log. Record the type so the bad call site is findable, but
            # never the value itself.
            value = f'<unsupported:{type(value).__name__}>'
        parts.append(f'{key}={_format_value(value)}')
    return ' '.join(parts)


def log_event(logger, level, event, *, exc_info=False, **fields):
    """Write one structured operational event.

    `event` is a stable dotted name (`workflow.transition_completed`), and
    `fields` are safe scalars only — identifiers, codes, counts, durations and
    outcomes. Passing a dict, a model instance or any other container is a bug
    and is rendered as `<unsupported:...>` rather than serialized.
    """
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    logger.log(level, format_event(event, **fields), exc_info=exc_info)


# --------------------------------------------------------------------------
# Logging filters
# --------------------------------------------------------------------------

class RequestContextFilter:
    """Attach `request_id` / `user_id` to every record the formatter needs.

    A record may already carry them (a call site that knows better); otherwise
    they come from the ContextVars, and finally from `-`. No value is ever
    derived from a cookie, header or request body.
    """

    def __init__(self, name=''):
        self.name = name

    def filter(self, record):
        if not getattr(record, 'request_id', None):
            record.request_id = get_request_id() or MISSING
        if not getattr(record, 'user_id', None):
            user_id = get_user_id()
            record.user_id = MISSING if user_id is None else str(user_id)
        return True


# Kept as an alias so an existing `ecosystem.settings._SafeContextFilter`
# reference in a deployment's own configuration keeps working.
SafeContextFilter = RequestContextFilter


REDACTED = '[redacted]'

# Credentials inside a URL: redis://user:password@host, postgres://…, amqp://…
_URL_CREDENTIALS = re.compile(r'(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/\s@]+)@')

# Header-shaped values that must never survive in a message, whatever produced
# them. The header name stays visible so the log still says *what* was dropped.
#
# The value is consumed up to the next `;` or `,` rather than to the next
# space, because `Authorization: Bearer <token>` puts the actual credential
# *after* a space — masking only the first word would leave the token exposed.
_HEADER_VALUES = re.compile(
    r'(?i)\b(authorization|cookie|set-cookie|x-csrftoken|csrftoken|csrfmiddlewaretoken|'
    r'sessionid|proxy-authorization)\b(\s*[:=]\s*)(?P<value>"[^"]*"|\'[^\']*\'|[^;,\n]+)'
)

# A short value is not a credential worth masking — masking "1" or "on" would
# corrupt ordinary technical output while protecting nothing.
MIN_REDACTABLE_SECRET_LENGTH = 8


class SensitiveValueRedactionFilter:
    """Mask configured secrets and credential-shaped substrings in a record.

    Applied to *every* application handler, so it also covers third-party and
    Django-internal records — an exception message quoting a connection string
    is exactly the case a per-call-site rule would miss.

    Only genuinely secret, sufficiently long configured values are masked;
    ordinary identifiers, status codes and counts pass through untouched.
    """

    def __init__(self, name=''):
        self.name = name
        self._cached_secrets = None
        self._cached_signature = None

    # -- configured secrets

    def _secret_values(self):
        """Current non-empty secret settings, longest first.

        Resolved lazily and re-resolved when the settings change, so
        `override_settings` in a test and a reconfigured deployment are both
        handled without importing settings at module import time.
        """
        from django.conf import settings

        signature = tuple(
            str(getattr(settings, name, '') or '')
            for name in (
                'SECRET_KEY',
                'EMAIL_HOST_PASSWORD',
                'REALTIME_REDIS_URL',
            )
        ) + (str((settings.DATABASES.get('default') or {}).get('PASSWORD', '') or ''),)

        if signature == self._cached_signature:
            return self._cached_secrets

        secrets = set()
        for value in signature:
            if len(value) >= MIN_REDACTABLE_SECRET_LENGTH:
                secrets.add(value)
        # The Redis URL's password on its own, so a partially quoted URL is
        # still masked even when the whole URL is not present verbatim.
        redis_url = str(getattr(settings, 'REALTIME_REDIS_URL', '') or '')
        match = _URL_CREDENTIALS.search(redis_url)
        if match:
            userinfo = match.group('userinfo')
            if ':' in userinfo:
                password = userinfo.split(':', 1)[1]
                if len(password) >= MIN_REDACTABLE_SECRET_LENGTH:
                    secrets.add(password)

        self._cached_signature = signature
        # Longest first: masking a longer secret that contains a shorter one
        # must not leave the tail of the longer value behind.
        self._cached_secrets = sorted(secrets, key=len, reverse=True)
        return self._cached_secrets

    # -- redaction

    def redact(self, text):
        if not text:
            return text
        for secret in self._secret_values():
            if secret in text:
                text = text.replace(secret, REDACTED)
        text = _URL_CREDENTIALS.sub(lambda m: f'{m.group("scheme")}{REDACTED}@', text)
        text = _HEADER_VALUES.sub(lambda m: f'{m.group(1)}{m.group(2)}{REDACTED}', text)
        return text

    def filter(self, record):
        # `record.args` are redacted before formatting, so a secret passed as a
        # parameter is masked even though it is not yet part of the message.
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: self.redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self.redact(value) if isinstance(value, str) else value
                    for value in record.args
                )
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        return True


class SafeFormatter(logging.Formatter):
    """The file/console formatter, with redaction applied after formatting too.

    A stack trace is rendered by the formatter, *after* every filter has run,
    so an exception whose message or a frame's local repr contains a secret
    would otherwise reach the file unmasked. ERROR records keep their full
    traceback — only the values inside it are masked.
    """

    _redactor = SensitiveValueRedactionFilter()

    def format(self, record):
        return self._redactor.redact(super().format(record))


# --------------------------------------------------------------------------
# Configuration inspection
#
# One description of the current logging setup, shared by the system checks,
# `check_production_readiness` and `manage.py check_logging`, so the three can
# never disagree about whether logging is usable.
# --------------------------------------------------------------------------

def describe_logging_configuration():
    """Return a safe summary of the effective logging configuration.

    Read-only: it stats the log directory but writes nothing. `file_path` is
    included for a local operator; callers that publish a report (JSON) must
    use `file_path_configured` instead and leave the path itself out.
    """
    import os
    from pathlib import Path

    from django.conf import settings

    to_file = bool(getattr(settings, 'LOG_TO_FILE', False))
    to_console = bool(getattr(settings, 'LOG_TO_CONSOLE', True))
    raw_path = getattr(settings, 'LOG_FILE_PATH', None)
    path = Path(raw_path) if raw_path else None

    summary = {
        'to_file': to_file,
        'to_console': to_console,
        'level': str(getattr(settings, 'LOG_LEVEL', '')),
        'realtime_level': str(getattr(settings, 'REALTIME_LOG_LEVEL', '')),
        'max_bytes': int(getattr(settings, 'LOG_FILE_MAX_BYTES', 0) or 0),
        'backup_count': int(getattr(settings, 'LOG_FILE_BACKUP_COUNT', 0) or 0),
        'file_path': str(path) if path else '',
        'file_path_configured': bool(path),
        'file_path_absolute': bool(path and path.is_absolute()),
        'file_exists': False,
        'directory_exists': False,
        'writable': False,
        'collides_with_static_or_media': False,
    }

    if not to_file or path is None:
        return summary

    directory = path.parent
    summary['directory_exists'] = directory.is_dir()
    summary['file_exists'] = path.is_file()
    if summary['file_exists']:
        summary['writable'] = os.access(path, os.W_OK)
    elif summary['directory_exists']:
        # The handler creates the file itself; a writable directory is enough.
        summary['writable'] = os.access(directory, os.W_OK)

    for setting_name in ('STATIC_ROOT', 'MEDIA_ROOT'):
        root = getattr(settings, setting_name, None)
        if not root:
            continue
        try:
            resolved_root = Path(root).resolve()
            # A log file under STATIC_ROOT would be published by the web
            # server; under MEDIA_ROOT it would sit among act attachments.
            if path.resolve() == resolved_root or resolved_root in path.resolve().parents:
                summary['collides_with_static_or_media'] = True
        except (OSError, ValueError):
            continue

    return summary
