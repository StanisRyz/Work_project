"""«Правила при входе» of a column and «Действия» of a board.

Two pages in the boards' frame, read by every reader of the board and set up
by whoever manages it (`can_manage_board()`): a working column's entry rules
(`/work/boards/<board>/columns/<column>/rules/` — its pins, its checklist
template, its field values, its followers; `services._apply_column_entry()`
is what they do to a card) and the board's actions «Передать дальше»
(`/work/boards/<board>/actions/`; `services.run_board_action()` is what a
button does).

Every right is asked before the HTTP method, every write is an ordinary POST
form calling `boards/services.py`, a GET on a writing route changes nothing,
and a refusal comes back as a message (or as the form with what was typed).
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import (
    ActionForm,
    ColumnFieldRulesForm,
    ColumnFollowersForm,
    DirectionForm,
    TemplateItemForm,
    active_members,
)
from .models import (
    MAX_ACTIONS,
    MAX_TEMPLATE_ITEMS,
    BoardAction,
    BoardActionFieldRule,
    BoardColumn,
    BoardColumnChecklistTemplate,
    BoardColumnFieldRule,
    BoardColumnFollower,
    BoardField,
)
from .permissions import board_readers_q, can_manage_board, can_view_board
from .services import (
    BoardError,
    FieldValueError,
    add_template_item,
    archive_action,
    create_action,
    delete_template_item,
    move_action,
    move_template_item,
    set_column_field_rules,
    set_column_followers,
    update_action,
)
from .views import _board_or_404, _frame, _refused, _require


def _board_columns(board):
    """Every column of the board with its sub-board, in the board's order — one query."""
    return list(
        BoardColumn.objects.filter(sub_board__board=board).select_related('sub_board')
        .order_by('sub_board__position', 'sub_board_id', 'position', 'pk')
    )


def _live_fields(board):
    return list(
        BoardField.objects.filter(board=board, is_archived=False)
        .prefetch_related('options').order_by('position', 'pk')
    )


def _field_error(form, exc):
    """A `FieldValueError` beside its field's rule; anything else on the form."""
    if isinstance(exc, FieldValueError) and f'rule_{exc.field_id}' in form.fields:
        form.add_error(f'rule_{exc.field_id}', str(exc))
    else:
        form.add_error(None, str(exc))


# --------------------------------------------------------------------------
# «Правила при входе»
# --------------------------------------------------------------------------


def _rules_url(board, column):
    return reverse('boards:column_rules', args=[board.pk, column.pk])


def _rules_column(request, pk, column_pk, *, manage):
    """The board and its working column; a reader for the page, the manager
    for a change — asked before the method. The closing column has no page."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board) if manage else can_view_board(request.user, board))
    column = get_object_or_404(
        BoardColumn.objects.select_related('sub_board'), pk=column_pk, sub_board__board=board,
    )
    if column.is_done:
        raise Http404('У завершающей колонки нет правил при входе.')
    return board, column


def describe_rule_value(field, raw):
    """A stored rule's value in words — a list option by its label."""
    if field.kind == BoardField.Kind.SELECT:
        option = next((option for option in field.options.all() if str(option.pk) == raw), None)
        if option is None:
            return '—'
        return option.label + (' (в архиве)' if option.is_archived else '')
    if field.kind == BoardField.Kind.DATE and len(raw) == 10:
        return f'{raw[8:10]}.{raw[5:7]}.{raw[0:4]}'
    return raw


def _render_rules(request, board, column, *, fields_form=None, template_text='', template_error='',
                  status=200):
    can_manage = can_manage_board(request.user, board)
    fields = _live_fields(board)
    rules = {
        rule.field_id: rule
        for rule in BoardColumnFieldRule.objects.filter(column=column).select_related('field')
    }
    if fields_form is None:
        fields_form = ColumnFieldRulesForm(fields=fields, rules=rules)
    pinned_ids = set(column.pins.values_list('user_id', flat=True))
    follower_ids = set(BoardColumnFollower.objects.filter(column=column).values_list('user_id', flat=True))
    template = list(BoardColumnChecklistTemplate.objects.filter(column=column).order_by('position', 'pk'))
    readers = list(
        get_user_model().objects.filter(board_readers_q(board)).distinct()
        .order_by('last_name', 'first_name', 'username', 'pk')
    )
    archived_rules = [
        rule for rule in BoardColumnFieldRule.objects.filter(column=column, field__is_archived=True)
        .select_related('field')
    ]
    return render(request, 'boards/column_rules.html', {
        **_frame(request, board),
        'header_title': f'Правила колонки · {board.name}',
        'board': board,
        'column': column,
        'can_manage': can_manage and not board.is_archived,
        'members': list(active_members(board)) if can_manage else [],
        'pinned_ids': pinned_ids,
        'pinned_people': [person for person in readers if person.pk in pinned_ids],
        'template': template,
        'template_full': len(template) >= MAX_TEMPLATE_ITEMS,
        'template_limit': MAX_TEMPLATE_ITEMS,
        'template_text': template_text,
        'template_error': template_error,
        'fields_form': fields_form,
        'rule_summary': [
            (field, describe_rule_value(field, rules[field.pk].value), rules[field.pk].overwrite)
            for field in fields if field.pk in rules
        ],
        'archived_rules': archived_rules,
        'readers': readers,
        'follower_ids': follower_ids,
        'followers': [person for person in readers if person.pk in follower_ids],
        'actions_here': list(
            BoardAction.objects.filter(board=board, target_column=column, is_archived=False)
            .order_by('position', 'pk')
        ),
    }, status=status)


