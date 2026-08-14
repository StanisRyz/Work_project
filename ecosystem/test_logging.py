"""Operational logging: redaction, request context, file handler, business events.

The tests that matter most here are the negative ones. A log file is read by
people who are not the people who wrote the code, is copied into tickets, and
is handed to an IT service — so «the secret is absent» and «the user's text is
absent» are the assertions worth having, more than any assertion about a
particular wording.
"""

import logging
import logging.handlers
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from ecosystem import checks as deployment_checks
from ecosystem.logging_utils import (
    REDACTED,
    RequestContextFilter,
    SafeFormatter,
    SensitiveValueRedactionFilter,
    describe_logging_configuration,
    format_event,
    get_request_id,
    get_user_id,
    log_event,
    reset_request_context,
    set_request_context,
)
from ecosystem.middleware import REQUEST_ID_HEADER


REDIS_URL_WITH_PASSWORD = 'redis://app:s3cr3t-redis-password@redis.internal:6379/0'
POSTGRES_URL_WITH_PASSWORD = 'postgresql://quality:pg-secret-password@db.internal:5432/quality'
TEST_SECRET_KEY = 'x7Qw9zLp2mR4tYv8bN3kJ6hG1sD5fA0cE7uI9oP4rT2wX6yZ8aB3'


def _record(message, *args, level=logging.INFO):
    return logging.LogRecord('test', level, __file__, 1, message, args, None)


class EventFormattingTests(SimpleTestCase):
    def test_fields_keep_the_order_they_were_given(self):
        rendered = format_event('workflow.transition_completed', act_id=7, action='send_to_ko')

        self.assertEqual(rendered, 'workflow.transition_completed act_id=7 action=send_to_ko')

    def test_newlines_and_control_characters_are_escaped(self):
        rendered = format_event('probe', note='first\nsecond\ttab\rreturn')

        self.assertNotIn('\n', rendered)
        self.assertNotIn('\r', rendered)
        self.assertNotIn('\t', rendered)
        self.assertIn('\\n', rendered)

    def test_scalars_are_converted_safely(self):
        import uuid
        from enum import Enum

        class Colour(Enum):
            RED = 'red'

        identifier = uuid.uuid4()
        rendered = format_event(
            'probe', flag=True, off=False, count=3, ratio=1.25, kind=Colour.RED,
            identifier=identifier, missing=None,
        )

        self.assertIn('flag=true', rendered)
        self.assertIn('off=false', rendered)
        self.assertIn('count=3', rendered)
        self.assertIn('ratio=1.2', rendered)
        self.assertIn('kind=red', rendered)
        self.assertIn(f'identifier={identifier.hex}', rendered)
        self.assertIn('missing=-', rendered)

    def test_a_nested_payload_is_refused_rather_than_serialized(self):
        # The guard that stops a whole model or form from reaching a log file
        # through one careless call.
        rendered = format_event('probe', payload={'comment': 'секретный текст'})

        self.assertNotIn('секретный текст', rendered)
        self.assertIn('<unsupported:dict>', rendered)

    def test_a_value_containing_spaces_is_quoted(self):
        self.assertIn('note="two words"', format_event('probe', note='two words'))


