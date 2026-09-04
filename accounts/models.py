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
        # Планово-диспетчерский отдел: owns the calculator's «Проработка»
        # journal. A first-class role like the others — never a department
        # check, never a synonym of any existing role.
        PDO = 'pdo', 'ПДО'
        # Manufacturing supervisors are ordinary operational users. Their
        # organisational department is separate metadata and grants no rights.
        MAS = 'mas', 'Мастер производства'
        # Отдел СМК: owns the quality-management-system corrective actions.
        # A first-class role like the others — never a department check, and
        # it grants nothing outside the SMK module.
        SMK = 'smk', 'СМК'
        # The remaining departments, as first-class roles like every other.
        # They carry *no* rights of their own on purpose: an employee holding
        # one reads what any authenticated user reads — «Все акты», «Архив»,
        # протоколы, СМК, задачи — and completes the tasks assigned to them
        # personally, which `tasks.permissions.can_complete_task()` already
        # allows on the strength of `TaskAssignee` and never on a role. Give
        # one of them a permission only by adding it to an existing rule in the
        # module that owns it, never by inventing a check here.
        OPR = 'opr', 'Отдел продаж'
        OZK = 'ozk', 'Отдел закупок'
        LAB = 'lab', 'Лаборатория'
        SKL = 'skl', 'Склад'
        FEO = 'feo', 'ФЭО'
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
    # Who receives «Сообщить об ошибке» from the topbar. Deliberately a flag on
    # the profile rather than a role: reporting a bug is not a quality-workflow
    # right, the people who handle them are chosen individually, and any role
    # may be one. Set in Django Admin and nowhere else — there is no page for
    # it, exactly as there is none for roles or departments.
    is_bug_responsible = models.BooleanField('Ответственный за ошибки', default=False)
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
