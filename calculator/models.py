"""The shared «Проработка» journal of the winding-time calculator.

The formulas themselves stay where they came from — the ported browser
modules in `static/js/calculator/` — so this app never recomputes a result.
It stores the *outcome* of a calculation together with every input that
produced it, which is what makes a historical row auditable and
reproducible, and it owns the numbers the browser is not trusted with: the
normalized keys, the per-unit production time and the evaluated 1С value.

Two different identities live here on purpose:

* the Django primary key is *the* identity of a journal row, and two rows
  describing the same magnetic core are two rows;
* `calculation_signature` is the identity of a *calculation case* — the full
  set of inputs the agreed formulas consume — and it is unique only among
  the rows the Calculator tab produced by itself.
"""
import re

from django.contrib.auth.models import User
from django.db import models

# Bumped only when the agreed formulas change, so historical rows stay
# distinguishable afterwards. Deliberately a plain integer: there is no
# version-management framework here and none is wanted.
CALCULATION_VERSION = 1

# How many decimals a signature keeps. Enough for millimetres and tape
# thickness, coarse enough that 0.1 + 0.2 and 0.3 describe the same case.
_SIGNATURE_DECIMALS = 6


class EntrySource(models.TextChoices):
    """Where a journal row came from. Internal; not shown in the journal."""

    CALCULATOR = 'CALCULATOR', 'Калькулятор'
    MANUAL = 'MANUAL', 'Добавлено вручную'
    IMPORT = 'IMPORT', 'Импорт'


def build_case_key(name):
    """The normalized *name* of a case, kept for search and for legacy data.

    Exactly the normalization the source implementation applies in
    `journalHasName()` — `String(name).replace(/\\s+/g, '').toLowerCase()`.
    It is no longer unique: `100/130-40` calculated with two tape thicknesses
    is two calculation cases sharing one name.
    """
    return re.sub(r'\s+', '', str(name)).lower()


def normalize_calibration(calibration_enabled, calibration_diameter_mm):
    """The one non-calibrated state: off, missing and `0` all mean `0`."""
    if not calibration_enabled or calibration_diameter_mm in (None, ''):
        return 0.0
    value = float(calibration_diameter_mm)
    return value if value > 0 else 0.0


def build_calculation_signature(d, outer_diameter, b, tape_thickness_mm,
                                calibration_enabled, calibration_diameter_mm):
    """The identity of a complete calculation case.

    Geometry, tape thickness and the normalized calibration — everything the
    agreed formulas actually consume. The visible `d/D-b` name is deliberately
    not part of it, and neither is anything the browser could rename: the
    signature is always derived here, from validated numbers.
    """
    calibration = normalize_calibration(calibration_enabled, calibration_diameter_mm)
    return '|'.join(
        f'{round(float(value), _SIGNATURE_DECIMALS):.{_SIGNATURE_DECIMALS}f}'
        for value in (d, outer_diameter, b, tape_thickness_mm, calibration)
    )


