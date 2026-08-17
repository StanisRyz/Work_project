"""One-time migration of a real legacy `prorabotka.json` into the database.

Never part of normal runtime and never run by a migration: the file-based
journal is gone, and this exists only so an existing shared file can be
carried over once.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from calculator.models import EntrySource, WindingEntry
from calculator.services import CalculatorValidationError, clean_entry


class Command(BaseCommand):
    help = 'Импортировать журнал «Проработка» из файла prorabotka.json (разовая миграция).'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Путь к файлу prorabotka.json')

    def handle(self, *args, **options):
        path = Path(options['path']).expanduser()
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except OSError as exc:
            raise CommandError(f'Не удалось прочитать файл: {exc}')
        except json.JSONDecodeError as exc:
            raise CommandError(f'Файл не является корректным JSON: {exc}')

        if not isinstance(document, dict) or document.get('version') != 1:
            raise CommandError('Ожидается структура версии 1: {"version": 1, ..., "entries": [...]}.')
        entries = document.get('entries')
        if not isinstance(entries, list):
            raise CommandError('В файле нет списка "entries".')

        imported = duplicates = invalid = 0
        for raw in entries:
            if not isinstance(raw, dict):
                invalid += 1
                continue
            try:
                values = clean_entry(self._normalize(raw))
            except CalculatorValidationError as error:
                invalid += 1
                self.stderr.write(f'Пропущено: {raw.get("name") or "(без имени)"} — {error}')
                continue
            # A legacy file may hold the same calculation case twice; the
            # journal keeps one row for it, as the file-based journal did.
            if WindingEntry.objects.filter(
                calculation_signature=values['calculation_signature'],
            ).exists():
                duplicates += 1
                continue
            # `CALCULATOR`, not `IMPORT`: an imported case must own its
            # signature, or the first recalculation of that core in the tab
            # would add a second row for a case already worked out.
            WindingEntry.objects.create(
                **values, source=EntrySource.CALCULATOR, **self._production(raw),
            )
            imported += 1

        self.stdout.write(
            f'Импортировано: {imported}; пропущено дубликатов: {duplicates}; '
            f'некорректных записей: {invalid}.'
        )

    @staticmethod
    def _normalize(raw):
        """Bring a version-1 record into the shape `clean_entry()` expects.

        Legacy files carry no calibration fields and may store only the
        rounded coefficient, so the raw one falls back to it.
        """
        data = dict(raw)
        data.setdefault('name', '')
        if data.get('rawComplexityCoefficient') is None:
            data['rawComplexityCoefficient'] = data.get('complexityCoefficient')
        data['calibrationEnabled'] = bool(data.get('calibrationEnabled'))
        return data

    @staticmethod
    def _production(raw):
        """Production data, with the per-unit time recomputed rather than read.

        A legacy row that carries partial or unusable numbers is imported as
        an unconfirmed entry: the calculation is preserved, and the shop
        simply confirms the batch again.
        """
        quantity = raw.get('batchQuantity')
        batch_hours = raw.get('actualBatchTimeHours')
        if batch_hours is None and raw.get('actualTimeSeconds') is not None:
            try:
                batch_hours = float(raw['actualTimeSeconds']) / 3600
            except (TypeError, ValueError):
                batch_hours = None
        try:
            quantity = int(quantity)
            batch_hours = float(batch_hours)
        except (TypeError, ValueError):
            return {'production_confirmed': False}
        if quantity <= 0 or batch_hours <= 0:
            return {'production_confirmed': False}
        confirmed = bool(raw.get('productionConfirmed'))
        return {
            'batch_quantity': quantity,
            'actual_batch_time_hours': batch_hours,
            'actual_unit_time_hours': batch_hours / quantity if confirmed else None,
            'production_confirmed': confirmed,
        }