@login_required
def column_rules(request, pk, column_pk):
    """The page: what a card entering this working column gets."""
    board, column = _rules_column(request, pk, column_pk, manage=False)
    return _render_rules(request, board, column)


@login_required
def template_add(request, pk, column_pk):
    board, column = _rules_column(request, pk, column_pk, manage=True)
    if request.method != 'POST':
        return redirect(_rules_url(board, column))
    form = TemplateItemForm(request.POST)
    text = form.data.get('text', '')
    try:
        add_template_item(column, actor=request.user, text=text)
    except BoardError as exc:
        return _render_rules(request, board, column, template_text=text, template_error=str(exc), status=400)
    messages.success(request, 'Пункт шаблона добавлен.')
    return redirect(f'{_rules_url(board, column)}#template')


def _template_item_route(request, pk, column_pk, item_pk, write):
    board, column = _rules_column(request, pk, column_pk, manage=True)
    item = get_object_or_404(BoardColumnChecklistTemplate, pk=item_pk, column=column)
    if request.method == 'POST':
        try:
            write(item)
        except BoardError as exc:
            messages.error(request, str(exc))
    return redirect(f'{_rules_url(board, column)}#template')


@login_required
def template_move(request, pk, column_pk, item_pk):
    def write(item):
        form = DirectionForm(request.POST)
        if not form.is_valid():
            raise BoardError('Неизвестное направление.')
        move_template_item(item, actor=request.user, direction=form.cleaned_data['direction'])

    return _template_item_route(request, pk, column_pk, item_pk, write)


@login_required
def template_delete(request, pk, column_pk, item_pk):
    def write(item):
        delete_template_item(item, actor=request.user)
        messages.success(request, 'Пункт шаблона удалён. Карточки, которые его получили, его сохранят.')

    return _template_item_route(request, pk, column_pk, item_pk, write)


@login_required
def rules_fields(request, pk, column_pk):
    board, column = _rules_column(request, pk, column_pk, manage=True)
    if request.method != 'POST':
        return redirect(_rules_url(board, column))
    fields = _live_fields(board)
    rules = {rule.field_id: rule for rule in BoardColumnFieldRule.objects.filter(column=column)}
    form = ColumnFieldRulesForm(request.POST, fields=fields, rules=rules)
    if form.is_valid():
        try:
            changed = set_column_field_rules(column, actor=request.user, values=form.rule_values())
        except BoardError as exc:
            _field_error(form, exc)
        else:
            messages.success(request, 'Значения полей сохранены.' if changed else 'Ничего не изменилось.')
            return redirect(f'{_rules_url(board, column)}#fields')
    return _render_rules(request, board, column, fields_form=form, status=400)


@login_required
def rules_followers(request, pk, column_pk):
    board, column = _rules_column(request, pk, column_pk, manage=True)
    if request.method != 'POST':
        return redirect(_rules_url(board, column))
    form = ColumnFollowersForm(request.POST)
    if not form.is_valid():
        _refused(request, form)
    else:
        try:
            changed = set_column_followers(column, actor=request.user, user_ids=form.cleaned_data['users'])
        except BoardError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Подписчики сохранены.' if changed else 'Ничего не изменилось.')
    return redirect(f'{_rules_url(board, column)}#followers')


# --------------------------------------------------------------------------
# «Действия»
# --------------------------------------------------------------------------


def _actions_url(board, anchor=''):
    url = reverse('boards:actions', args=[board.pk])
    return f'{url}#{anchor}' if anchor else url


def _actions_board(request, pk, *, manage):
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board) if manage else can_view_board(request.user, board))
    return board


def _action_rules(action):
    return {rule.field_id: rule for rule in BoardActionFieldRule.objects.filter(action=action)}


