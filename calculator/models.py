"""The shared «Проработка» journal of the winding-time calculator.

The formulas themselves stay where they came from — the ported browser
modules in `static/js/calculator/` — so this app never recomputes a result.
It stores the *outcome* of a calculation together with every input that
produced it, which is what makes a historical row auditable and
reproducible, and it owns the two numbers the browser is not trusted with:
the normalized `case_key` and the per-unit production time.
"""
import re

from django.contrib.auth.models import User
from django.db import models

# Bumped only when the agreed formulas change, so historical rows stay
# distinguishable afterwards. Deliberately a plain integer: there is no
# version-management framework here and none is wanted.
CALCULATION_VERSION = 1


def build_case_key(name):
    """The normalized identity of a calculation case.

    Exactly the normalization the source implementation applies in
    `journalHasName()` — `String(name).replace(/\\s+/g, '').toLowerCase()` —
    so a journal imported from the legacy JSON keeps the same notion of
    "this magnetic core has already been worked out". It is always derived on
    the server; a browser-supplied key is never stored.
    """
    return re.sub(r'\s+', '', str(name)).lower()


class WindingEntry(models.Model):
    """One magnetic core worked out once and shared by every user."""

    name = models.CharField('Магнитопровод', max_length=120)
    # Unique in the database, not only in JavaScript: two users calculating
    # the same core concurrently must end up with one logical row.
    case_key = models.CharField('Ключ расчётного случая', max_length=120, unique=True)

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

    def __str__(self):
        return self.name

    def to_payload(self):
        """The entry in the shape the ported journal modules already expect."""
        return {
            'id': self.pk,
            'name': self.name,
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
            'calculationVersion': self.calculation_version,
        }
