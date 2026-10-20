"""Which sections the «Разделы» list offers, and to whom.

One list, read once by the view. Every entry names a route by its URL name —
or, for a category with no page of its own («Калькуляторы»), `links` to each
of its pages, so the row offers the choice the navigation's submenu does — and
carries the *existing* permission rule of the section behind it — this module
invents no rights of its own. A card is drawn only when its rule says yes, and
hiding it is a convenience: the view behind it re-checks the same rule and
refuses a URL typed by hand, exactly as it did before this page existed.

«Документация» asks its own rule (`documents.permissions.can_view_documents`,
today «every signed-in employee»; which folders they then see is the library's
business) and «Доски» asks board access (`boards.permissions.can_use_boards`);
the other sections are open to every authenticated user, which is what
`_always()` states rather than leaving the key out.
"""

from django.urls import reverse

from boards.permissions import can_use_boards, can_use_requests
from documents.permissions import can_view_documents


def _always(user):
    """Open to every authenticated user — the rule the section's own view applies."""
    return bool(getattr(user, 'is_authenticated', False))


# `icon` names a `<symbol>` in the sprite at the top of `dashboard/home.html`.
# It is not a permission, and it is read nowhere but the template.
QUICK_ACCESS_SECTIONS = (
    {
        'code': 'acts',
        'label': 'Акты',
        'description': 'Создание и просмотр актов',
        'url_name': 'acts:list',
        'icon': 'acts',
        'is_visible': _always,
    },
    {
        'code': 'protocols',
        'label': 'Протоколы',
        'description': 'Протоколы проверок и совещаний',
        'url_name': 'protocols:list',
        'icon': 'protocols',
        'is_visible': _always,
    },
    {
        'code': 'smk',
        'label': 'СМК',
        'description': 'Процессы и документы системы качества',
        'url_name': 'smk:list',
        'icon': 'smk',
        'is_visible': _always,
    },
    {
        'code': 'tasks',
        'label': 'Задачи',
        'description': 'Мои задачи и поручения',
        'url_name': 'tasks:list',
        'icon': 'tasks',
        'is_visible': _always,
    },
    {
        # «Калькуляторы» is a category, as in the navigation: no page of its
        # own, so its row names the calculators and each is its own link.
        'code': 'calculator',
        'label': 'Калькуляторы',
        'description': 'Расчетные инструменты',
        'url_name': None,
        'links': (
            ('Время навивки', 'calculator:page'),
            ('Рубка пластин', 'plate_cutting:page'),
        ),
        'icon': 'calculator',
        'is_visible': _always,
    },
    {
        'code': 'documents',
        'label': 'Документация',
        'description': 'Нормативные и справочные документы',
        'url_name': 'documents:browse',
        'icon': 'documents',
        'is_visible': can_view_documents,
    },
    {
        'code': 'boards',
        'label': 'Доски',
        'description': 'Канбан-доски отделов',
        'url_name': 'boards:list',
        'icon': 'boards',
        'is_visible': can_use_boards,
    },
    {
        # «Заявки»: file a request to a board that takes them, and follow
        # one's own — for every active employee while some board does.
        'code': 'requests',
        'label': 'Заявки',
        'description': 'Подать заявку на доску и следить за ней',
        'url_name': 'boards:requests',
        'icon': 'requests',
        'is_visible': can_use_requests,
    },
)


def get_quick_access_sections(user):
    """The sections this user may open, in declaration order, with resolved URLs."""
    return [
        {
            'code': section['code'],
            'label': section['label'],
            'description': section['description'],
            'icon': section['icon'],
            'url': reverse(section['url_name']) if section['url_name'] else '',
            'links': [
                {'label': label, 'url': reverse(url_name)}
                for label, url_name in section.get('links', ())
            ],
        }
        for section in QUICK_ACCESS_SECTIONS
        if section['is_visible'](user)
    ]
