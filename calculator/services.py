"""Backend validation and the only place journal rows are written.

The browser validates the same things for immediate feedback, but nothing
here trusts it: every number is re-checked, the keys are re-derived, the 1С
expression is parsed again and the per-unit production time is recomputed
from the confirmed batch data. The numeric checks and their wording follow
the calculator's own server prototype (`server.py` in the source repository)
so the rules did not change on the way into Django.

Row creation comes in two flavours and they are not interchangeable:
`create_entry()` is the Calculator tab writing a calculation case, which
converges on one row per signature, and `create_manual_entry()` is a person
adding a line to «Проработка» by hand, which always produces a new row.
"""
import math

from django.db import transaction

from .expressions import OneCExpressionError, evaluate_one_c
from .models import (
    CALCULATION_VERSION,
    EntrySource,
    WindingEntry,
    build_calculation_signature,
    build_case_key,
)


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


def _employee_name(value):
    """The responsible employee: optional free text, length-capped."""
    name = str(value or '').strip()
    if len(name) > 120:
        raise CalculatorValidationError({'employeeName': 'Сотрудник: не более 120 символов.'})
    return name


def _one_c(value):
    """The «1С, ч» field, evaluated here even when the browser previewed it."""
    try:
        return evaluate_one_c(value)
    except OneCExpressionError as error:
        raise CalculatorValidationError({'oneCExpression': str(error)})


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
        # Derived from the validated numbers, never from the visible name and
        # never from anything the request could set directly.
        'calculation_signature': build_calculation_signature(
            inner, outer, width, thickness, calibration_enabled, calibration_diameter,
        ),
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
    """Store a case calculated in the Calculator tab; return `(entry, created)`.

    Deduplication is on the *whole* calculation signature, so `100/130-40`
    with a 0,23 tape and `100/130-40` with a 0,30 tape are two rows while the
    very same calculation repeated is one. An existing case is not an error:
    the caller gets the row that is already there, which is also how two users
    calculating concurrently converge — `get_or_create()` retries the lookup
    when the partial unique constraint rejects the loser's insert.
    """
    values = clean_entry(data)
    signature = values.pop('calculation_signature')
    with transaction.atomic():
        entry, created = WindingEntry.objects.get_or_create(
            calculation_signature=signature,
            source=EntrySource.CALCULATOR,
            defaults={**values, 'created_by': user, 'updated_by': user},
        )
    return entry, created


def create_manual_entry(user, data):
    """Add a row typed into «Проработка» by hand; always a new row.

    Identical dimensions, tape and calibration may repeat as often as the shop
    needs — each line is told apart by its primary key, which is why the
    signature constraint deliberately does not cover manual rows. The numbers
    still come from the one calculation engine: the tab computes them exactly
    as it does for its own calculations, and they are validated here.
    """
    values = clean_entry(data)
    with transaction.atomic():
        return WindingEntry.objects.create(
            **values, source=EntrySource.MANUAL, created_by=user, updated_by=user,
        )


def confirm_production(user, entry, data):
    """Confirm production data, deriving the per-unit time on the server.

    «1С» and «Сотрудник» ride along with the confirmation because they are
    edited in the same row, but they are optional: an empty one clears the
    stored value and never blocks the confirmation itself.
    """
    quantity = _batch_quantity(data.get('batchQuantity'))
    batch_hours = _batch_time_hours(data.get('actualBatchTimeHours'))
    one_c_expression, one_c_hours = _one_c(data.get('oneCExpression'))
    employee_name = _employee_name(data.get('employeeName'))
    with transaction.atomic():
        locked = WindingEntry.objects.select_for_update().get(pk=entry.pk)
        locked.batch_quantity = quantity
        locked.actual_batch_time_hours = batch_hours
        # Never taken from the request: the browser may show it, the server
        # decides it.
        locked.actual_unit_time_hours = batch_hours / quantity
        locked.one_c_expression = one_c_expression
        # Likewise: the tab previews the arithmetic, the parser here decides.
        locked.one_c_hours = one_c_hours
        locked.employee_name = employee_name
        locked.production_confirmed = True
        locked.updated_by = user
        locked.save(update_fields=[
            'batch_quantity', 'actual_batch_time_hours', 'actual_unit_time_hours',
            'one_c_expression', 'one_c_hours', 'employee_name',
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


def journal_entries():
    """Everything «Проработка» shows — which is everything the export gets.

    Confirmation is a production-floor state, not an export gate: an
    unconfirmed or half-filled row is still a worked-out magnetic core, and
    the missing numbers simply arrive in the workbook as empty cells.
    """
    return WindingEntry.objects.all()