class RedactionFilterTests(SimpleTestCase):
    def setUp(self):
        self.filter = SensitiveValueRedactionFilter()

    @override_settings(SECRET_KEY=TEST_SECRET_KEY)
    def test_a_secret_in_the_message_is_masked(self):
        record = _record(f'startup used key {TEST_SECRET_KEY}')

        self.filter.filter(record)

        self.assertNotIn(TEST_SECRET_KEY, record.msg)
        self.assertIn(REDACTED, record.msg)

    @override_settings(SECRET_KEY=TEST_SECRET_KEY)
    def test_a_secret_in_positional_args_is_masked(self):
        record = _record('configured with %s', TEST_SECRET_KEY)

        self.filter.filter(record)

        self.assertNotIn(TEST_SECRET_KEY, record.getMessage())

    @override_settings(SECRET_KEY=TEST_SECRET_KEY)
    def test_a_secret_in_dict_args_is_masked(self):
        record = _record('configured with %(key)s', {'key': TEST_SECRET_KEY})

        self.filter.filter(record)

        self.assertNotIn(TEST_SECRET_KEY, record.getMessage())

    def test_redis_url_credentials_are_masked(self):
        record = _record(f'cannot reach {REDIS_URL_WITH_PASSWORD}')

        self.filter.filter(record)

        self.assertNotIn('s3cr3t-redis-password', record.msg)
        self.assertIn(REDACTED, record.msg)
        # The host stays visible: it is what makes the message diagnostic.
        self.assertIn('redis.internal', record.msg)

    def test_postgresql_url_credentials_are_masked(self):
        record = _record(f'connection failed for {POSTGRES_URL_WITH_PASSWORD}')

        self.filter.filter(record)

        self.assertNotIn('pg-secret-password', record.msg)
        self.assertIn('db.internal', record.msg)

    def test_an_authorization_header_value_is_masked(self):
        record = _record('rejected request Authorization: Bearer abcdef123456token')

        self.filter.filter(record)

        self.assertNotIn('abcdef123456token', record.msg)
        # The header name survives, so the log still says what was dropped.
        self.assertIn('Authorization', record.msg)

    def test_a_cookie_value_is_masked(self):
        record = _record('inbound Cookie=sessionid-abc123def456 rest')

        self.filter.filter(record)

        self.assertNotIn('sessionid-abc123def456', record.msg)

    def test_a_csrf_token_value_is_masked(self):
        record = _record('csrftoken: 9f8e7d6c5b4a3210zyxw')

        self.filter.filter(record)

        self.assertNotIn('9f8e7d6c5b4a3210zyxw', record.msg)

    @override_settings(EMAIL_HOST_PASSWORD='smtp-service-password')
    def test_the_smtp_password_is_masked(self):
        record = _record('SMTPAuthenticationError for smtp-service-password')

        self.filter.filter(record)

        self.assertNotIn('smtp-service-password', record.msg)

    def test_ordinary_safe_text_is_left_intact(self):
        original = (
            'workflow.transition_completed act_id=42 action=send_to_ko '
            'previous_status=CREATED_OTK next_status=KO_REVIEW duration_ms=12.5 outcome=ok'
        )
        record = _record(original)

        self.filter.filter(record)

        self.assertEqual(record.msg, original)

    def test_a_short_configured_value_is_not_treated_as_a_secret(self):
        # Masking a value like "on" or "5432" would corrupt ordinary technical
        # output while protecting nothing.
        with override_settings(EMAIL_HOST_PASSWORD='abc'):
            record = _record('port=5432 mode=abc status=ok')
            self.filter.filter(record)

        self.assertEqual(record.msg, 'port=5432 mode=abc status=ok')

    @override_settings(SECRET_KEY=TEST_SECRET_KEY)
    def test_a_stack_trace_is_kept_but_its_secrets_are_masked(self):
        formatter = SafeFormatter('%(message)s')
        try:
            raise RuntimeError(f'connection string {POSTGRES_URL_WITH_PASSWORD}')
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                'test', logging.ERROR, __file__, 1, 'failed', None, sys.exc_info()
            )

        rendered = formatter.format(record)

        # The trace survives — that is the point of an ERROR record.
        self.assertIn('Traceback', rendered)
        self.assertIn('RuntimeError', rendered)
        self.assertNotIn('pg-secret-password', rendered)


class RequestContextTests(SimpleTestCase):
    def tearDown(self):
        # Never leave a bound context behind for the next test.
        reset_request_context(set_request_context(request_id=None, user_id=None))

    def test_the_filter_falls_back_to_a_placeholder(self):
        record = _record('probe')

        RequestContextFilter().filter(record)

        self.assertEqual(record.request_id, '-')
        self.assertEqual(record.user_id, '-')

    def test_the_filter_reads_the_bound_context(self):
        tokens = set_request_context(request_id='abc123', user_id=7)
        try:
            record = _record('probe')
            RequestContextFilter().filter(record)
        finally:
            reset_request_context(tokens)

        self.assertEqual(record.request_id, 'abc123')
        self.assertEqual(record.user_id, '7')

    def test_reset_restores_the_previous_context(self):
        tokens = set_request_context(request_id='outer', user_id=1)
        inner = set_request_context(request_id='inner', user_id=2)
        self.assertEqual(get_request_id(), 'inner')

        reset_request_context(inner)
        self.assertEqual(get_request_id(), 'outer')
        self.assertEqual(get_user_id(), 1)

        reset_request_context(tokens)
        self.assertIsNone(get_request_id())


