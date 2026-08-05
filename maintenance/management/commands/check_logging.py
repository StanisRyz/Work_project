"""Inspect the effective logging configuration, and optionally prove it writes.

Read-only by default: it reports which handlers are active, at what level, how
rotation is configured and whether the log path is usable. It touches no
business data and no database row.

`--write-probe` is the one action it can take: it emits a single ordinary
`logging.probe` INFO record and reports whether the file grew. It deliberately
does not fake an ERROR — a synthetic error in a production log is exactly the
kind of noise that makes an operator distrust the file.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from ecosystem.logging_utils import describe_logging_configuration


PASS = 'ok'
WARNING = 'warning'
BLOCKING = 'blocking'

_LABELS = {PASS: 'PASS', WARNING: 'WARNING', BLOCKING: 'BLOCKING'}


class Command(BaseCommand):
    help = (
        'Показывает включённые обработчики логов, уровень, параметры ротации и '
        'готовность файла журнала. По умолчанию только чтение; --write-probe '
        'записывает одну проверочную строку.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--write-probe',
            action='store_true',
            help='Записать одну строку logging.probe и проверить, что файл изменился.',
        )
        parser.add_argument(
            '--json-report',
            help='Куда записать безопасный JSON-отчёт (без полного пути к журналу).',
        )

    def handle(self, *args, **options):
        summary = describe_logging_configuration()
        results = evaluate_logging_configuration(summary)

        self.stdout.write('Конфигурация логирования')
        self.stdout.write('')
        self.stdout.write(f'  Файл:            {"включён" if summary["to_file"] else "выключен"}')
        self.stdout.write(f'  Консоль:         {"включена" if summary["to_console"] else "выключена"}')
        self.stdout.write(f'  Уровень:         {summary["level"]} (realtime: {summary["realtime_level"]})')
        if summary['to_file']:
            # The full path is printed for the local operator only; it never
            # reaches the JSON report below.
            self.stdout.write(f'  Путь:            {summary["file_path"]}')
            # Plain ASCII "x", not the multiplication sign: this is printed to
            # a console that may be cp1251 on Windows, where a non-encodable
            # character crashes the command itself.
            self.stdout.write(
                f'  Ротация:         {summary["max_bytes"]} байт x '
                f'{summary["backup_count"]} архивных копий'
            )
            self.stdout.write(
                f'  Каталог:         {"существует" if summary["directory_exists"] else "отсутствует"}, '
                f'{"доступен для записи" if summary["writable"] else "недоступен для записи"}'
            )

        self.stdout.write('')
        for item in results:
            self.stdout.write(f'  [{_LABELS[item["status"]]:<8}] {item["check"]}: {item["detail"]}')

        probe_result = None
        if options['write_probe']:
            probe_result = self._run_write_probe(summary)
            results.append(probe_result)
            self.stdout.write('')
            self.stdout.write(
                f'  [{_LABELS[probe_result["status"]]:<8}] '
                f'{probe_result["check"]}: {probe_result["detail"]}'
            )

        if options['json_report']:
            destination = Path(options['json_report'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            report = {
                'schema_version': 1,
                'to_file': summary['to_file'],
                'to_console': summary['to_console'],
                'level': summary['level'],
                'realtime_level': summary['realtime_level'],
                'max_bytes': summary['max_bytes'],
                'backup_count': summary['backup_count'],
                # The path itself is deliberately absent: a report is shared,
                # and a server path is infrastructure detail.
                'file_path_configured': summary['file_path_configured'],
                'file_path_absolute': summary['file_path_absolute'],
                'writable': summary['writable'],
                'results': results,
            }
            destination.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            self.stdout.write('')
            self.stdout.write(f'JSON-отчёт записан: {destination}')

        blocking = [item for item in results if item['status'] == BLOCKING]
        self.stdout.write('')
        if blocking:
            self.stdout.write(
                self.style.ERROR(f'Логирование не готово: проблем — {len(blocking)}.')
            )
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS('Логирование настроено.'))
        return None

    def _run_write_probe(self, summary):
        """Emit one ordinary INFO record and check the file actually grew."""
        import logging

        from ecosystem.logging_utils import log_event

        logger = logging.getLogger('ecosystem.startup')

        if not summary['to_file']:
            log_event(logger, 'INFO', 'logging.probe', target='console')
            return _result(
                'write_probe',
                WARNING,
                'Файловый журнал выключен: строка записана только в консоль.',
            )

        path = Path(summary['file_path'])
        size_before = path.stat().st_size if path.is_file() else -1
        try:
            log_event(logger, 'INFO', 'logging.probe', target='file')
            for handler in logging.getLogger('ecosystem.startup').handlers:
                handler.flush()
        except Exception as exc:  # noqa: BLE001 - the type is the useful part
            return _result(
                'write_probe', BLOCKING, f'Не удалось записать ({type(exc).__name__}).'
            )

        if not path.is_file():
            return _result('write_probe', BLOCKING, 'Файл журнала не был создан.')
        if path.stat().st_size <= size_before:
            return _result(
                'write_probe',
                BLOCKING,
                'Файл журнала не изменился после записи проверочной строки.',
            )
        return _result('write_probe', PASS, 'Проверочная строка записана в файл журнала.')


def _result(check, status, detail):
    return {'check': check, 'status': status, 'detail': detail}


def evaluate_logging_configuration(summary):
    """Turn the configuration summary into PASS/WARNING/BLOCKING results.

    Shared with `check_production_readiness` so the readiness report and this
    command can never disagree about whether logging is usable.
    """
    results = []

    if not summary['to_file'] and not summary['to_console']:
        results.append(
            _result('log_handlers', BLOCKING, 'Ни один обработчик логов не включён.')
        )
    elif summary['to_file'] and summary['to_console']:
        results.append(_result('log_handlers', PASS, 'Файл и консоль.'))
    elif summary['to_file']:
        results.append(_result('log_handlers', PASS, 'Только файл.'))
    else:
        results.append(
            _result(
                'log_handlers',
                WARNING,
                'Только консоль: сбор и хранение выполняет process manager или '
                'внешняя система.',
            )
        )

    if not summary['to_file']:
        return results

    if summary['max_bytes'] <= 0 or summary['backup_count'] <= 0:
        results.append(
            _result(
                'log_rotation',
                BLOCKING,
                'Ротация настроена некорректно: LOG_FILE_MAX_BYTES и '
                'LOG_FILE_BACKUP_COUNT должны быть положительными.',
            )
        )
    else:
        results.append(
            _result(
                'log_rotation',
                PASS,
                f'{summary["max_bytes"]} байт x {summary["backup_count"]} архивных копий.',
            )
        )

    if summary['collides_with_static_or_media']:
        results.append(
            _result(
                'log_path',
                BLOCKING,
                'Файл журнала находится внутри STATIC_ROOT или MEDIA_ROOT.',
            )
        )
    elif not summary['writable']:
        results.append(
            _result('log_path', BLOCKING, 'Каталог или файл журнала недоступен для записи.')
        )
    elif not summary['file_path_absolute']:
        results.append(
            _result(
                'log_path',
                WARNING,
                'Путь относительный: он зависит от рабочего каталога процесса.',
            )
        )
    else:
        results.append(_result('log_path', PASS, 'Путь доступен для записи.'))

    return results