class WindingEntry(models.Model):
    """One magnetic core worked out once and shared by every user."""

    name = models.CharField('Магнитопровод', max_length=120)
    # The normalized name. Not unique: the same core worked out with another
    # tape or with calibration is another case under the same name.
    case_key = models.CharField('Ключ имени', max_length=120, db_index=True)
    # Unique in the database — but only among `CALCULATOR` rows, through the
    # partial constraint below. Two users calculating the same case
    # concurrently must end up with one logical row; two users deliberately
    # adding the same core by hand must not.
    calculation_signature = models.CharField(
        'Подпись расчётного случая', max_length=120, db_index=True,
    )
    source = models.CharField(
        'Источник', max_length=16, choices=EntrySource.choices,
        default=EntrySource.CALCULATOR, db_index=True,
    )

    # Physical and calculated values are double precision on purpose. The
    # agreed formulas run in the browser on IEEE-754 doubles, and a float
    # column stores exactly the number that was calculated; a Decimal column
    # would quantize it and slowly drift away from the source implementation.
    d = models.FloatField('d, мм')
    outer_diameter = models.FloatField('D, мм')
    b = models.FloatField('b, мм')
    tape_thickness_mm = models.FloatField('Толщина ленты, мм')
    height_mm = models.FloatField('Высота навивки, мм')

    calibration_enabled = models.BooleanField('Калибровка', default=False)
    # Only meaningful when calibration is on; stays NULL otherwise rather
    # than carrying an invented zero.
    calibration_diameter_mm = models.FloatField('Радиус калибровки, мм', null=True, blank=True)

    standard_coefficient = models.FloatField('Скорость навивки, сек/мм')
    # Both the unrounded coefficient and the 0,25-step value the interface,
    # the journal and the export show, as in the source implementation.
    raw_complexity_coefficient = models.FloatField('КС без округления')
    complexity_coefficient = models.FloatField('КС')

    winding_time_seconds = models.FloatField('Время навивки, с')
    additional_operations_time_seconds = models.FloatField('Время доп. операций, с')
    total_time_seconds = models.FloatField('Расчётное время, с')

    batch_quantity = models.PositiveIntegerField('Единиц в партии', null=True, blank=True)
    actual_batch_time_hours = models.FloatField('Фактическое время партии, ч', null=True, blank=True)
    # Always derived on the server from the two fields above; never accepted
    # from the browser.
    actual_unit_time_hours = models.FloatField('Фактическое время единицы, ч', null=True, blank=True)
    production_confirmed = models.BooleanField('Производственные данные подтверждены', default=False)

    # What the user typed into «1С, ч» — a number or a small arithmetic
    # expression — and the value the server evaluated from it. Both are kept:
    # the expression documents how the norm was assembled, the number is what
    # every reader and the export use.
    one_c_expression = models.CharField('1С, выражение', max_length=120, blank=True, default='')
    one_c_hours = models.FloatField('1С, ч', null=True, blank=True)
    # A surname typed by hand, not a Django user: the person answering for the
    # core, who need not have an account here at all.
    employee_name = models.CharField('Сотрудник', max_length=120, blank=True, default='')

    calculation_version = models.PositiveSmallIntegerField('Версия расчёта', default=CALCULATION_VERSION)

    # Auditing only — the journal itself is shared, not owned.
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='created_winding_entries', verbose_name='Создал',
    )
    updated_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='updated_winding_entries', verbose_name='Обновил',
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['created_at', 'pk']
        verbose_name = 'Расчёт навивки'
        verbose_name_plural = 'Расчёты навивки'
        constraints = [
            # Partial on purpose: it makes the Calculator tab converge on one
            # row per calculation case while leaving manual rows free to
            # repeat as often as the shop needs.
            models.UniqueConstraint(
                fields=['calculation_signature'],
                condition=models.Q(source=EntrySource.CALCULATOR),
                name='calculator_unique_calculator_case',
            ),
        ]

    def __str__(self):
        return self.name

    def to_payload(self):
        """The entry in the shape the ported journal modules already expect."""
        return {
            'id': self.pk,
            'name': self.name,
            'source': self.source,
            'd': self.d,
            'D': self.outer_diameter,
            'b': self.b,
            'tapeThicknessMm': self.tape_thickness_mm,
            'heightMm': self.height_mm,
            'calibrationEnabled': self.calibration_enabled,
            'calibrationDiameterMm': self.calibration_diameter_mm,
            'standardCoefficient': self.standard_coefficient,
            'rawComplexityCoefficient': self.raw_complexity_coefficient,
            'complexityCoefficient': self.complexity_coefficient,
            'windingTimeSeconds': self.winding_time_seconds,
            'additionalOperationsTimeSeconds': self.additional_operations_time_seconds,
            'totalTimeSeconds': self.total_time_seconds,
            'batchQuantity': self.batch_quantity,
            'actualBatchTimeHours': self.actual_batch_time_hours,
            'actualUnitTimeHours': self.actual_unit_time_hours,
            'productionConfirmed': self.production_confirmed,
            'oneCExpression': self.one_c_expression,
            'oneCHours': self.one_c_hours,
            'employeeName': self.employee_name,
            'calculationVersion': self.calculation_version,
        }
