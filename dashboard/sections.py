"""Which sections the «Быстрый доступ» grid offers, and to whom.

One list, read once by the view. Every entry names a route by its URL name and
carries the *existing* permission rule of the section behind it — this module
invents no rights of its own. A card is drawn only when its rule says yes, and
hiding it is a convenience: the view behind it re-checks the same rule and
refuses a URL typed by hand, exactly as it did before this page existed.

Today only «Документация» is restricted (`documents.permissions`); the other
five sections are open to every authenticated user, which is what
`_always()` states rather than leaving the key out.
"""

from django.urls import reverse

from documents.permissions import can_view_documents


def _always(user):
    """Open to every authenticated user — the rule the section's own view applies."""
    return bool(getattr(user, 'is_authenticated', False))


# `icon` names a `<symbol>` in the sprite at the top of `dashboard/home.html`;
# `accent` picks one of the tints in `static/css/dashboard.css`. Neither is a
# permission, and neither is read anywhere but the template.
QUICK_ACCESS_SECTIONS = (
    {
        'code': 'acts',
        'label': 'Акты',
        'description': 'Создание и просмотр актов',
        'url_name': 'acts:list',
        'icon': 'acts',
        'accent': 'blue',
        'is_visible': _always,
    },
    {
        'code': 'protocols',
        'label': 'Протоколы',
        'description': 'Протоколы проверок и совещаний',
        'url_name': 'protocols:list',
        'icon': 'protocols',
        'accent': 'violet',
        'is_visible': _always,
    },
    {
        'code': 'smk',
        'label': 'СМК',
        'description': 'Процессы и документы системы качества',
        'url_name': 'smk:list',
        'icon': 'smk',
        'accent': 'green',
        'is_visible': _always,
    },
    {
        'code': 'tasks',
        'label': 'Задачи',
        'description': 'Мои задачи и поручения',
        'url_name': 'tasks:list',
        'icon': 'tasks',
        'accent': 'amber',
        'is_visible': _always,
    },
    {
        # One card for the whole «Калькуляторы» category, opening the winding
        # calculator; the second calculator stays one click away in the
        # navigation submenu rather than doubling the grid.
        'code': 'calculator',
        'label': 'Калькуляторы',
        'description': 'Расчетные инструменты и шаблоны',
        'url_name': 'calculator:page',
        'icon': 'calculator',
        'accent': 'blue',
        'is_visible': _always,
    },
    {
        'code': 'documents',
        'label': 'Документация',
        'description': 'Нормативные и справочные документы',
        'url_name': 'documents:browse',
        'icon': 'documents',
        'accent': 'blue',
        'is_visible': can_view_documents,
    },
)


def get_quick_access_sections(user):
    """The cards this user may see, in declaration order, with resolved URLs."""
    return [
        {
            'code': section['code'],
            'label': section['label'],
            'description': section['description'],
            'icon': section['icon'],
            'accent': section['accent'],
            'url': reverse(section['url_name']),
        }
        for section in QUICK_ACCESS_SECTIONS
        if section['is_visible'](user)
    ]
