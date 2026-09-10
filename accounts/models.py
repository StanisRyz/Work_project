from django.contrib.auth.models import User
from django.db import models


class Department(models.Model):
    """Where a person works. What they may do is `UserProfile.Role`."""

    # What a person reads, and the only part meant to be edited: renaming a
    # unit in Admin is safe and changes nothing anywhere.
    name = models.CharField('Название', max_length=120)
    # The unit's stable identity, which is why it exists beside the name.
    # Migrations create reference departments with `get_or_create(code=...)`,
    # so a locally renamed unit is recognised rather than duplicated; the
    # database-transfer tool lists units by code for the same reason. One code
    # is load-bearing in application logic — `tasks.services.get_pdo_recipients()`
    # finds ПДО by `code='PDO'` — so a code may be chosen once and must not be
    # changed afterwards, while the name may be changed freely.
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
        """What an employee may do — the project's only source of rights.

        Not the same thing as `department` below, which is where they work.
        The two answer different questions and must keep doing so: every
        permission in the project reads the role, and none reads the
        department. `acts.permissions` is where «what role is this user» is
        answered for the whole project — a module that needs a role check
        imports a helper from there instead of comparing values itself, so
        there is exactly one implementation of every answer.

        The asymmetry is deliberate and worth knowing before changing either.
        A role is a code constant: adding or removing one is a code change
        plus a `choices` migration. A department is a database row an
        administrator edits and renames freely — which is precisely why no
        rule is ever keyed on it. Permissions must not depend on the wording
        somebody typed into Admin.

        One documented exception exists, and it grants nobody anything:
        `tasks.services.get_pdo_recipients()` selects *who is notified* about
        a rejection task by `department__code='PDO'`, because planning a
        replacement product is what that department does whatever roles its
        members hold. Deciding who is told is not deciding who may act.
        """

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
        # Nothing in the project reads any of these five — that is the design,
        # not an oversight, and a reader who greps for them and finds nothing
        # has found the truth rather than a gap.
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
    # Where the employee works — the counterpart of `Role` above, and nothing
    # to do with what they may do. It addresses work (`Task.department`, the
    # мероприятия of an act, a protocol or an СМК record), narrows the
    # исполнитель selectors in the editing forms, and sorts registries.
    # `SET_NULL` rather than `PROTECT`: dissolving a unit must not take the
    # people who were in it with it, and a profile without a department is a
    # person whose unit has not been decided yet, never an error.
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
