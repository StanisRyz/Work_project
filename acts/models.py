from pathlib import Path
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from references.models import ActStatus, DefectType, Operation, Priority


ACT_STATUS_CODES = {
    'CREATED_OTK': 'CREATED_OTK',
    'KO_REVIEW': 'KO_REVIEW',
    'TO_ANALYSIS': 'TO_ANALYSIS',
    'ACTIONS_ASSIGNED': 'ACTIONS_ASSIGNED',
    'CLOSED': 'CLOSED',
    'CANCELLED': 'CANCELLED',
}


def get_act_status(code):
    try:
        return ActStatus.objects.get(code=ACT_STATUS_CODES[code])
    except KeyError as exc:
        raise ValidationError(f'Unknown act status code: {code}') from exc
    except ActStatus.DoesNotExist as exc:
        raise ValidationError(
            f'Required act status "{ACT_STATUS_CODES[code]}" is missing. Run seed_references first.'
        ) from exc


def act_attachment_upload_to(instance, filename):
    extension = Path(filename).suffix.lower()
    act_id = instance.act_id or 'unassigned'
    return f'acts/attachments/{act_id}/{uuid4().hex}{extension}'


class Act(models.Model):
    class KoDecision(models.TextChoices):
        ALLOW = 'ALLOW', 'Пропустить'
        REJECT = 'REJECT', 'Не пропускать'
        RETURN = 'RETURN', 'Вернуть на уточнение'

    number = models.CharField('Номер акта', max_length=32, unique=True, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='created_acts',
        verbose_name='Создал',
    )
    party_number = models.CharField('Номер партии', max_length=120)
    nomenclature = models.CharField('Номенклатура', max_length=240)
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT, verbose_name='Операция')
    defect_type = models.ForeignKey(DefectType, on_delete=models.PROTECT, verbose_name='Вид дефекта')
    priority = models.ForeignKey(
        Priority,
        on_delete=models.PROTECT,
        verbose_name='Приоритет',
        blank=True,
        null=True,
    )
    status = models.ForeignKey(ActStatus, on_delete=models.PROTECT, verbose_name='Статус')
    description = models.TextField('Описание')
    due_date = models.DateField('Срок рассмотрения', blank=True, null=True)
    ko_decision = models.CharField(
        'Решение КО',
        max_length=20,
        choices=KoDecision.choices,
        blank=True,
    )
    ko_comment = models.TextField('Комментарий КО', blank=True)
    ko_decision_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='ko_decided_acts',
        blank=True,
        null=True,
        verbose_name='Решение КО внес',
    )
    ko_decision_at = models.DateTimeField('Дата решения КО', blank=True, null=True)
    to_root_cause = models.TextField('Корневая причина', blank=True)
    to_action_summary = models.TextField('Предлагаемые мероприятия', blank=True)
    to_analysis_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='to_analyzed_acts',
        blank=True,
        null=True,
        verbose_name='Анализ ТО внес',
    )
    to_analysis_at = models.DateTimeField('Дата анализа ТО', blank=True, null=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Акт'
        verbose_name_plural = 'Акты'

    def __str__(self):
        return self.number

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = self._generate_number()
        super().save(*args, **kwargs)

    @classmethod
    def _generate_number(cls):
        year = timezone.localdate().year
        prefix = f'АОК-{year}-'
        last_act = cls.objects.filter(number__startswith=prefix).order_by('-number').first()
        next_number = 1
        if last_act:
            try:
                next_number = int(last_act.number.rsplit('-', 1)[1]) + 1
            except (IndexError, ValueError):
                next_number = cls.objects.filter(number__startswith=prefix).count() + 1
        return f'{prefix}{next_number:03d}'


class ActHistoryEvent(models.Model):
    class EventType(models.TextChoices):
        CREATED = 'CREATED', 'Акт создан'
        SENT_TO_KO = 'SENT_TO_KO', 'Акт передан в КО'
        KO_DECISION_APPLIED = 'KO_DECISION_APPLIED', 'Решение КО внесено'
        RETURNED_TO_OTK = 'RETURNED_TO_OTK', 'Акт возвращён в ОТК'
        SENT_TO_TO = 'SENT_TO_TO', 'Акт передан в ТО'
        TO_ANALYSIS_APPLIED = 'TO_ANALYSIS_APPLIED', 'Анализ ТО внесён'
        COMMENT_ADDED = 'COMMENT_ADDED', 'Комментарий добавлен'
        ATTACHMENT_ADDED = 'ATTACHMENT_ADDED', 'Вложение добавлено'
        ATTACHMENT_DELETED = 'ATTACHMENT_DELETED', 'Вложение удалено'

    act = models.ForeignKey(
        Act,
        on_delete=models.CASCADE,
        related_name='history_events',
        verbose_name='Акт',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='act_history_events',
        verbose_name='Пользователь',
        blank=True,
        null=True,
    )
    event_type = models.CharField('Тип события', max_length=40, choices=EventType.choices)
    message = models.TextField('Сообщение')
    from_status = models.ForeignKey(
        ActStatus,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Статус до',
        blank=True,
        null=True,
    )
    to_status = models.ForeignKey(
        ActStatus,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Статус после',
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Событие истории акта'
        verbose_name_plural = 'События истории актов'

    def __str__(self):
        return f'{self.act.number}: {self.get_event_type_display()}'


class ActComment(models.Model):
    act = models.ForeignKey(
        Act,
        on_delete=models.CASCADE,
        related_name='comments',
        verbose_name='Акт',
    )
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='act_comments',
        verbose_name='Автор',
        blank=True,
        null=True,
    )
    text = models.TextField('Комментарий')
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Комментарий к акту'
        verbose_name_plural = 'Комментарии к актам'

    def __str__(self):
        author = self.author.get_username() if self.author else 'без автора'
        return f'{self.act.number}: {author}'


class ActAttachment(models.Model):
    act = models.ForeignKey(
        Act,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Акт',
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='act_attachments',
        verbose_name='Загрузил',
        blank=True,
        null=True,
    )
    file = models.FileField('Файл', upload_to=act_attachment_upload_to)
    original_name = models.CharField('Исходное имя файла', max_length=255)
    description = models.TextField('Описание', blank=True)
    file_size = models.PositiveIntegerField('Размер файла', default=0)
    content_type = models.CharField('Тип содержимого', max_length=120, blank=True)
    uploaded_at = models.DateTimeField('Загружено', auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']
        verbose_name = 'Вложение акта'
        verbose_name_plural = 'Вложения актов'

    def __str__(self):
        return f'{self.act.number}: {self.original_name}'
