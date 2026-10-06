"""Board codes and card numbers, step two: the existing rows.

Every board gets `D<pk>` — Latin, and derived from nothing but its own key,
so no code is guessed from a name — and its cards are numbered 1, 2, 3, … in
the order they were created (`created_at`, then `pk`), cancelled ones
included: a number is never reused. The owner changes the code in the
board's «⋯» menu afterwards.

Reversible: going back empties both columns again, which `0011`'s reverse
then drops.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Board = apps.get_model('boards', 'Board')
    BoardCard = apps.get_model('boards', 'BoardCard')

    for board in Board.objects.order_by('pk'):
        if not board.code:
            board.code = f'D{board.pk}'
            board.save(update_fields=['code'])
        cards = list(BoardCard.objects.filter(board=board).order_by('created_at', 'pk'))
        for number, card in enumerate(cards, start=1):
            card.number = number
        BoardCard.objects.bulk_update(cards, ['number'])


def clear(apps, schema_editor):
    apps.get_model('boards', 'Board').objects.update(code=None)
    apps.get_model('boards', 'BoardCard').objects.update(number=None)


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0011_board_code_card_number'),
    ]

    operations = [migrations.RunPython(backfill, clear)]
