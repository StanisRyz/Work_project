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
