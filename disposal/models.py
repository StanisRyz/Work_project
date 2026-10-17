"""«Утилизация МП»: the record that a prohibited «Цех МП» product was destroyed.

What waits for disposal is not stored at all — it is every «Цех МП» defect the
current КО decision prohibited from use (`disposal.selectors.pending_defects()`),
read off `acts.ActDefect` as it stands. Only the fact of disposal is stored:
one `DisposalAct` per «Утилизировать» (who, when) and one `DisposalItem` per
defect it covered. The one-to-one on the defect is the database's word that a
product is disposed of once.

An item snapshots what the signed paper says — the act's number, the product,
the order, ЗНП, party, МП type, defect type and the quantity — because the
paper is a document: an act returned for rework and edited later must not
rewrite a disposal already signed. The defect itself stays the only source of
defect data everywhere else.
"""

from django.contrib.auth.models import User
from django.db import models


class DisposalAct(models.Model):
    """One «Утилизировать»: the printed form the master signed."""

    disposed_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='disposal_acts', verbose_name='Утилизировал',
    )
    disposed_at = models.DateTimeField('Утилизировано', auto_now_add=True)

    class Meta:
        ordering = ['-disposed_at', '-pk']
        verbose_name = 'Акт утилизации МП'
        verbose_name_plural = 'Акты утилизации МП'

    def __str__(self):
        return self.label

    @property
    def label(self):
        return f'Акт утилизации №{self.pk}'


class DisposalItem(models.Model):
    """One prohibited МП covered by a `DisposalAct`, as it was printed."""

    disposal = models.ForeignKey(
        DisposalAct, on_delete=models.PROTECT, related_name='items', verbose_name='Акт утилизации',
    )
    defect = models.OneToOneField(
        'acts.ActDefect', on_delete=models.PROTECT, related_name='disposal_item', verbose_name='Дефект',
    )
    position = models.PositiveIntegerField('Порядок', default=0)
    act_number = models.CharField('Номер акта', max_length=32, blank=True)
    nomenclature = models.CharField('Номенклатура', max_length=240, blank=True)
    order_number = models.CharField('Номер заказа', max_length=80, blank=True)
    znp_number = models.CharField('Номер ЗНП', max_length=80, blank=True)
    party_number = models.CharField('Номер партии', max_length=120, blank=True)
    mp_type = models.CharField('Тип МП', max_length=20, blank=True)
    defect_type = models.CharField('Вид дефекта', max_length=255, blank=True)
    quantity = models.PositiveIntegerField('Количество, шт.', null=True, blank=True)

    class Meta:
        ordering = ['disposal', 'position', 'pk']
        verbose_name = 'Утилизированное МП'
        verbose_name_plural = 'Утилизированные МП'

    def __str__(self):
        return f'{self.disposal.label}: ЗНП {self.znp_number or "—"}'
