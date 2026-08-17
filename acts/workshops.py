"""Workshop policy for act defects — the one place a цех's rules live.

`ActDefect` is the canonical source of defect data, and a workshop decides
which of its fields apply, which of them the user must fill, what has to be
cleared when the workshop does not collect it, and which reference defect types
may be chosen. `acts/forms.py` validates against these profiles and the browser
only mirrors `client_config()`, so a new workshop needs a new choice on
`ActDefect.Workshop`, one profile here and its defect type codes — nothing
spread across forms, views and JavaScript.

This module deliberately imports no models: `acts/models.py` reads the workshop
codes from it for its database constraints.
"""

from dataclasses import dataclass


MP_SHOP = 'MP_SHOP'
PIR_SHOP = 'PIR_SHOP'

# Every field of a defect the create/edit form may collect. A profile lists the
# subset its workshop actually applies; the rest is cleared before persistence.
DEFECT_FIELDS = (
    'workshop',
    'znp_number',
    'party_number',
    'defect_type',
    'operation',
    'mp_type',
    'detected_at',
    'checked_quantity',
    'nonconforming_quantity',
    'description',
)

# What a non-applicable field is reset to. Anything not listed becomes `''`.
NON_APPLICABLE_VALUES = {'operation': None}


@dataclass(frozen=True)
class WorkshopProfile:
    """Applicability, required set, defect types and form presentation."""

    code: str
    label: str
    fields: tuple
    required_fields: tuple
    defect_type_codes: tuple
    # Presentation only — the browser mirrors it, the backend never reads it.
    legend: str
    detected_at_group: str

    @property
    def non_applicable_fields(self):
        return tuple(name for name in DEFECT_FIELDS if name not in self.fields)

    def applies(self, field_name):
        return field_name in self.fields

    def requires(self, field_name):
        return field_name in self.required_fields

    def non_applicable_value(self, field_name):
        return NON_APPLICABLE_VALUES.get(field_name, '')

    def allows_defect_type(self, code):
        return code in self.defect_type_codes

    def as_client_config(self):
        return {
            'label': self.label,
            'fields': list(self.fields),
            'required': list(self.required_fields),
            'defect_types': list(self.defect_type_codes),
            'legend': self.legend,
            'detected_at_group': self.detected_at_group,
        }


# Defect types offered by the МП workshop — unchanged.
MP_DEFECT_TYPE_CODES = (
    'SIZE_NONCONFORMITY',
    'DEFORMATION',
    'ASYMMETRIC_CUT',
    'OBLIQUE_CUT',
    'GRINDING_SIZE_DEVIATION',
    'END_FACE_DELAMINATION_DAMAGE',
    'CUT_SURFACE_DELAMINATION',
    'OL_WINDING_TENSION_LOSS',
    'WINDING_SHIFT',
    'HIGH_ROUGHNESS',
    'OTHER',
)
# The ПиР workshop reuses three of the same reference records and nothing else.
PIR_DEFECT_TYPE_CODES = (
    'DEFORMATION',
    'OTHER',
    'SIZE_NONCONFORMITY',
)

# ПиР collects neither a party number, nor an operation, nor a МП type, nor a
# description, so those four never reach the database for a ПиР defect.
PIR_DEFECT_FIELDS = (
    'workshop',
    'znp_number',
    'defect_type',
    'detected_at',
    'checked_quantity',
    'nonconforming_quantity',
)

MP_PROFILE = WorkshopProfile(
    code=MP_SHOP,
    label='Цех МП',
    fields=DEFECT_FIELDS,
    required_fields=DEFECT_FIELDS,
    defect_type_codes=MP_DEFECT_TYPE_CODES,
    legend='Партия',
    detected_at_group='result',
)

PIR_PROFILE = WorkshopProfile(
    code=PIR_SHOP,
    label='Цех ПиР',
    fields=PIR_DEFECT_FIELDS,
    required_fields=PIR_DEFECT_FIELDS,
    defect_type_codes=PIR_DEFECT_TYPE_CODES,
    legend='Цех',
    detected_at_group='control',
)

WORKSHOP_PROFILES = {
    MP_SHOP: MP_PROFILE,
    PIR_SHOP: PIR_PROFILE,
}

# Every defect type any workshop may offer; the per-workshop set is what
# actually gets validated.
ALL_DEFECT_TYPE_CODES = tuple(
    dict.fromkeys(
        code
        for profile in WORKSHOP_PROFILES.values()
        for code in profile.defect_type_codes
    )
)


def universally_required_fields():
    """Fields every workshop requires, so the form may mark them required.

    Anything else is required by the selected profile in `ActDefectForm.clean`,
    which keeps the browser from marking a field required under a workshop that
    does not collect it at all.
    """
    return tuple(
        name
        for name in DEFECT_FIELDS
        if all(profile.requires(name) for profile in WORKSHOP_PROFILES.values())
    )


def get_profile(workshop):
    """Return the profile of `workshop`, or `None` when it has none."""
    return WORKSHOP_PROFILES.get(workshop or '')


def client_config():
    """Compact presentation metadata for `static/js/act_create.js`."""
    return {code: profile.as_client_config() for code, profile in WORKSHOP_PROFILES.items()}
