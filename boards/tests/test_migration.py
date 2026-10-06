"""`boards.0006`–`0008`: the four fixed columns become «Основная» and back.

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
