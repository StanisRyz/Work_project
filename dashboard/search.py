"""The topbar's quick search: one box that finds an act, a protocol, a task or
an СМК record by what people actually type.

Read-only and rights-neutral. Every group is drawn from the owning module's
own readable queryset — `acts.permissions`, `protocols.selectors`,
`tasks.permissions` — so a hit is always something the same user could reach
by clicking through that section; this file adds no visibility rule of its
own. Matching is deliberately small (`icontains` on the identifiers and the
one or two texts a person remembers a document by), not a full-text index.

What people type, and what it finds:
- «АОК-2026-00104», «104», «Россети», «катушка» → acts by number, customer,
  nomenclature;
- «Качество 4», «Качество №4», «№4» → protocols by type and number, and any
  protocol whose повестка mentions the words;
- «12», «задача 12», «обучение» → tasks by number or wording;
- «СМК 3», «СМК №3» → the СМК record.
"""

import re
from dataclasses import dataclass

from django.db.models import Q
from django.urls import reverse

from acts.models import Act
from acts.permissions import get_visible_acts_filter
from protocols.selectors import get_readable_protocols_queryset
from smk.models import SmkSource
from tasks.permissions import get_readable_tasks_queryset
from tasks.presentation import describe_task_type

MIN_LENGTH = 2
GROUP_LIMIT = 8
_NUMBER = re.compile(r'(\d+)\s*$')


@dataclass(frozen=True)
class Hit:
    title: str
    subtitle: str
    url: str


def _trailing_number(term):
    match = _NUMBER.search(term)
    return int(match.group(1)) if match else None


def _acts(user, term):
    condition = get_visible_acts_filter(user)
    if condition is None:
        return []
    acts = (
        Act.objects.filter(condition)
        .filter(Q(number__icontains=term) | Q(customer__icontains=term) | Q(nomenclature__icontains=term))
        .select_related('status')
        .order_by('-created_at', '-pk')[:GROUP_LIMIT]
    )
    return [
        Hit(
            title=act.number or 'Акт б/н',
            subtitle=' · '.join(part for part in (act.nomenclature, act.customer, str(act.status)) if part),
            url=reverse('acts:detail', args=[act.pk]),
        )
        for act in acts
    ]


def _protocols(term):
    number = _trailing_number(term)
    name = _NUMBER.sub('', term).replace('№', '').strip()
    criteria = Q(agenda_items__text__icontains=term)
    if number is not None:
        by_number = Q(number=number)
        if name:
            by_number &= Q(protocol_type__name__icontains=name)
        criteria |= by_number
    elif name:
        criteria |= Q(protocol_type__name__icontains=name)
    protocols = (
        get_readable_protocols_queryset().filter(criteria).distinct()
        .order_by('-created_at', '-pk')[:GROUP_LIMIT]
    )
    return [
        Hit(
            title=f'{protocol.protocol_type.name} №{protocol.number}',
            subtitle=f'{protocol.get_status_display()} · {protocol.created_at:%d.%m.%Y}',
            url=reverse('protocols:detail', args=[protocol.pk]),
        )
        for protocol in protocols
    ]


def _tasks(user, term):
    number = _trailing_number(term)
    prefix = _NUMBER.sub('', term).replace('№', '').strip().lower()
    criteria = Q(task_text__icontains=term)
    # A task is found by its number only when the term *is* a number: «12»,
    # «№12», «задача 12». «Качество 4» must not find task №4.
    if number is not None and prefix in ('', 'задача'):
        criteria |= Q(pk=number)
    tasks = (
        get_readable_tasks_queryset(user).filter(criteria).select_related('status')
        .order_by('status__is_final', 'due_date', 'pk')[:GROUP_LIMIT]
    )
    return [
        Hit(
            title=f'Задача №{task.pk}',
            subtitle=f'{task.task_text[:90]} · {describe_task_type(task)} · {task.status}',
            url=reverse('tasks:detail', args=[task.pk]),
        )
        for task in tasks
    ]


def _smk(term):
    number = _trailing_number(term)
    name = _NUMBER.sub('', term).replace('№', '').strip().lower()
    if number is None or name not in ('', 'смк'):
        return []
    return [
        Hit(
            title=source.label,
            subtitle=f'{source.get_origin_display()} · {source.audit_date:%d.%m.%Y}' if source.audit_date else source.get_origin_display(),
            url=reverse('smk:detail', args=[source.pk]),
        )
        for source in SmkSource.objects.filter(pk=number)
    ]


def quick_search(user, term):
    """`[(group label, [Hit, …]), …]` — only the groups that found something."""
    term = (term or '').strip()
    if len(term) < MIN_LENGTH:
        return []
    groups = (
        ('Акты', _acts(user, term)),
        ('Протоколы', _protocols(term)),
        ('Задачи', _tasks(user, term)),
        ('СМК', _smk(term)),
    )
    return [(label, hits) for label, hits in groups if hits]