class RequestLoggingMiddlewareTests(TestCase):
    """Volume policy and context handling, driven through the real stack."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('logging_probe', password='probe-password-1')

    def test_every_response_carries_a_request_id(self):
        response = self.client.get(reverse('health_live'))

        self.assertIn(REQUEST_ID_HEADER, response)
        self.assertTrue(response[REQUEST_ID_HEADER])

    def test_request_ids_are_unique_per_request(self):
        first = self.client.get(reverse('health_live'))[REQUEST_ID_HEADER]
        second = self.client.get(reverse('health_live'))[REQUEST_ID_HEADER]

        self.assertNotEqual(first, second)

    def test_an_incoming_request_id_header_is_never_trusted(self):
        # A client-chosen value could deliberately collide with another
        # request's id, making the log unusable exactly when it matters.
        response = self.client.get(
            reverse('health_live'), headers={'x-request-id': 'client-supplied-value'}
        )

        self.assertNotEqual(response[REQUEST_ID_HEADER], 'client-supplied-value')

    def test_the_context_is_cleared_after_the_response(self):
        self.client.get(reverse('health_live'))

        self.assertIsNone(get_request_id())
        self.assertIsNone(get_user_id())

    def test_a_mutating_request_is_logged_with_the_user_id(self):
        self.client.force_login(self.user)

        with self.assertLogs('ecosystem.request', level=logging.INFO) as captured:
            self.client.post(reverse('accounts:logout'))

        logged = '\n'.join(captured.output)
        self.assertIn('http.request', logged)
        self.assertIn('method=POST', logged)
        self.assertIn(f'user_id={self.user.pk}', logged)
        # Never the username.
        self.assertNotIn('logging_probe', logged)

    def test_an_ordinary_fast_get_is_not_logged(self):
        self.client.force_login(self.user)

        with self.assertNoLogs('ecosystem.request', level=logging.INFO):
            self.client.get(reverse('acts:list'))

    def test_a_slow_get_is_logged_as_a_warning(self):
        self.client.force_login(self.user)

        # Any request is "slow" against a zero threshold.
        with override_settings(LOG_SLOW_REQUEST_MS=0):
            with self.assertLogs('ecosystem.request', level=logging.WARNING) as captured:
                self.client.get(reverse('acts:list'))

        self.assertIn('outcome=slow', '\n'.join(captured.output))

    def test_a_client_error_is_logged_even_for_a_get(self):
        self.client.force_login(self.user)

        with self.assertLogs('ecosystem.request', level=logging.WARNING) as captured:
            self.client.get('/acts/999999/')

        self.assertIn('outcome=client_error', '\n'.join(captured.output))

    def test_health_requests_are_excluded_by_default(self):
        with self.assertNoLogs('ecosystem.request', level=logging.INFO):
            self.client.get(reverse('health_live'))
            self.client.get(reverse('health_ready'))

    def test_health_requests_can_be_enabled_for_diagnosis(self):
        with override_settings(LOG_HEALTH_REQUESTS=True, LOG_SLOW_REQUEST_MS=0):
            with self.assertLogs('ecosystem.request', level=logging.WARNING) as captured:
                self.client.get(reverse('health_live'))

        self.assertIn('health', '\n'.join(captured.output))

    def test_mutating_request_logging_can_be_switched_off(self):
        self.client.force_login(self.user)

        with override_settings(LOG_MUTATING_REQUESTS=False):
            with self.assertNoLogs('ecosystem.request', level=logging.INFO):
                self.client.post(reverse('accounts:logout'))

    def test_the_sse_endpoint_is_not_treated_as_a_slow_request(self):
        from ecosystem.middleware import STREAMING_PATHS, _log_request

        request = RequestFactory().get(STREAMING_PATHS[0])
        request.resolver_match = None
        response = mock.Mock(status_code=200)

        with override_settings(LOG_SLOW_REQUEST_MS=1):
            with self.assertNoLogs('ecosystem.request', level=logging.WARNING):
                # A stream stays open for minutes by design; its lifecycle is
                # logged by the realtime logger, keyed by connection id.
                _log_request(request, response, duration_ms=600_000, request_id='x', user_id=1)

    def test_an_exception_is_logged_with_the_request_id_and_a_stack_trace(self):
        from ecosystem.middleware import RequestLoggingMiddleware

        def boom(request):
            raise RuntimeError('exploded')

        middleware = RequestLoggingMiddleware(boom)
        request = RequestFactory().get('/acts/')
        request.user = self.user

        with self.assertLogs('ecosystem.request', level=logging.ERROR) as captured:
            with self.assertRaises(RuntimeError):
                middleware(request)

        logged = '\n'.join(captured.output)
        self.assertIn('http.request_failed', logged)
        self.assertIn('request_id=', logged)
        self.assertIn('Traceback', logged)
        self.assertIn('RuntimeError', logged)

    def test_no_query_string_or_body_ever_reaches_the_log(self):
        self.client.force_login(self.user)

        with override_settings(LOG_SLOW_REQUEST_MS=0):
            with self.assertLogs('ecosystem.request', level=logging.WARNING) as captured:
                self.client.get(reverse('acts:list'), {'search': 'секретный-запрос'})

        self.assertNotIn('секретный-запрос', '\n'.join(captured.output))


class AsyncRequestLoggingTests(TestCase):
    """The async middleware branch, which serves the SSE endpoint under ASGI.

    The sync tests above exercise a different code path entirely, so without
    these the async branch would ship untested.
    """

    async def test_an_async_request_gets_a_request_id_header(self):
        from django.test import AsyncClient

        response = await AsyncClient().get(reverse('health_live'))

        self.assertIn(REQUEST_ID_HEADER, response)
        self.assertTrue(response[REQUEST_ID_HEADER])

    async def test_the_async_context_is_cleared_after_the_response(self):
        from django.test import AsyncClient

        await AsyncClient().get(reverse('health_live'))

        self.assertIsNone(get_request_id())
        self.assertIsNone(get_user_id())

    async def test_concurrent_async_requests_get_distinct_ids(self):
        import asyncio

        from django.test import AsyncClient

        client = AsyncClient()
        responses = await asyncio.gather(
            *(client.get(reverse('health_live')) for _ in range(5))
        )

        ids = {response[REQUEST_ID_HEADER] for response in responses}
        # ContextVars are per-context, so concurrent requests must never share
        # an id — that is the whole reason this is not a thread-local.
        self.assertEqual(len(ids), 5)

    async def test_an_async_exception_is_logged_with_a_stack_trace(self):
        from ecosystem.middleware import RequestLoggingMiddleware

        async def boom(request):
            raise RuntimeError('async exploded')

        middleware = RequestLoggingMiddleware(boom)
        request = RequestFactory().get('/acts/')

        with self.assertLogs('ecosystem.request', level=logging.ERROR) as captured:
            with self.assertRaises(RuntimeError):
                await middleware(request)

        logged = '\n'.join(captured.output)
        self.assertIn('http.request_failed', logged)
        self.assertIn('Traceback', logged)


class FileHandlerTests(SimpleTestCase):
    """The rotating file handler itself, exercised directly.

    The project's own handler is configured at import time, so these build an
    equivalent handler against a temporary directory rather than reconfiguring
    logging globally and leaking that into other tests.
    """

    def _handler(self, directory, **kwargs):
        options = {'maxBytes': 1024, 'backupCount': 2, 'encoding': 'utf-8'}
        options.update(kwargs)
        handler = logging.handlers.RotatingFileHandler(
            str(Path(directory) / 'application.log'), **options
        )
        handler.setFormatter(SafeFormatter('%(message)s'))
        handler.addFilter(SensitiveValueRedactionFilter())
        return handler

    def test_the_file_is_created_and_written(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = self._handler(directory)
            logger = logging.getLogger('test.file.created')
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                log_event(logger, 'INFO', 'probe.event', act_id=1)
            finally:
                logger.removeHandler(handler)
                handler.close()

            content = (Path(directory) / 'application.log').read_text(encoding='utf-8')

        self.assertIn('probe.event act_id=1', content)

    def test_utf8_text_survives_a_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = self._handler(directory)
            logger = logging.getLogger('test.file.utf8')
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                log_event(logger, 'INFO', 'probe.event', status='Передан_в_КО')
            finally:
                logger.removeHandler(handler)
                handler.close()

            content = (Path(directory) / 'application.log').read_text(encoding='utf-8')

        self.assertIn('Передан_в_КО', content)

    def test_rotation_creates_a_backup_and_respects_the_backup_count(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = self._handler(directory, maxBytes=200, backupCount=2)
            logger = logging.getLogger('test.file.rotation')
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                for index in range(200):
                    log_event(logger, 'INFO', 'probe.event', index=index, filler='x' * 50)
            finally:
                logger.removeHandler(handler)
                handler.close()

            files = sorted(path.name for path in Path(directory).iterdir())

        self.assertIn('application.log', files)
        self.assertIn('application.log.1', files)
        # backupCount=2 means the base file plus at most two archives.
        self.assertLessEqual(len(files), 3)
        self.assertNotIn('application.log.3', files)

    @override_settings(SECRET_KEY=TEST_SECRET_KEY)
    def test_a_secret_is_absent_from_both_the_active_and_the_rotated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = self._handler(directory, maxBytes=200, backupCount=3)
            logger = logging.getLogger('test.file.secrets')
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                for index in range(120):
                    logger.info('attempt %s with key %s', index, TEST_SECRET_KEY)
            finally:
                logger.removeHandler(handler)
                handler.close()

            for path in Path(directory).iterdir():
                with self.subTest(file=path.name):
                    self.assertNotIn(
                        TEST_SECRET_KEY, path.read_text(encoding='utf-8')
                    )


class LoggingConfigurationTests(SimpleTestCase):
    def _logging_check_ids(self, **overrides):
        with override_settings(**overrides):
            return {
                message.id
                for message in deployment_checks.check_logging_configuration(None)
            }

    def test_a_configuration_with_no_handler_is_blocking(self):
        self.assertIn(
            'ecosystem.E022',
            self._logging_check_ids(LOG_TO_FILE=False, LOG_TO_CONSOLE=False),
        )

    def test_console_only_is_a_valid_configuration(self):
        self.assertEqual(
            self._logging_check_ids(LOG_TO_FILE=False, LOG_TO_CONSOLE=True), set()
        )

    def test_a_zero_rotation_limit_is_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            reported = self._logging_check_ids(
                LOG_TO_FILE=True,
                LOG_TO_CONSOLE=True,
                LOG_FILE_PATH=Path(directory) / 'application.log',
                LOG_FILE_MAX_BYTES=0,
                LOG_FILE_BACKUP_COUNT=0,
            )

        self.assertIn('ecosystem.E023', reported)
        self.assertIn('ecosystem.E024', reported)

    def test_a_log_file_inside_static_or_media_is_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            reported = self._logging_check_ids(
                LOG_TO_FILE=True,
                LOG_TO_CONSOLE=True,
                STATIC_ROOT=directory,
                LOG_FILE_PATH=Path(directory) / 'application.log',
                LOG_FILE_MAX_BYTES=1024,
                LOG_FILE_BACKUP_COUNT=1,
            )

        self.assertIn('ecosystem.E025', reported)

    def test_a_relative_production_path_is_blocking(self):
        reported = self._logging_check_ids(
            IS_PRODUCTION=True,
            LOG_TO_FILE=True,
            LOG_TO_CONSOLE=True,
            LOG_FILE_PATH=Path('logs/application.log'),
            LOG_FILE_MAX_BYTES=1024,
            LOG_FILE_BACKUP_COUNT=1,
        )

        self.assertIn('ecosystem.E026', reported)

    def test_an_unwritable_path_is_blocking_in_production(self):
        reported = self._logging_check_ids(
            IS_PRODUCTION=True,
            LOG_TO_FILE=True,
            LOG_TO_CONSOLE=True,
            LOG_FILE_PATH=Path('/nonexistent-directory-for-tests/application.log').resolve(),
            LOG_FILE_MAX_BYTES=1024,
            LOG_FILE_BACKUP_COUNT=1,
        )

        self.assertIn('ecosystem.E027', reported)

    def test_the_summary_never_needs_the_path_to_be_shared(self):
        summary = describe_logging_configuration()

        # A readiness report publishes `file_path_configured`, never the path.
        self.assertIn('file_path_configured', summary)
        self.assertIn('to_file', summary)
        self.assertIn('to_console', summary)


class CheckLoggingCommandTests(TestCase):
    def _run(self, **options):
        buffer = StringIO()
        try:
            call_command('check_logging', stdout=buffer, stderr=buffer, **options)
            code = 0
        except SystemExit as exc:
            code = exc.code
        return code, buffer.getvalue()

    def test_it_reports_the_active_handlers_and_level(self):
        code, output = self._run()

        self.assertEqual(code, 0)
        self.assertIn('log_handlers', output)

    def test_it_changes_no_database_row(self):
        from acts.models import Act

        before = (Act.objects.count(), User.objects.count())
        self._run()
        self.assertEqual((Act.objects.count(), User.objects.count()), before)

    def test_the_write_probe_writes_one_line_to_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'application.log'
            handler = logging.handlers.RotatingFileHandler(
                str(path), maxBytes=100000, backupCount=1, encoding='utf-8'
            )
            handler.setFormatter(SafeFormatter('%(message)s'))
            logger = logging.getLogger('ecosystem.startup')
            logger.addHandler(handler)
            try:
                with override_settings(
                    LOG_TO_FILE=True,
                    LOG_FILE_PATH=path,
                    LOG_FILE_MAX_BYTES=100000,
                    LOG_FILE_BACKUP_COUNT=1,
                ):
                    code, output = self._run(write_probe=True)
            finally:
                logger.removeHandler(handler)
                handler.close()

            content = path.read_text(encoding='utf-8')

        self.assertEqual(code, 0)
        self.assertIn('write_probe', output)
        self.assertIn('logging.probe', content)

    def test_the_write_probe_never_fakes_an_error(self):
        with self.assertNoLogs('ecosystem.startup', level=logging.ERROR):
            self._run(write_probe=True)

    def test_the_json_report_omits_the_log_file_path(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / 'logging.json'
            self._run(json_report=str(report_path))
            raw = report_path.read_text(encoding='utf-8')
            report = json.loads(raw)

        self.assertIn('file_path_configured', report)
        self.assertNotIn('file_path', report)
        self.assertNotIn('application.log', raw)


class BusinessEventLoggingTests(TestCase):
    """Workflow, task, email and attachment events: ids yes, user text never."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())

    def setUp(self):
        from accounts.models import Department, UserProfile
        from acts.models import Act
        from references.models import ActStatus, DefectType, Operation

        self.department = Department.objects.create(name='ОТК', code='OTK')
        self.otk_user = User.objects.create_user('otk_probe', password='probe-password-1')
        UserProfile.objects.update_or_create(
            user=self.otk_user,
            defaults={'role': 'otk', 'department': self.department, 'is_active': True},
        )
        self.act = Act.objects.create(
            number='АОК-2026-00001',
            created_by=self.otk_user,
            party_number='P-1',
            nomenclature='Изделие',
            operation=Operation.objects.first(),
            defect_type=DefectType.objects.first(),
            status=ActStatus.objects.get(code='CREATED_OTK'),
            description='СЕКРЕТНОЕ ОПИСАНИЕ ДЕФЕКТА',
        )

    def test_a_successful_transition_is_logged_with_ids_and_statuses(self):
        from acts.services import send_to_ko

        with self.assertLogs('ecosystem.workflow', level=logging.INFO) as captured:
            send_to_ko(self.act, self.otk_user)

        logged = '\n'.join(captured.output)
        self.assertIn('workflow.transition_completed', logged)
        self.assertIn('action=send_to_ko', logged)
        self.assertIn(f'act_id={self.act.pk}', logged)
        self.assertIn(f'actor_user_id={self.otk_user.pk}', logged)
        self.assertIn('previous_status=CREATED_OTK', logged)
        self.assertIn('next_status=KO_REVIEW', logged)
        self.assertIn('outcome=ok', logged)

    def test_a_transition_log_never_contains_business_text_or_a_username(self):
        from acts.services import send_to_ko

        with self.assertLogs('ecosystem.workflow', level=logging.INFO) as captured:
            send_to_ko(self.act, self.otk_user)

        logged = '\n'.join(captured.output)
        self.assertNotIn('СЕКРЕТНОЕ ОПИСАНИЕ ДЕФЕКТА', logged)
        self.assertNotIn('otk_probe', logged)
        self.assertNotIn(self.act.number, logged)

    def test_a_rejected_transition_is_logged_as_rejected(self):
        from acts.services import ActWorkflowError, approve_act

        with self.assertLogs('ecosystem.workflow', level=logging.INFO) as captured:
            with self.assertRaises(ActWorkflowError):
                # Wrong status for this action.
                approve_act(self.act, self.otk_user)

        logged = '\n'.join(captured.output)
        self.assertIn('workflow.transition_rejected', logged)
        self.assertIn('outcome=rejected', logged)
        self.assertIn('action=approve_act', logged)

    def test_a_missing_readable_attachment_is_logged_without_the_file_name(self):
        from acts.models import ActAttachment

        other_user = User.objects.create_user('unrelated_probe', password='probe-password-1')
        attachment = ActAttachment.objects.create(
            act=self.act,
            uploaded_by=self.otk_user,
            original_name='секретный-документ.pdf',
            file_size=10,
            content_type='application/pdf',
        )
        self.client.force_login(other_user)

        with self.assertLogs('ecosystem.attachments', level=logging.WARNING) as captured:
            response = self.client.get(
                reverse('acts:download_attachment', args=[self.act.pk, attachment.pk])
            )

        logged = '\n'.join(captured.output)
        self.assertEqual(response.status_code, 404)
        self.assertIn('attachment.storage_failed', logged)
        self.assertIn(f'attachment_id={attachment.pk}', logged)
        self.assertIn('operation=download', logged)
        self.assertIn('outcome=missing_file', logged)
        self.assertNotIn('секретный-документ', logged)

    def test_one_request_id_joins_the_http_request_to_its_service_logs(self):
        # The whole point of the request context: an operator quotes the
        # X-Request-ID from a user's screen and finds the workflow line that
        # the same request produced, in a different logger.
        import re

        self.client.force_login(self.otk_user)

        # Both loggers are captured explicitly: they use `propagate=False`, so
        # capturing the root logger would see neither.
        captured_records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured_records.append(record)

        handler = _Capture()
        request_logger = logging.getLogger('ecosystem.request')
        workflow_logger = logging.getLogger('ecosystem.workflow')
        for target in (request_logger, workflow_logger):
            target.addHandler(handler)
        try:
            response = self.client.post(reverse('acts:send_to_ko', args=[self.act.pk]))
        finally:
            for target in (request_logger, workflow_logger):
                target.removeHandler(handler)

        self.assertIn(response.status_code, (200, 302))

        def context_ids(logger_name, event):
            found = set()
            for record in captured_records:
                if record.name == logger_name and event in record.getMessage():
                    RequestContextFilter().filter(record)
                    if re.fullmatch(r'[0-9a-f]{32}', str(record.request_id)):
                        found.add(record.request_id)
            return found

        workflow_ids = context_ids('ecosystem.workflow', 'workflow.transition_completed')
        request_ids = context_ids('ecosystem.request', 'http.request')

        self.assertTrue(workflow_ids, 'the transition produced no workflow line with a request id')
        self.assertTrue(request_ids, 'the POST produced no request line with a request id')
        # The same id on both, which is what makes an incident traceable.
        self.assertEqual(workflow_ids, request_ids)
        self.assertEqual(response[REQUEST_ID_HEADER], workflow_ids.pop())

    def test_the_email_worker_logs_aggregate_counts_only(self):
        from notifications.email_delivery import process_pending_deliveries

        with self.assertLogs('notifications.email', level=logging.INFO) as captured:
            process_pending_deliveries()

        logged = '\n'.join(captured.output)
        self.assertIn('email.worker_started', logged)
        self.assertIn('email.worker_completed', logged)
        self.assertIn('processed=', logged)


class TaskEventLoggingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())

    def setUp(self):
        from accounts.models import Department, UserProfile
        from acts.models import Act, ActCorrectiveAction, ActRootAnalysis
        from references.models import ActStatus, DefectType, Operation, TaskStatus
        from tasks.models import Task, TaskAssignee

        self.department = Department.objects.create(name='ТО', code='TO')
        self.user = User.objects.create_user('task_probe', password='probe-password-1')
        UserProfile.objects.update_or_create(
            user=self.user,
            defaults={'role': 'to', 'department': self.department, 'is_active': True},
        )
        act = Act.objects.create(
            created_by=self.user,
            party_number='P-1',
            nomenclature='Изделие',
            operation=Operation.objects.first(),
            defect_type=DefectType.objects.first(),
            status=ActStatus.objects.get(code='ARCHIVED'),
            description='описание',
        )
        # A task always originates from an approved corrective action, so the
        # fixture builds that chain rather than a detached task row.
        root_analysis = ActRootAnalysis.objects.create(
            act=act, root_cause='причина', display_order=0
        )
        corrective_action = ActCorrectiveAction.objects.create(
            root_analysis=root_analysis,
            comment='мероприятие',
            department=self.department,
            due_date=timezone_today(),
            display_order=0,
        )
        self.task = Task.objects.create(
            source_action=corrective_action,
            act=act,
            root_analysis=root_analysis,
            task_text='СЕКРЕТНЫЙ ТЕКСТ ЗАДАЧИ',
            department=self.department,
            due_date=timezone_today(),
            created_by=self.user,
            status=TaskStatus.objects.get(code='IN_PROGRESS'),
        )
        TaskAssignee.objects.create(task=self.task, user=self.user)

    def test_completing_a_task_is_logged_with_ids_and_statuses(self):
        from tasks.services import complete_task

        with self.assertLogs('ecosystem.workflow', level=logging.INFO) as captured:
            complete_task(self.task, self.user, 'выполнено полностью')

        logged = '\n'.join(captured.output)
        self.assertIn('task.completed', logged)
        self.assertIn(f'task_id={self.task.pk}', logged)
        self.assertIn('previous_status=IN_PROGRESS', logged)
        self.assertIn('next_status=COMPLETED', logged)
        self.assertIn('assignee_count=1', logged)

    def test_a_task_log_never_contains_the_task_text_or_execution_comment(self):
        from tasks.services import complete_task

        with self.assertLogs('ecosystem.workflow', level=logging.INFO) as captured:
            complete_task(self.task, self.user, 'СЕКРЕТНЫЙ РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ')

        logged = '\n'.join(captured.output)
        self.assertNotIn('СЕКРЕТНЫЙ ТЕКСТ ЗАДАЧИ', logged)
        self.assertNotIn('СЕКРЕТНЫЙ РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ', logged)
        self.assertNotIn('task_probe', logged)

    def test_a_second_completion_is_logged_as_rejected(self):
        from tasks.services import TaskWorkflowError, complete_task

        complete_task(self.task, self.user, 'выполнено')

        with self.assertLogs('ecosystem.workflow', level=logging.INFO) as captured:
            with self.assertRaises(TaskWorkflowError):
                complete_task(self.task, self.user, 'ещё раз')

        logged = '\n'.join(captured.output)
        self.assertIn('task.operation_rejected', logged)
        self.assertIn('outcome=rejected', logged)

    def test_an_empty_execution_comment_is_logged_as_rejected(self):
        from tasks.services import TaskWorkflowError, complete_task

        with self.assertLogs('ecosystem.workflow', level=logging.INFO) as captured:
            with self.assertRaises(TaskWorkflowError):
                complete_task(self.task, self.user, '   ')

        self.assertIn('reason=empty_execution_comment', '\n'.join(captured.output))


def timezone_today():
    from django.utils import timezone

    return timezone.localdate()
