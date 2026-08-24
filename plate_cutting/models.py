"""Saved package sets of the plate-cutting calculator.

Two tables and nothing more: a named set and its ordered packages. Only the
*inputs* of a package are stored — the length band, the number of plates and
the number of holes. Not a single second, hour, total or expanded formula ever
reaches the database, so a preset saved today is recalculated with whatever
`plate_cutting/constants.py` and `static/js/plate_cutting.js` say tomorrow.

The band is stored as the identifier the calculator's `<select>` already uses
(`PlateLengthRange.value`), so there is no second cutting-time table here: the
seconds are looked up in the constants module when a preset is loaded.
"""
from django.contrib.auth.models import User
from django.db import models

from .constants import RANGE_VALUE_MAX_LENGTH

#: A hand-filled calculator never comes near this; it only stops an absurd
#: payload from becoming an absurd number of rows.
MAX_PACKAGES_PER_PRESET = 50

PRESET_NAME_MAX_LENGTH = 120


def build_search_name(name):
    """The normalized name the modal's search field matches against.

    Derived, never entered: search has to behave identically on PostgreSQL
    and on the SQLite used for local runs, and SQLite's `LIKE` only folds
    ASCII — «Мартовский» would not match «мартовский» through `icontains`.
    Both sides are lowercased here instead, exactly as `calculator` derives
    its `case_key`.
    """
    return str(name or '').strip().lower()


class PlateCuttingPreset(models.Model):
    """One saved set, visible to and loadable by every authenticated user."""

    name = models.CharField('Название набора', max_length=PRESET_NAME_MAX_LENGTH)
    # `build_search_name(name)`, kept beside it so a substring search is one
    # indexed column lookup rather than a function over every row.
    search_name = models.CharField(
        'Ключ поиска', max_length=PRESET_NAME_MAX_LENGTH, db_index=True,
    )
    # PROTECT, not CASCADE: the author is traceability, and deleting an
    # account must not silently take saved sets other people use with it.
    author = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='plate_cutting_presets',
        verbose_name='Автор',
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Набор пакетов рубки пластин'
        verbose_name_plural = 'Наборы пакетов рубки пластин'

    def __str__(self):
        return self.name


class PlateCuttingPresetPackage(models.Model):
    """One package of a saved set: inputs only, in the order it was saved."""

    preset = models.ForeignKey(
        PlateCuttingPreset,
        on_delete=models.CASCADE,
        related_name='packages',
        verbose_name='Набор',
    )
    # The `<select>` identifier of a band from `constants.PLATE_LENGTH_RANGES`.
    # Deliberately not `choices`: the agreed bands are business constants, and
    # editing them must not require a migration of this table. The service
    # layer validates every identifier against those constants instead.
    range_value = models.CharField('Диапазон длины', max_length=RANGE_VALUE_MAX_LENGTH)
    plate_count = models.PositiveIntegerField('Количество пластин')
    hole_count = models.PositiveIntegerField('Количество отверстий')
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)

    class Meta:
        ordering = ['display_order', 'pk']
        verbose_name = 'Пакет набора рубки пластин'
        verbose_name_plural = 'Пакеты наборов рубки пластин'
        constraints = [
            models.UniqueConstraint(
                fields=['preset', 'display_order'],
                name='unique_plate_cutting_preset_package_order',
            ),
            # The calculator's own rule, kept at the database level: a package
            # without plates is not a package. Holes may legitimately be 0.
            models.CheckConstraint(
                condition=models.Q(plate_count__gte=1),
                name='plate_cutting_preset_package_plates_positive',
            ),
        ]

    def __str__(self):
        return f'{self.preset}: пакет {self.display_order + 1}'