def _render_actions(request, board, *, form=None, editing=None, status=200):
    """The list, «+ Действие» (`?new=1`, or a refused one) and one action's
    edit form (`?edit=<id>`, or a refused one)."""
    can_manage = can_manage_board(request.user, board) and not board.is_archived
    columns = _board_columns(board)
    fields = _live_fields(board)
    actions = list(
        BoardAction.objects.filter(board=board)
        .select_related('target_column__sub_board')
        .prefetch_related('assignee_rows__user', 'field_rules__field__options')
        .order_by('is_archived', 'position', 'pk')
    )
    if editing is None and can_manage and request.GET.get('edit', '').isdigit():
        editing = next((action for action in actions if action.pk == int(request.GET['edit'])), None)
    creating = form is not None and editing is None or (
        form is None and can_manage and request.GET.get('new') == '1'
    )
    if form is None and can_manage:
        if editing is not None:
            form = ActionForm(
                board=board, columns=columns, fields=fields, rules=_action_rules(editing),
                initial=ActionForm.initial_for(editing),
            )
        elif creating:
            form = ActionForm(board=board, columns=columns, fields=fields)
    live = [action for action in actions if not action.is_archived]
    rows = []
    for action in actions:
        rows.append({
            'action': action,
            'people': [row.user for row in action.assignee_rows.all()],
            'rules': [
                (rule.field, describe_rule_value(rule.field, rule.value), rule.overwrite)
                for rule in action.field_rules.all()
            ],
            'can_move_left': not action.is_archived and live and live[0].pk != action.pk,
            'can_move_right': not action.is_archived and live and live[-1].pk != action.pk,
        })
    return render(request, 'boards/actions.html', {
        **_frame(request, board),
        'header_title': f'Действия · {board.name}',
        'board': board,
        'rows': rows,
        'live_count': len(live),
        'limit': MAX_ACTIONS,
        'can_manage': can_manage,
        'form': form,
        'editing': editing,
        'creating': creating,
    }, status=status)


@login_required
def actions_page(request, pk):
    """«Действия» of a board: every reader reads them, the manager sets them up."""
    board = _actions_board(request, pk, manage=False)
    return _render_actions(request, board)


@login_required
def action_create(request, pk):
    board = _actions_board(request, pk, manage=True)
    if request.method != 'POST':
        return redirect(_actions_url(board))
    form = ActionForm(request.POST, board=board, columns=_board_columns(board), fields=_live_fields(board))
    if form.is_valid():
        try:
            action = create_action(board, actor=request.user, **form.service_kwargs())
        except BoardError as exc:
            _field_error(form, exc)
        else:
            messages.success(request, f'Действие «{action.name}» добавлено — его кнопка появилась в карточках.')
            return redirect(_actions_url(board, f'action-{action.pk}'))
    return _render_actions(request, board, form=form, status=400)


def _action_or_404(board, action_pk):
    return get_object_or_404(BoardAction, pk=action_pk, board=board)


@login_required
def action_update(request, pk, action_pk):
    board = _actions_board(request, pk, manage=True)
    action = _action_or_404(board, action_pk)
    if request.method != 'POST':
        return redirect(f'{_actions_url(board)}?edit={action.pk}')
    form = ActionForm(
        request.POST, board=board, columns=_board_columns(board), fields=_live_fields(board),
        rules=_action_rules(action),
    )
    if form.is_valid():
        try:
            update_action(action, actor=request.user, **form.service_kwargs())
        except BoardError as exc:
            _field_error(form, exc)
        else:
            messages.success(request, f'Действие «{form.cleaned_data["name"].strip()}» сохранено.')
            return redirect(_actions_url(board, f'action-{action.pk}'))
    return _render_actions(request, board, form=form, editing=action, status=400)


def _action_route(request, pk, action_pk, write):
    board = _actions_board(request, pk, manage=True)
    action = _action_or_404(board, action_pk)
    if request.method == 'POST':
        try:
            write(action)
        except BoardError as exc:
            messages.error(request, str(exc))
    return redirect(_actions_url(board, f'action-{action.pk}'))


@login_required
def action_move(request, pk, action_pk):
    def write(action):
        form = DirectionForm(request.POST)
        if not form.is_valid():
            raise BoardError('Неизвестное направление.')
        move_action(action, actor=request.user, direction=form.cleaned_data['direction'])

    return _action_route(request, pk, action_pk, write)


@login_required
def action_archive(request, pk, action_pk):
    def write(action):
        archive_action(action, actor=request.user, archived=True)
        messages.success(request, f'Действие «{action.name}» убрано в архив — его кнопки больше нет.')

    return _action_route(request, pk, action_pk, write)


@login_required
def action_restore(request, pk, action_pk):
    def write(action):
        archive_action(action, actor=request.user, archived=False)
        messages.success(request, f'Действие «{action.name}» снова в карточках.')

    return _action_route(request, pk, action_pk, write)
