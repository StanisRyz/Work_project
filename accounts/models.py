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
        #
        # `MAS` is retired: «Мастера производства» has been split into three
        # named productions, and new profiles are given one of the three below.
        # It stays in `choices` because profiles created before the split still
        # store `'mas'` — removing the value would leave those rows holding
        # something the field no longer admits, which is a data problem, not a
        # tidy-up. Retire a role by not offering it, never by deleting it.
        MAS = 'mas', 'Мастер производства'
        # The three productions «Мастера производства» became. Titles of a
        # person — the unit they work in is `Department` («Производство ПиР» and
        # so on), and the two are deliberately worded differently so a reader
        # can tell which of the two systems they are looking at. Like every
        # role added since ОПР, they grant nothing: see the note below.
        MAS_PIR = 'mas_pir', 'Мастер ПиР'
        MAS_MP_RL = 'mas_mp_rl', 'Мастер МП и РЛ'
        MAS_TR = 'mas_tr', 'Мастер ТР'
        # Отдел СМК: owns the quality-management-system corrective actions.
        # A first-class role like the others — never a department check, and
        # it grants nothing outside the SMK module.
        SMK = 'smk', 'СМК'
        # The remaining departments, as first-class roles like every other.
        # Nothing in the project reads any of these five, nor any of the three
        # productions above — that is the design,
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


# Roles a substitution may hand over. Everything but «Администратор»: the
# administrative role is given deliberately, in the profile, and never borrows
# a vacation's end date.
SUBSTITUTABLE_ROLES = tuple(
    (value, label) for value, label in UserProfile.Role.choices
    if value != UserProfile.Role.ADMIN
)


class RoleSubstitution(models.Model):
    """«Замещение»: a role lent to a user for a period, on top of their own.

    A technologist covering a designer's vacation gets КО from `date_from` to
    `date_to` inclusive and keeps being ТО the whole time: rights are only
    ever *added*, and the person being replaced loses nothing. Every
    permission in the project asks `accounts.roles.get_user_roles()`, which is
    the profile role plus the substitutions in force today — so nothing has to
    be undone when the period ends, it simply stops counting.

    Set in Django Admin, like roles themselves. The tasks already assigned to
    the person being replaced do not move: a substitution lends a role, not
    somebody's personal work.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='role_substitutions',
        verbose_name='Сотрудник',
    )
    role = models.CharField('Дополнительная роль', max_length=20, choices=SUBSTITUTABLE_ROLES)
    # Whom the user stands in for. Optional — a role can be lent without
    # naming anybody — and kept only to say so in the header and in the act
    # history; it grants nothing and takes nothing from that person.
    substitutes_for = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='substituted_by',
        verbose_name='Замещает',
        blank=True,
        null=True,
    )
    date_from = models.DateField('С')
    date_to = models.DateField('По (включительно)')
    reason = models.CharField('Основание', max_length=200, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Кто назначил',
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['-date_from', 'pk']
        verbose_name = 'Замещение (дополнительная роль)'
        verbose_name_plural = 'Замещения (дополнительные роли)'
        indexes = [
            models.Index(fields=['user', 'date_from', 'date_to'], name='role_subst_user_dates'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(date_to__gte=models.F('date_from')),
                name='role_substitution_period_is_ordered',
            ),
            # Admin's form offers no «Администратор», and this is what keeps a
            # hand-written row from lending it anyway.
            models.CheckConstraint(
                condition=~models.Q(role='admin'),
                name='role_substitution_never_admin',
            ),
        ]

    def __str__(self):
        return f'{self.user}: {self.get_role_display()} ({self.date_from:%d.%m.%Y}–{self.date_to:%d.%m.%Y})'

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.date_from and self.date_to and self.date_to < self.date_from:
            errors['date_to'] = 'Дата окончания не может быть раньше даты начала.'
        if self.role == UserProfile.Role.ADMIN:
            errors['role'] = 'Роль «Администратор» не передаётся замещением.'
        if self.substitutes_for_id and self.substitutes_for_id == self.user_id:
            errors['substitutes_for'] = 'Сотрудник не может замещать сам себя.'
        if errors:
            raise ValidationError(errors)

    def is_active_on(self, day):
        return self.date_from <= day <= self.date_to
