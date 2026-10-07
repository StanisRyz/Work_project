from django.db import models


class Operation(models.Model):
    name = models.CharField('Название', max_length=160)
    code = models.CharField('Код', max_length=64, unique=True)
    description = models.TextField('Описание', blank=True)
    sort_order = models.PositiveIntegerField('Порядок', default=100)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = 'Операция'
        verbose_name_plural = 'Операции'

    def __str__(self):
        return self.name


class DefectType(models.Model):
    name = models.CharField('Название', max_length=160)
    code = models.CharField('Код', max_length=64, unique=True)
    description = models.TextField('Описание', blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Вид дефекта'
        verbose_name_plural = 'Виды дефектов'

    def __str__(self):
        return self.name


class ActStatus(models.Model):
    name = models.CharField('Название', max_length=160)
    code = models.CharField('Код', max_length=64, unique=True)
    description = models.TextField('Описание', blank=True)
    sort_order = models.PositiveIntegerField('Порядок', default=100)
    is_final = models.BooleanField('Финальный', default=False)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = 'Статус акта'
        verbose_name_plural = 'Статусы актов'

    def __str__(self):
        return self.name


class TaskStatus(models.Model):
    name = models.CharField('Название', max_length=160)
    code = models.CharField('Код', max_length=64, unique=True)
    description = models.TextField('Описание', blank=True)
    sort_order = models.PositiveIntegerField('Порядок', default=100)
    is_final = models.BooleanField('Финальный', default=False)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = 'Статус задачи'
        verbose_name_plural = 'Статусы задач'

    def __str__(self):
        return self.name


class Priority(models.Model):
    name = models.CharField('Название', max_length=160)
    code = models.CharField('Код', max_length=64, unique=True)
    description = models.TextField('Описание', blank=True)
    sort_order = models.PositiveIntegerField('Порядок', default=100)
    color = models.CharField('Цвет', max_length=32, blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = 'Приоритет'
        verbose_name_plural = 'Приоритеты'

    def __str__(self):
        return self.name


class DeviationReason(models.Model):
    """Why a deadline moved — «Ждём материал (снабжение)», «Брак», …

    A plant-wide dictionary, kept in Django Admin only (there are no pages).
    A board's «перенос срока» names one (`boards.BoardCardDueChange.reason`).
    An inactive reason is no longer offered but keeps reading in the history
    of the moves that named it; a reason that was named is never deleted
    (`PROTECT`). Seeded by `references.0005` and `seed_references`.
    """

    code = models.CharField('Код', max_length=64, unique=True)
    name = models.CharField('Название', max_length=160)
    is_active = models.BooleanField('Активна', default=True)
    display_order = models.PositiveIntegerField('Порядок', default=100)

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = 'Причина отклонения'
        verbose_name_plural = 'Причины отклонений'

    def __str__(self):
        return self.name


# The reasons a fresh installation starts with: `(code, name, display order)`.
# `references.0005` and `seed_references` both read this list.
DEVIATION_REASONS = (
    ('MATERIAL', 'Ждём материал (снабжение)', 10),
    ('DESIGN', 'Нет КД / ждём конструктора', 20),
    ('DEFECT', 'Брак', 30),
    ('EQUIPMENT', 'Оборудование', 40),
    ('PAYMENT', 'Ждём оплату', 50),
    ('CUSTOMER_CHANGE', 'Изменение заказа покупателем', 60),
    ('CAPACITY', 'Загрузка цеха', 70),
    ('OTHER', 'Другое', 80),
)
