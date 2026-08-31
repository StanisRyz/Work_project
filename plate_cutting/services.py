"""Validation and transactional writes for plate-cutting presets."""
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from .constants import find_range
from .models import (
    MAX_HOLE_COUNT,
    MAX_PACKAGES_PER_PRESET,
    MAX_PLATE_COUNT,
    MAX_SET_QUANTITY,
    PRESET_NAME_MAX_LENGTH,
    PlateCuttingPreset,
    PlateCuttingPresetPackage,
    build_search_name,
)
from .permissions import can_manage_plate_cutting_presets

SEARCH_LIMIT = 50
CONFLICT_CREATE = 'create'
CONFLICT_OVERWRITE = 'overwrite'
CONFLICT_SAVE_AS_NEW = 'save_as_new'
CONFLICT_ACTIONS = frozenset({
    CONFLICT_CREATE,
    CONFLICT_OVERWRITE,
    CONFLICT_SAVE_AS_NEW,
})


class PlateCuttingValidationError(Exception):
    """Rejected input, carrying the message returned by the JSON endpoint."""


class PlateCuttingPermissionError(Exception):
    """A persisted preset mutation was attempted without management rights."""


class PlateCuttingNameConflict(Exception):
    """A normal create used a logical name already present in the library."""

    def __init__(self, preset):
        super().__init__('Набор с таким названием уже существует.')
        self.preset = preset


def _assert_can_manage(user):
    if not can_manage_plate_cutting_presets(user):
        raise PlateCuttingPermissionError(
            'У вас нет прав на изменение библиотеки наборов.'
        )


def _author_name(user):
    return user.get_full_name() or user.get_username()


def _integer(value, label, minimum, maximum):
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
                raw.get('plates'), f'Пакет {index}: количество пластин',
                1, MAX_PLATE_COUNT,
            ),
            'hole_count': _integer(
                raw.get('holes'), f'Пакет {index}: количество отверстий',
                0, MAX_HOLE_COUNT,
            ),
            'display_order': index - 1,
        })
    return cleaned


def _clean_payload(data):
    if not isinstance(data, dict):
        raise PlateCuttingValidationError('Некорректный запрос.')
    action = str(data.get('conflict_action') or CONFLICT_CREATE)
    if action not in CONFLICT_ACTIONS:
        raise PlateCuttingValidationError('Некорректный способ сохранения набора.')
    return (
        _clean_name(data.get('name')),
        _integer(
            data.get('set_quantity', 1), 'Количество наборов',
            1, MAX_SET_QUANTITY,
        ),
        _clean_packages(data.get('packages')),
        action,
    )


def _create_packages(preset, packages):
    PlateCuttingPresetPackage.objects.bulk_create(
        PlateCuttingPresetPackage(preset=preset, **package)
        for package in packages
    )


def _suffixed_name(base_name, number):
    suffix = f'_{number:02d}'
    return f'{base_name[:PRESET_NAME_MAX_LENGTH - len(suffix)]}{suffix}'


def _create_unique_suffixed_preset(user, name, set_quantity):
    """Create the first free suffixed row, retrying after a concurrent win."""
    number = 1
    while True:
        candidate = _suffixed_name(name, number)
        try:
            with transaction.atomic():
                return PlateCuttingPreset.objects.create(
                    name=candidate,
                    search_name=build_search_name(candidate),
                    set_quantity=set_quantity,
                    author=user,
                )
        except IntegrityError:
            number += 1


@transaction.atomic
def create_preset(user, data):
    """Create, overwrite or create a suffixed copy as one transaction."""
    _assert_can_manage(user)
    name, set_quantity, packages, action = _clean_payload(data)
    search_name = build_search_name(name)
    existing = (
        PlateCuttingPreset.objects.select_for_update()
        .filter(search_name=search_name)
        .first()
    )

    if existing and action == CONFLICT_CREATE:
        raise PlateCuttingNameConflict(existing)

    if action == CONFLICT_OVERWRITE:
        if existing is None:
            raise PlateCuttingValidationError(
                'Существующий набор уже удалён. Сохраните набор заново.'
            )
        existing.name = name
        existing.set_quantity = set_quantity
        existing.save(update_fields=['name', 'set_quantity', 'updated_at'])
        existing.packages.all().delete()
        _create_packages(existing, packages)
        return existing, False

    if action == CONFLICT_SAVE_AS_NEW:
        preset = _create_unique_suffixed_preset(user, name, set_quantity)
        _create_packages(preset, packages)
        return preset, True

    try:
        with transaction.atomic():
            preset = PlateCuttingPreset.objects.create(
                name=name,
                search_name=search_name,
                set_quantity=set_quantity,
                author=user,
            )
    except IntegrityError:
        conflicting = (
            PlateCuttingPreset.objects.select_for_update()
            .get(search_name=search_name)
        )
        raise PlateCuttingNameConflict(conflicting)
    _create_packages(preset, packages)
    return preset, True


@transaction.atomic
def delete_preset(user, preset_id):
    """Delete one locked preset; its package rows cascade in the transaction."""
    _assert_can_manage(user)
    preset = PlateCuttingPreset.objects.select_for_update().filter(pk=preset_id).first()
    if preset is None:
        raise PlateCuttingValidationError('Набор не найден.')
    preset.delete()


def search_presets(query='', limit=SEARCH_LIMIT):
    presets = (
        PlateCuttingPreset.objects
        .select_related('author')
        .annotate(package_count=Count('packages'))
    )
    text = build_search_name(query)
    if text:
        presets = presets.filter(search_name__contains=text)
    return list(presets[:limit])


def preset_summary(preset):
    return {
        'id': preset.pk,
        'name': preset.name,
        'author': _author_name(preset.author),
        'created_at': timezone.localtime(preset.created_at).strftime('%d.%m.%Y'),
        'package_count': getattr(preset, 'package_count', None) or preset.packages.count(),
        'set_quantity': preset.set_quantity,
    }


def preset_detail(preset):
    return {
        'id': preset.pk,
        'name': preset.name,
        'author': _author_name(preset.author),
        'created_at': timezone.localtime(preset.created_at).strftime('%d.%m.%Y'),
        'set_quantity': preset.set_quantity,
        'packages': [
            {
                'range': package.range_value,
                'plates': package.plate_count,
                'holes': package.hole_count,
            }
            for package in preset.packages.all()
        ],
    }
