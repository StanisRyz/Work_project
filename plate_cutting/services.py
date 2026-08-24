"""The only place a saved package set is written or read back.

Everything the browser sends is re-checked here against the same rules the
page enforces for immediate feedback — a non-empty name, at least one package,
a band that exists in `constants.PLATE_LENGTH_RANGES`, plates > 0 and holes >= 0,
and both counters within `models.MAX_PLATE_COUNT` / `models.MAX_HOLE_COUNT`.
`create_preset()` writes the set and all of its packages in one transaction, so
an invalid package leaves nothing behind at all.

No result is ever stored or returned from here: the payloads carry inputs only,
and the calculator recomputes the seconds and hours after loading.
"""
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .constants import find_range
from .models import (
    MAX_HOLE_COUNT,
    MAX_PACKAGES_PER_PRESET,
    MAX_PLATE_COUNT,
    PRESET_NAME_MAX_LENGTH,
    PlateCuttingPreset,
    PlateCuttingPresetPackage,
    build_search_name,
)

#: How many rows one search answers with. The library is expected to grow; the
#: modal is a picker with a search field, not a report.
SEARCH_LIMIT = 50


class PlateCuttingValidationError(Exception):
    """Rejected input, carrying the message the modal shows as it is."""


def _author_name(user):
    """The display name shown next to a preset — never an email or a role."""
    return user.get_full_name() or user.get_username()


def _integer(value, label, minimum, maximum):
    """A whole number from JSON, within *minimum*..*maximum*.

    The conversion itself is the check: `str.isdigit()` cannot be used as a
    guard because it answers True for strings `int()` refuses — `'--5'` after
    stripping signs, and superscripts such as `'²'` — which turned malformed
    input into an uncaught `ValueError` and an HTTP 500 instead of the
    message the modal shows.

    *maximum* is not optional. Both stored counters are `PositiveIntegerField`,
    i.e. PostgreSQL `integer`: a value above its ceiling aborts the insert
    rather than being rejected, and only in production. See `MAX_PLATE_COUNT`.
    """
    if isinstance(value, bool):
        raise PlateCuttingValidationError(f'{label}: введите целое число.')
    if isinstance(value, int):
        number = value
    else:
        text = str(value if value is not None else '').strip()
        try:
            number = int(text)
        except (TypeError, ValueError):
            raise PlateCuttingValidationError(f'{label}: введите целое число.')
    if number < minimum:
        raise PlateCuttingValidationError(
            f'{label}: значение должно быть не меньше {minimum}.'
        )
    if number > maximum:
        raise PlateCuttingValidationError(
            f'{label}: значение должно быть не больше {maximum}.'
        )
    return number


def _clean_name(value):
    name = str(value if value is not None else '').strip()
    if not name:
        raise PlateCuttingValidationError('Укажите название набора.')
    if len(name) > PRESET_NAME_MAX_LENGTH:
        raise PlateCuttingValidationError(
            f'Название набора не длиннее {PRESET_NAME_MAX_LENGTH} символов.'
        )
    return name


def _clean_packages(raw_packages):
    """The packages to store, validated and numbered in the order they came."""
    if not isinstance(raw_packages, list) or not raw_packages:
        raise PlateCuttingValidationError('Добавьте хотя бы один пакет.')
    if len(raw_packages) > MAX_PACKAGES_PER_PRESET:
        raise PlateCuttingValidationError(
            f'В одном наборе не больше {MAX_PACKAGES_PER_PRESET} пакетов.'
        )

    cleaned = []
    for index, raw in enumerate(raw_packages, start=1):
        if not isinstance(raw, dict):
            raise PlateCuttingValidationError(f'Пакет {index}: некорректные данные.')
        band = find_range(raw.get('range'))
        if band is None:
            raise PlateCuttingValidationError(
                f'Пакет {index}: выберите диапазон длины пластины.'
            )
        cleaned.append({
            'range_value': band.value,
            'plate_count': _integer(
                raw.get('plates'), f'Пакет {index}: количество пластин', 1, MAX_PLATE_COUNT,
            ),
            'hole_count': _integer(
                raw.get('holes'), f'Пакет {index}: количество отверстий', 0, MAX_HOLE_COUNT,
            ),
            'display_order': index - 1,
        })
    return cleaned


@transaction.atomic
def create_preset(user, data):
    """Store the current package structure under a name. All or nothing.

    Both cleaners run *before* the first `INSERT`, and the whole call is one
    transaction, so a rejected package cannot leave a half-saved set behind.
    """
    if not isinstance(data, dict):
        raise PlateCuttingValidationError('Некорректный запрос.')
    name = _clean_name(data.get('name'))
    packages = _clean_packages(data.get('packages'))

    preset = PlateCuttingPreset.objects.create(
        name=name, search_name=build_search_name(name), author=user,
    )
    PlateCuttingPresetPackage.objects.bulk_create(
        PlateCuttingPresetPackage(preset=preset, **package) for package in packages
    )
    return preset


def search_presets(query='', limit=SEARCH_LIMIT):
    """Saved sets whose name contains *query*, newest first.

    Substring matching done by the database against the normalized
    `search_name` column, so it is case-insensitive for Cyrillic on every
    backend. No full-text index and no search dependency. Every authenticated
    user sees every set.
    """
    presets = (
        PlateCuttingPreset.objects
        .select_related('author')
        # Counted by the database, so the modal's list is one query.
        .annotate(package_count=Count('packages'))
    )
    text = build_search_name(query)
    if text:
        presets = presets.filter(search_name__contains=text)
    return list(presets[:limit])


def preset_summary(preset):
    """One row of the «Загрузить набор» list. Inputs and traceability only."""
    return {
        'id': preset.pk,
        'name': preset.name,
        'author': _author_name(preset.author),
        'created_at': timezone.localtime(preset.created_at).strftime('%d.%m.%Y'),
        'package_count': getattr(preset, 'package_count', None) or preset.packages.count(),
    }


def preset_detail(preset):
    """The authoritative structure the calculator rebuilds itself from."""
    return {
        'id': preset.pk,
        'name': preset.name,
        'author': _author_name(preset.author),
        'created_at': timezone.localtime(preset.created_at).strftime('%d.%m.%Y'),
        'packages': [
            {
                'range': package.range_value,
                'plates': package.plate_count,
                'holes': package.hole_count,
            }
            # `Meta.ordering` is `display_order` first: the saved order is the
            # order the packages are rebuilt in.
            for package in preset.packages.all()
        ],
    }
