"""`boards.0006`–`0008`: the four fixed columns become «Основная» and back;
`boards.0010`: the journal of cards stored before it; `boards.0015`: the card
fields; `boards.0016`: «Застой» of a column; `boards.0017`: the checklist,
the subscriptions and the mentions; `boards.0018`: the files of «Чат».

Run through `MigrationExecutor` on the test database: the board app is taken
back to `0005` (sub-boards exist, `stage` still rules), cards are written the
old way, and the migrations are applied forward and rolled back one step.
"""

from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


BEFORE = [('boards', '0005_sub_boards_and_columns')]
AFTER = [('boards', '0008_drop_card_stage')]
ONE_STEP_BACK = [('boards', '0007_sub_board_required')]


def migrate(targets):
    """Migrate `boards` to `targets`; the historical apps of everything applied."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor = MigrationExecutor(connection)
    return executor._create_project_state(with_applied_migrations=True).apps


class DefaultSubBoardMigrationTests(TransactionTestCase):
    # The test database keeps its seeded reference rows for the tests after.
    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _old_board(self, apps):
        """A board with cards in all three stages, one done and one cancelled."""
        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        BoardCard = apps.get_model('boards', 'BoardCard')
        Task = apps.get_model('tasks', 'Task')
        TaskStatus = apps.get_model('references', 'TaskStatus')

        statuses = {
            code: TaskStatus.objects.get_or_create(
                code=code, defaults={'name': code, 'is_final': code != 'IN_PROGRESS'},
            )[0]
            for code in ('IN_PROGRESS', 'COMPLETED', 'CANCELLED')
        }
        owner = User.objects.create(username='migration_owner')
        board = Board.objects.create(name='Старая доска', owner=owner)
        empty = Board.objects.create(name='Пустая доска', owner=owner)
        cards = {}
        for title, stage, position, status in (
            ('todo-1', 'TODO', 1024, 'IN_PROGRESS'),
            ('todo-2', 'TODO', 2048, 'IN_PROGRESS'),
            ('progress', 'IN_PROGRESS', 1536, 'IN_PROGRESS'),
            ('review', 'REVIEW', 777, 'IN_PROGRESS'),
            ('done', 'REVIEW', 3072, 'COMPLETED'),
            ('cancelled', 'IN_PROGRESS', 4096, 'CANCELLED'),
        ):
            card = BoardCard.objects.create(
                board=board, stage=stage, position=position, title=title, created_by=owner,
            )
            Task.objects.create(
                source_type='BOARD', board_card=card, task_text=title, due_date=date(2026, 10, 9),
                created_by=owner, status=statuses[status],
            )
            cards[title] = card.pk
        return board.pk, empty.pk, cards

    def test_forward_then_one_step_back(self):
        apps = migrate(BEFORE)
        board_id, empty_id, cards = self._old_board(apps)

        apps = migrate(AFTER)
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        BoardCard = apps.get_model('boards', 'BoardCard')
        for pk in (board_id, empty_id):
            sub_boards = list(SubBoard.objects.filter(board_id=pk))
            self.assertEqual([(sub.name, sub.position) for sub in sub_boards], [('Основная', 1)])
            self.assertEqual(
                list(
                    BoardColumn.objects.filter(sub_board=sub_boards[0])
                    .order_by('position').values_list('name', 'position', 'is_done')
                ),
                [('Сделать', 1, False), ('В работе', 2, False), ('На проверке', 3, False), ('Готово', 4, True)],
            )
        main = SubBoard.objects.get(board_id=board_id)
        column = {
            name: BoardColumn.objects.get(sub_board=main, name=name).pk
            for name in ('Сделать', 'В работе', 'На проверке')
        }
        placed = {
            card.title: (card.sub_board_id, card.column_id, card.position)
            for card in BoardCard.objects.filter(board_id=board_id)
        }
        self.assertEqual(placed, {
            'todo-1': (main.pk, column['Сделать'], 1024),
            'todo-2': (main.pk, column['Сделать'], 2048),
            'progress': (main.pk, column['В работе'], 1536),
            'review': (main.pk, column['На проверке'], 777),
            # A done card keeps the column it returns to when reopened; a
            # cancelled one keeps its record the same way.
            'done': (main.pk, column['На проверке'], 3072),
            'cancelled': (main.pk, column['В работе'], 4096),
        })
        self.assertEqual(BoardCard.objects.filter(board_id=board_id).count(), len(cards))

        # A column moved by hand before the rollback: `stage` follows the
        # place, not the name.
        BoardColumn.objects.filter(pk=column['На проверке']).update(position=1)
        BoardColumn.objects.filter(pk=column['Сделать']).update(position=3)
        apps = migrate(ONE_STEP_BACK)
        BoardCard = apps.get_model('boards', 'BoardCard')
        stages = dict(BoardCard.objects.filter(board_id=board_id).values_list('title', 'stage'))
        self.assertEqual(stages, {
            'todo-1': 'REVIEW',
            'todo-2': 'REVIEW',
            'progress': 'IN_PROGRESS',
            'review': 'TODO',
            'done': 'TODO',
            'cancelled': 'IN_PROGRESS',
        })

    def test_rolling_back_the_data_restores_the_original_stages(self):
        apps = migrate(BEFORE)
        board_id, _, _ = self._old_board(apps)
        migrate(AFTER)
        apps = migrate(BEFORE)
        BoardCard = apps.get_model('boards', 'BoardCard')
        SubBoard = apps.get_model('boards', 'SubBoard')
        self.assertEqual(
            dict(BoardCard.objects.filter(board_id=board_id).values_list('title', 'stage')),
            {
                'todo-1': 'TODO', 'todo-2': 'TODO', 'progress': 'IN_PROGRESS',
                'review': 'REVIEW', 'done': 'REVIEW', 'cancelled': 'IN_PROGRESS',
            },
        )
        self.assertFalse(SubBoard.objects.exists())
        self.assertFalse(BoardCard.objects.filter(sub_board__isnull=False).exists())
        # And forward again: the data migration runs a second time cleanly.
        apps = migrate(AFTER)
        self.assertEqual(apps.get_model('boards', 'SubBoard').objects.filter(board_id=board_id).count(), 1)


JOURNAL_BEFORE = [('boards', '0009_board_card_event')]
JOURNAL_AFTER = [('boards', '0010_backfill_card_events')]
NO_JOURNAL = [('boards', '0008_drop_card_stage')]


class CardJournalBackfillTests(TransactionTestCase):
    """`boards.0010`: the journal of cards stored before it, from facts only."""

    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _cards(self, apps):
        from datetime import datetime, timezone as dt_timezone

        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        BoardCard = apps.get_model('boards', 'BoardCard')
        BoardCardEvent = apps.get_model('boards', 'BoardCardEvent')
        Task = apps.get_model('tasks', 'Task')
        TaskStatus = apps.get_model('references', 'TaskStatus')

        statuses = {
            code: TaskStatus.objects.get_or_create(
                code=code, defaults={'name': code, 'is_final': code != 'IN_PROGRESS'},
            )[0]
            for code in ('IN_PROGRESS', 'COMPLETED', 'CANCELLED')
        }
        author = User.objects.create(username='journal_author')
        finisher = User.objects.create(username='journal_finisher')
        board = Board.objects.create(name='Доска с прошлым', owner=author)
        sub_board = SubBoard.objects.create(board=board, name='Основная', position=1, created_by=author)
        column = BoardColumn.objects.create(sub_board=sub_board, name='Сделать', position=1)
        when = {
            'created': datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc),
            'completed': datetime(2026, 9, 3, 15, 30, tzinfo=dt_timezone.utc),
            'cancelled': datetime(2026, 9, 4, 11, 0, tzinfo=dt_timezone.utc),
        }
        cards = {}
        for title, status, extra in (
            ('open', 'IN_PROGRESS', {}),
            ('done', 'COMPLETED', {'completed_by': finisher, 'completed_at': when['completed']}),
            ('cancelled', 'CANCELLED', {'cancelled_by': finisher, 'cancelled_at': when['cancelled']}),
            # Completed, but nobody is recorded as having done it: nothing is guessed.
            ('anonymous', 'COMPLETED', {'completed_at': when['completed']}),
        ):
            card = BoardCard.objects.create(
                board=board, sub_board=sub_board, column=column, position=1024, title=title,
                created_by=author,
            )
            BoardCard.objects.filter(pk=card.pk).update(created_at=when['created'])
            Task.objects.create(
                source_type='BOARD', board_card=card, task_text=title, due_date=date(2026, 10, 9),
                created_by=author, status=statuses[status], **extra,
            )
            cards[title] = card.pk
        # A card that already has its «создание» keeps exactly one.
        BoardCardEvent.objects.create(card_id=cards['open'], actor=author, kind='CREATED', details={})
        return cards, author.pk, finisher.pk, when

    def test_facts_only_and_a_second_run_adds_nothing(self):
        import importlib

        apps = migrate(JOURNAL_BEFORE)
        cards, author, finisher, when = self._cards(apps)

        apps = migrate(JOURNAL_AFTER)
        BoardCardEvent = apps.get_model('boards', 'BoardCardEvent')

        def journal():
            return sorted(BoardCardEvent.objects.values_list('card_id', 'kind', 'actor_id'))

        expected = sorted([
            (cards['open'], 'CREATED', author),
            (cards['done'], 'CREATED', author),
            (cards['done'], 'COMPLETED', finisher),
            (cards['cancelled'], 'CREATED', author),
            (cards['cancelled'], 'CANCELLED', finisher),
            (cards['anonymous'], 'CREATED', author),
        ])
        self.assertEqual(journal(), expected)
        # Each entry carries the time of the fact, not of the migration.
        stamped = {
            (event.card_id, event.kind): event.created_at
            for event in BoardCardEvent.objects.exclude(card_id=cards['open'])
        }
        self.assertEqual(stamped[(cards['done'], 'CREATED')], when['created'])
        self.assertEqual(stamped[(cards['done'], 'COMPLETED')], when['completed'])
        self.assertEqual(stamped[(cards['cancelled'], 'CANCELLED')], when['cancelled'])
        self.assertFalse(BoardCardEvent.objects.exclude(details={}).exists())

        backfill = importlib.import_module('boards.migrations.0010_backfill_card_events').backfill
        backfill(apps, None)
        self.assertEqual(journal(), expected)

    def test_rolling_the_journal_back_and_forward(self):
        apps = migrate(JOURNAL_BEFORE)
        self._cards(apps)
        migrate(JOURNAL_AFTER)
        apps = migrate(NO_JOURNAL)
        with self.assertRaises(LookupError):
            apps.get_model('boards', 'BoardCardEvent')
        apps = migrate(JOURNAL_AFTER)
        self.assertEqual(apps.get_model('boards', 'BoardCardEvent').objects.count(), 6)


NUMBERS_BEFORE = [('boards', '0010_backfill_card_events')]
NUMBERS_NULLABLE = [('boards', '0011_board_code_card_number')]
NUMBERS_AFTER = [('boards', '0014_board_column_pins')]


class CodesAndNumbersMigrationTests(TransactionTestCase):
    """`boards.0011`–`0013`: `D<pk>` for every board, its cards numbered by
    creation; and back."""

    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _boards(self, apps):
        from datetime import datetime, timezone as dt_timezone

        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        BoardCard = apps.get_model('boards', 'BoardCard')

        owner = User.objects.create(username='numbers_owner')
        boards, cards = {}, {}
        for name in ('Первая', 'Вторая', 'Пустая'):
            board = Board.objects.create(name=name, owner=owner)
            sub_board = SubBoard.objects.create(board=board, name='Основная', position=1, created_by=owner)
            column = BoardColumn.objects.create(sub_board=sub_board, name='Сделать', position=1)
            boards[name] = (board.pk, sub_board, column)
        # Created out of `pk` order, and two at the same moment (then by `pk`).
        for title, board_name, day in (
            ('late', 'Первая', 3), ('early', 'Первая', 1), ('same-a', 'Первая', 2),
            ('same-b', 'Первая', 2), ('only', 'Вторая', 5),
        ):
            board_id, sub_board, column = boards[board_name]
            card = BoardCard.objects.create(
                board_id=board_id, sub_board=sub_board, column=column, position=1024,
                title=title, created_by=owner,
            )
            BoardCard.objects.filter(pk=card.pk).update(
                created_at=datetime(2026, 9, day, 9, 0, tzinfo=dt_timezone.utc),
            )
            cards[title] = card.pk
        return {name: value[0] for name, value in boards.items()}, cards

    def test_forward_codes_and_numbers_then_back(self):
        apps = migrate(NUMBERS_BEFORE)
        boards, cards = self._boards(apps)

        apps = migrate(NUMBERS_AFTER)
        Board = apps.get_model('boards', 'Board')
        BoardCard = apps.get_model('boards', 'BoardCard')
        self.assertEqual(
            dict(Board.objects.values_list('pk', 'code')),
            {pk: f'D{pk}' for pk in boards.values()},
        )
        numbers = dict(BoardCard.objects.values_list('title', 'number'))
        self.assertEqual(numbers, {'early': 1, 'same-a': 2, 'same-b': 3, 'late': 4, 'only': 1})
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        self.assertEqual(set(BoardColumn.objects.values_list('pinned_mode', flat=True)), {'ADD'})

        apps = migrate(NUMBERS_NULLABLE)
        Board = apps.get_model('boards', 'Board')
        BoardCard = apps.get_model('boards', 'BoardCard')
        self.assertEqual(set(Board.objects.values_list('code', flat=True)), {None})
        self.assertEqual(set(BoardCard.objects.values_list('number', flat=True)), {None})

        apps = migrate(NUMBERS_BEFORE)
        field_names = {field.name for field in apps.get_model('boards', 'BoardCard')._meta.get_fields()}
        self.assertNotIn('number', field_names)

        apps = migrate(NUMBERS_AFTER)
        BoardCard = apps.get_model('boards', 'BoardCard')
        self.assertEqual(dict(BoardCard.objects.values_list('title', 'number'))['late'], 4)


FIELDS_BEFORE = [('boards', '0014_board_column_pins')]
FIELDS_AFTER = [('boards', '0015_board_fields')]


def _tables():
    return set(connection.introspection.table_names())


class BoardFieldsMigrationTests(TransactionTestCase):
    """`boards.0015`: three new tables with their constraints; and back."""

    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_forward_with_constraints_then_back(self):
        from django.db import IntegrityError, transaction

        tables = {'boards_boardfield', 'boards_boardfieldoption', 'boards_boardcardfieldvalue'}
        migrate(FIELDS_BEFORE)
        self.assertFalse(tables & _tables())

        apps = migrate(FIELDS_AFTER)
        self.assertEqual(tables & _tables(), tables)
        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        BoardCard = apps.get_model('boards', 'BoardCard')
        BoardField = apps.get_model('boards', 'BoardField')
        BoardFieldOption = apps.get_model('boards', 'BoardFieldOption')
        BoardCardFieldValue = apps.get_model('boards', 'BoardCardFieldValue')
        owner = User.objects.create(username='fields_migration_owner')
        board = Board.objects.create(name='Доска', code='FM', owner=owner)
        sub_board = SubBoard.objects.create(board=board, name='Основная', position=1, created_by=owner)
        column = BoardColumn.objects.create(sub_board=sub_board, name='Сделать', position=1)
        card = BoardCard.objects.create(
            board=board, sub_board=sub_board, column=column, position=1024, number=1,
            title='Карточка', created_by=owner,
        )
        field = BoardField.objects.create(board=board, name='Приоритет', kind='SELECT', position=1)
        option = BoardFieldOption.objects.create(field=field, label='Высокий', color='red', position=1)
        BoardCardFieldValue.objects.create(card=card, field=field, option=option)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardCardFieldValue.objects.create(card=card, field=field, value_text='второе')
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardFieldOption.objects.create(field=field, label='Розовый', color='pink', position=2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardField.objects.create(board=board, name='Пустое', kind='LIST', position=2)

        # Back with the rows in place: the tables simply go.
        migrate(FIELDS_BEFORE)
        self.assertFalse(tables & _tables())


STALE_BEFORE = [('boards', '0015_board_fields')]
STALE_AFTER = [('boards', '0016_column_stale_days')]


def _column_names(table):
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


class ColumnStaleDaysMigrationTests(TransactionTestCase):
    """`boards.0016`: a nullable `stale_after_days` with its check; and back."""

    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_forward_nullable_with_its_check_then_back(self):
        from django.db import IntegrityError, transaction

        apps = migrate(STALE_BEFORE)
        self.assertNotIn('stale_after_days', _column_names('boards_boardcolumn'))
        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        owner = User.objects.create(username='stale_migration_owner')
        board = Board.objects.create(name='Доска', code='SM', owner=owner)
        sub_board = SubBoard.objects.create(board=board, name='Основная', position=1, created_by=owner)
        working = BoardColumn.objects.create(sub_board=sub_board, name='Сделать', position=1)
        done = BoardColumn.objects.create(sub_board=sub_board, name='Готово', position=2, is_done=True)

        apps = migrate(STALE_AFTER)
        self.assertIn('stale_after_days', _column_names('boards_boardcolumn'))
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        # Every existing column is «off».
        self.assertEqual(
            set(BoardColumn.objects.filter(pk__in=[working.pk, done.pk]).values_list('stale_after_days', flat=True)),
            {None},
        )
        BoardColumn.objects.filter(pk=working.pk).update(stale_after_days=3)
        for pk, days in ((working.pk, 0), (working.pk, 366), (done.pk, 3)):
            with self.subTest(pk=pk, days=days), self.assertRaises(IntegrityError), transaction.atomic():
                BoardColumn.objects.filter(pk=pk).update(stale_after_days=days)

        # Back with a threshold set: the column simply goes, the rows stay.
        apps = migrate(STALE_BEFORE)
        self.assertNotIn('stale_after_days', _column_names('boards_boardcolumn'))
        self.assertEqual(apps.get_model('boards', 'BoardColumn').objects.filter(sub_board_id=sub_board.pk).count(), 2)


CHECKLIST_BEFORE = [('boards', '0016_column_stale_days')]
CHECKLIST_AFTER = [('boards', '0017_checklist_subscriptions_mentions')]
CHECKLIST_TABLES = {
    'boards_boardcardchecklistitem', 'boards_boardcardsubscription', 'boards_boardcardcommentmention',
}


def _table_names():
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


class ChecklistMigrationTests(TransactionTestCase):
    """`boards.0017`: the checklist, the subscriptions and the mentions, three
    new tables; and back — the cards, their messages and their journal stay."""

    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_forward_three_tables_with_their_constraints_then_back(self):
        from django.db import IntegrityError, transaction

        apps = migrate(CHECKLIST_BEFORE)
        self.assertFalse(CHECKLIST_TABLES & _table_names())
        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        BoardCard = apps.get_model('boards', 'BoardCard')
        BoardCardComment = apps.get_model('boards', 'BoardCardComment')
        owner = User.objects.create(username='checklist_migration_owner')
        board = Board.objects.create(name='Доска', code='CM', owner=owner)
        sub_board = SubBoard.objects.create(board=board, name='Основная', position=1, created_by=owner)
        column = BoardColumn.objects.create(sub_board=sub_board, name='Сделать', position=1)
        card = BoardCard.objects.create(
            board=board, sub_board=sub_board, column=column, position=1024, number=1,
            title='Карточка', created_by=owner,
        )
        comment = BoardCardComment.objects.create(card=card, author=owner, text='Сообщение')

        apps = migrate(CHECKLIST_AFTER)
        self.assertLessEqual(CHECKLIST_TABLES, _table_names())
        Item = apps.get_model('boards', 'BoardCardChecklistItem')
        Subscription = apps.get_model('boards', 'BoardCardSubscription')
        Mention = apps.get_model('boards', 'BoardCardCommentMention')
        Event = apps.get_model('boards', 'BoardCardEvent')
        # Nothing to backfill: every card starts with none of them.
        self.assertEqual((Item.objects.count(), Subscription.objects.count(), Mention.objects.count()), (0, 0, 0))
        Item.objects.create(card_id=card.pk, text='Шаг', position=1, created_by_id=owner.pk)
        Subscription.objects.create(card_id=card.pk, user_id=owner.pk)
        Mention.objects.create(comment_id=comment.pk, user_id=owner.pk)
        Event.objects.create(card_id=card.pk, actor_id=owner.pk, kind='CHECKLIST', details={'action': 'added'})
        for model, values in (
            (Subscription, {'card_id': card.pk, 'user_id': owner.pk}),
            (Mention, {'comment_id': comment.pk, 'user_id': owner.pk}),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.create(**values)

        # Back: the three tables go; the card, its message and its journal stay.
        apps = migrate(CHECKLIST_BEFORE)
        self.assertFalse(CHECKLIST_TABLES & _table_names())
        self.assertTrue(apps.get_model('boards', 'BoardCard').objects.filter(pk=card.pk).exists())
        self.assertTrue(apps.get_model('boards', 'BoardCardComment').objects.filter(pk=comment.pk).exists())
        self.assertEqual(apps.get_model('boards', 'BoardCardEvent').objects.filter(card_id=card.pk).count(), 1)


CHAT_FILES_BEFORE = [('boards', '0017_checklist_subscriptions_mentions')]
CHAT_FILES_AFTER = [('boards', '0018_chat_files')]


class ChatFilesMigrationTests(TransactionTestCase):
    """`boards.0018`: the files of «Чат», one new table; and back — the cards
    and their messages stay, and nothing was copied out of the task's
    attachments either way."""

    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_forward_one_table_then_back(self):
        apps = migrate(CHAT_FILES_BEFORE)
        self.assertNotIn('boards_boardcardfile', _table_names())
        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        BoardCard = apps.get_model('boards', 'BoardCard')
        BoardCardComment = apps.get_model('boards', 'BoardCardComment')
        owner = User.objects.create(username='chat_files_migration_owner')
        board = Board.objects.create(name='Доска', code='CF', owner=owner)
        sub_board = SubBoard.objects.create(board=board, name='Основная', position=1, created_by=owner)
        column = BoardColumn.objects.create(sub_board=sub_board, name='Сделать', position=1)
        card = BoardCard.objects.create(
            board=board, sub_board=sub_board, column=column, position=1024, number=1,
            title='Карточка', created_by=owner,
        )
        comment = BoardCardComment.objects.create(card=card, author=owner, text='Сообщение')

        apps = migrate(CHAT_FILES_AFTER)
        self.assertIn('boards_boardcardfile', _table_names())
        CardFile = apps.get_model('boards', 'BoardCardFile')
        # Nothing to backfill: no message had a file.
        self.assertEqual(CardFile.objects.count(), 0)
        CardFile.objects.create(
            card_id=card.pk, comment_id=comment.pk, uploaded_by_id=owner.pk,
            file='boards/files/1/x.pdf', original_name='x.pdf', size=3,
        )
        apps.get_model('boards', 'BoardCardComment').objects.create(card_id=card.pk, author_id=owner.pk, text='')

        # Back: the table goes; the card and both its messages stay.
        apps = migrate(CHAT_FILES_BEFORE)
        self.assertNotIn('boards_boardcardfile', _table_names())
        self.assertTrue(apps.get_model('boards', 'BoardCard').objects.filter(pk=card.pk).exists())
        self.assertEqual(apps.get_model('boards', 'BoardCardComment').objects.filter(card_id=card.pk).count(), 2)


SUBTASKS_BEFORE = [('boards', '0018_chat_files')]
SUBTASKS_AFTER = [('boards', '0019_card_subtasks')]


def _columns_of(table):
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


class SubtasksMigrationTests(TransactionTestCase):
    """`boards.0019`: `BoardCard.parent` and «a subtask stands in no column»;
    nothing is classified — every existing card stays a card — and back."""

    serialized_rollback = True

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_forward_one_column_and_a_constraint_then_back(self):
        from django.db import IntegrityError, transaction

        apps = migrate(SUBTASKS_BEFORE)
        self.assertNotIn('parent_id', _columns_of('boards_boardcard'))
        User = apps.get_model('auth', 'User')
        Board = apps.get_model('boards', 'Board')
        SubBoard = apps.get_model('boards', 'SubBoard')
        BoardColumn = apps.get_model('boards', 'BoardColumn')
        BoardCard = apps.get_model('boards', 'BoardCard')
        owner = User.objects.create(username='subtasks_migration_owner')
        board = Board.objects.create(name='Доска', code='SB', owner=owner)
        sub_board = SubBoard.objects.create(board=board, name='Основная', position=1, created_by=owner)
        column = BoardColumn.objects.create(sub_board=sub_board, name='Сделать', position=1)
        card = BoardCard.objects.create(
            board=board, sub_board=sub_board, column=column, position=1024, number=1,
            title='Заказ', created_by=owner,
        )

        apps = migrate(SUBTASKS_AFTER)
        BoardCard = apps.get_model('boards', 'BoardCard')
        # Nothing classified: the card is a card of its column, as it was.
        self.assertEqual(
            BoardCard.objects.filter(pk=card.pk).values_list('parent_id', 'column_id').get(),
            (None, column.pk),
        )
        subtask = BoardCard.objects.create(
            board_id=board.pk, sub_board_id=sub_board.pk, parent_id=card.pk, position=1, number=2,
            title='Позиция', created_by_id=owner.pk,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardCard.objects.filter(pk=subtask.pk).update(column_id=column.pk)
        Event = apps.get_model('boards', 'BoardCardEvent')
        Event.objects.create(card_id=card.pk, actor_id=owner.pk, kind='SUBTASK', details={'action': 'added'})

        # Back: the column goes; both cards and the journal entry stay.
        apps = migrate(SUBTASKS_BEFORE)
        self.assertNotIn('parent_id', _columns_of('boards_boardcard'))
        self.assertEqual(apps.get_model('boards', 'BoardCard').objects.filter(board_id=board.pk).count(), 2)
        self.assertEqual(apps.get_model('boards', 'BoardCardEvent').objects.filter(kind='SUBTASK').count(), 1)
