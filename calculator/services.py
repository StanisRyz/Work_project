"""Backend validation and the only place journal rows are written.

The browser validates the same things for immediate feedback, but nothing
here trusts it: every number is re-checked, `case_key` is re-derived and the
per-unit production time is recomputed from the confirmed batch data. The
numeric checks and their wording follow the calculator's own server
prototype (`server.py` in the source repository) so the rules did not change
on the way into Django.
"""
import math

from django.db import transaction

from .models import CALCULATION_VERSION, WindingEntry, build_case_key


class CalculatorValidationError(Exception):
    """Rejected input, carrying one message per offending field."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__('; '.join(errors.values()))


def normalize_complexity_coefficient(value):
    """The 0,25-step coefficient shown everywhere, as in the source modules.

    `math.floor(x * 4 + 0.5) / 4` reproduces JavaScript's `Math.round()`,
    which rounds halves upward rather than to even.
    """
    return math.floor(float(value) * 4 + 0.5) / 4


def _finite(errors, data, key, label):
    try:
        number = float(data.get(key))
    except (TypeError, ValueError):
        errors[key] = f'{label}: введите число.'
        return None
    if not math.isfinite(number):
        errors[key] = f'{label}: введите число.'
        return None
    return number


def _positive(errors, data, key, label):
    number = _finite(errors, data, key, label)
    if number is None:
        return None
    if number <= 0:
        errors[key] = f'{label}: значение должно быть больше нуля.'
        return None
    return number


def _non_negative(errors, data, key, label):
    number = _finite(errors, data, key, label)
    if number is None:
        return None
    if number < 0:
        errors[key] = f'{label}: значение не может быть отрицательным.'
        return None
    return number


def _batch_quantity(value):
    """Units in the batch: a strictly positive whole number."""
    message = {'batchQuantity': 'Единиц в партии: введите целое число больше нуля.'}
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CalculatorValidationError(message)
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise CalculatorValidationError(message)
    return int(number)


def _batch_time_hours(value):
    """Actual time for the whole batch, in hours."""
    message = {'actualBatchTimeHours': 'Фактическое время партии: введите время в часах больше нуля.'}
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CalculatorValidationError(message)
    if not math.isfinite(number) or number <= 0:
        raise CalculatorValidationError(message)
    return number


def clean_entry(data):
    """Validate a calculated case and return model field values.

    Raises `CalculatorValidationError` with a field → message mapping.
    """
    errors = {}

    name = str(data.get('name') or '').strip()
    if not name:
        errors['name'] = 'Не задано имя расчётного случая.'

    inner = _positive(errors, data, 'd', 'd')
    outer = _positive(errors, data, 'D', 'D')
    width = _positive(errors, data, 'b', 'b')
    thickness = _positive(errors, data, 'tapeThicknessMm', 'Толщина ленты')
    height = _positive(errors, data, 'heightMm', 'Высота навивки')
    standard = _positive(errors, data, 'standardCoefficient', 'Скорость навивки')
    winding_time = _positive(errors, data, 'windingTimeSeconds', 'Время навивки')
    # Additional operations always cost something under the agreed formulas,
    # but the stored value is only required to be a finite non-negative time.
    additional_time = _non_negative(
        errors, data, 'additionalOperationsTimeSeconds', 'Время дополнительных операций',
    )
    total_time = _positive(errors, data, 'totalTimeSeconds', 'Расчётное время')
    raw_complexity = _positive(errors, data, 'rawComplexityCoefficient', 'Коэффициент сложности')

    if inner is not None and outer is not None and outer <= inner:
        errors['D'] = 'Внешний диаметр D должен быть больше внутреннего диаметра d.'

    calibration_enabled = bool(data.get('calibrationEnabled'))
    calibration_diameter = None
    if calibration_enabled:
        calibration_diameter = _positive(
            errors, data, 'calibrationDiameterMm', 'Радиус калибровки',
        )

    if errors:
        raise CalculatorValidationError(errors)

    return {
        'name': name,
        'case_key': build_case_key(name),
        'd': inner,
        'outer_diameter': outer,
        'b': width,
        'tape_thickness_mm': thickness,
        'height_mm': height,
        'calibration_enabled': calibration_enabled,
        'calibration_diameter_mm': calibration_diameter,
        'standard_coefficient': standard,
        'raw_complexity_coefficient': raw_complexity,
        'complexity_coefficient': normalize_complexity_coefficient(raw_complexity),
        'winding_time_seconds': winding_time,
        'additional_operations_time_seconds': additional_time,
        'total_time_seconds': total_time,
        'calculation_version': CALCULATION_VERSION,
    }


def create_entry(user, data):
    """Store a calculated case; return `(entry, created)`.

    An existing normalized case is *not* an error: the caller gets the row
    that is already there, so two users calculating the same magnetic core
    concurrently still see exactly one logical journal entry.
    """
    values = clean_entry(data)
    case_key = values.pop('case_key')
    with transaction.atomic():
        entry, created = WindingEntry.objects.get_or_create(
            case_key=case_key,
            defaults={**values, 'created_by': user, 'updated_by': user},
        )
    return entry, created


def confirm_production(user, entry, batch_quantity, actual_batch_time_hours):
    """Confirm production data, deriving the per-unit time on the server."""
    quantity = _batch_quantity(batch_quantity)
    batch_hours = _batch_time_hours(actual_batch_time_hours)
    with transaction.atomic():
        locked = WindingEntry.objects.select_for_update().get(pk=entry.pk)
        locked.batch_quantity = quantity
        locked.actual_batch_time_hours = batch_hours
        # Never taken from the request: the browser may show it, the server
        # decides it.
        locked.actual_unit_time_hours = batch_hours / quantity
        locked.production_confirmed = True
        locked.updated_by = user
        locked.save(update_fields=[
            'batch_quantity', 'actual_batch_time_hours', 'actual_unit_time_hours',
            'production_confirmed', 'updated_by', 'updated_at',
        ])
    return locked


def unlock_production(user, entry):
    """Reopen confirmed production data for editing.

    The recorded numbers stay in place so the row comes back with the values
    the user is about to correct, exactly as the source UX behaved.
    """
    with transaction.atomic():
        locked = WindingEntry.objects.select_for_update().get(pk=entry.pk)
        locked.production_confirmed = False
        locked.updated_by = user
        locked.save(update_fields=['production_confirmed', 'updated_by', 'updated_at'])
    return locked


def confirmed_entries():
    """Rows the export may contain: confirmed *and* numerically complete."""
    return WindingEntry.objects.filter(
        production_confirmed=True,
        batch_quantity__gt=0,
        actual_batch_time_hours__gt=0,
        actual_unit_time_hours__gt=0,
    )
