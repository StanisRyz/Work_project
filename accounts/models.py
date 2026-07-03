from django.contrib.auth.models import User
from django.db import models


class Department(models.Model):
    name = models.CharField('Название', max_length=120)
    code = models.CharField('Код', max_length=32, unique=True)
    description = models.TextField('Описание', blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Подразделение'
        verbose_name_plural = 'Подразделения'

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    class Role(models.TextChoices):
        OTK = 'otk', 'ОТК'
        KO = 'ko', 'КО'
        TO = 'to', 'ТО'
        MANAGER = 'manager', 'Руководитель'
        ADMIN = 'admin', 'Администратор'

    user = models.OneToOneField(User, on_delete=models.CASCADE, verbose_name='Пользователь')
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        verbose_name='Подразделение',
        blank=True,
        null=True,
    )
    role = models.CharField('Роль', max_length=20, choices=Role.choices, default=Role.OTK)
    position = models.CharField('Должность', max_length=120, blank=True)
    internal_phone = models.CharField('Внутренний телефон', max_length=32, blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['user__username']
        verbose_name = 'Профиль пользователя'
        verbose_name_plural = 'Профили пользователей'

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        full_name = self.user.get_full_name()
        return full_name or self.user.username

    @property
    def role_label(self):
        return self.get_role_display()

    @property
    def department_label(self):
        return self.department.name if self.department else 'Без подразделения'
