"""The printed act: form ДП-СМК 07/04.5 «Акт регистрации и устранения несоответствия».

The paper form describes **one** nonconformity, so the printable act is one
form per defect, in the act's order: the header and the first block are the
defect's own (workshop, ЗНП, party, quantities, description, the ОТК who
registered it), section 1 is that defect's КО decision with its «Анализ
влияния отклонений» (`acts/quality_impact.py`, never restated), and sections
2–4 — the corrective actions, root causes and their execution — are the act's,
read once and repeated on every form, as the paper repeats them. An act with no
defect (legacy) prints one form from the act's own decision.

Plain data only, built here and drawn by `templates/acts/print.html`: nothing
in the template decides. A box the system has no answer for — «силами
поставщика / производства», «Повторная приемка», the conclusion — is printed
empty, to be filled by hand; nothing is invented to fill it.
"""

from . import quality_impact
from .models import Act


FORM_CODE = 'Форма ДП-СМК 07/04.5'
COMPANY = 'ООО «ЗТМ»'

# Section 1 lists the four current decisions, in the paper's order.
DECISION_OPTIONS = (
    Act.KoDecision.PROHIBIT_USE,
    Act.KoDecision.ALLOW_NO_REWORK,
    Act.KoDecision.ALLOW_WITH_REWORK,
    Act.KoDecision.ALLOW_NO_DEVIATION_REWORK,
)


def person(user):
    """«Должность» and «ФИО» of somebody who signed a step, or empties.

    The position is the profile's own, else the role it holds — a person with
    neither is printed without one rather than with a made-up title.
    """
    if user is None:
        return {'position': '', 'name': ''}
    profile = getattr(user, 'userprofile', None)
    position = ''
    if profile is not None:
        position = (profile.position or '').strip() or profile.get_role_display()
    return {'position': position, 'name': user.get_full_name() or user.get_username()}


def signature(user, at):
    return {**person(user), 'date': at}


def _decision_rows(decision):
    rows = [
        {'label': Act.KoDecision(value).label, 'checked': decision == value}
        for value in DECISION_OPTIONS
    ]
    # A decision from before the current four (kept readable, never rewritten)
    # is printed as it is rather than lost.
    legacy = decision and decision not in DECISION_OPTIONS
    return rows, (Act.KoDecision(decision).label if legacy else '')


def _impact_rows(defect):
    values = quality_impact.values_of(defect) if defect is not None else dict(quality_impact.EMPTY_VALUES)
    rows = []
    for item in quality_impact.ITEMS:
        texts = [(values.get(name) or '').strip() for name in item.text_fields]
        rows.append({
            'field': item.field,
            'label': item.label,
            'checked': item.is_checked(values),
            'texts': texts,
        })
    return rows


def _defect_comments(act, defect):
    """The facts of the first block the paper has no row for, one per line."""
    lines = []
    if act.customer:
        lines.append(f'Заказчик: {act.customer}')
    if defect is not None:
        if defect.znp_number:
            lines.append(f'ЗНП: {defect.znp_number}')
        if defect.operation_id:
            lines.append(f'Операция: {defect.operation}')
        if defect.mp_type:
            lines.append(f'Тип МП: {defect.get_mp_type_display()}')
    return lines


def build_act_print_form(act):
    """Every form of the printed act, and the act-level sections they share."""
    defects = list(
        act.defects.select_related(
            'defect_type', 'operation', 'ko_decision_by__userprofile',
        ).order_by('created_at', 'pk')
    )
    analyses = list(
        act.root_analyses.prefetch_related(
            'corrective_actions__department',
            'corrective_actions__assignees__user',
        ).order_by('display_order', 'pk')
    )
    actions = [action for analysis in analyses for action in analysis.corrective_actions.all()]

    from tasks.models import Task

    tasks = list(
        Task.objects.filter(act=act, source_type=Task.SourceType.ACT)
        .exclude(status__code='CANCELLED')
        .select_related('status', 'completed_by')
    )
    done = [task for task in tasks if task.status.code == 'COMPLETED']
    last_done = max(done, key=lambda task: task.completed_at) if done and len(done) == len(tasks) else None

    single = actions[0] if len(actions) == 1 else None
    shared = {
        'causes': [analysis.root_cause for analysis in analyses] or (
            [act.to_root_cause] if act.to_root_cause else []
        ),
        'actions': [
            {
                'text': action.comment,
                'department': action.department.name,
                'due_date': action.due_date,
                'assignees': ', '.join(
                    a.user.get_full_name() or a.user.get_username() for a in action.assignees.all()
                ),
            }
            for action in actions
        ],
        'legacy_actions': act.to_action_summary if not actions else '',
        'to_signature': signature(act.to_analysis_by, act.to_analysis_at),
        'due_date': max((action.due_date for action in actions), default=None),
        'responsible': ', '.join(
            a.user.get_full_name() or a.user.get_username() for a in single.assignees.all()
        ) if single else '',
        'execution': {
            'total': len(tasks),
            'done': len(done),
            'signature': signature(last_done.completed_by, last_done.completed_at) if last_done else None,
        },
    }

    forms = []
    for index, defect in enumerate(defects or [None], start=1):
        decision = defect.ko_decision if defect is not None else act.ko_decision
        decision_rows, legacy_decision = _decision_rows(decision)
        ko_by = (defect.ko_decision_by if defect is not None and defect.ko_decision_by_id else act.ko_decision_by)
        ko_at = (defect.ko_decision_at if defect is not None and defect.ko_decision_at else act.ko_decision_at)
        comment = defect.ko_comment if defect is not None else act.ko_comment
        party = defect.party_number if defect is not None else ''
        forms.append({
            'index': index,
            'count': len(defects),
            'defect': defect,
            'party_order': ' / '.join(value for value in (party, act.order_number) if value),
            'comments': _defect_comments(act, defect),
            'otk_signature': signature(act.created_by, act.created_at),
            'decisions': decision_rows,
            'legacy_decision': legacy_decision,
            'impact': _impact_rows(defect),
            'ko_signature': signature(ko_by, ko_at) if decision else signature(None, None),
            'ko_comment': comment,
        })
    return {'form_code': FORM_CODE, 'company': COMPANY, 'forms': forms, 'shared': shared}
