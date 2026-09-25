"""«Замещения»: a role lent for a period, on top of the user's own.

Every permission asks `accounts.roles.get_user_roles()`, so these tests check
the helper itself, one representative rule per module that reads roles, the
routing of act-stage tasks and notifications, and the two places a
substitution is shown — the profile menu and the act history.
"""

from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, RoleSubstitution, UserProfile
from accounts.roles import get_user_roles, has_role, role_holders_q
from acts.models import Act, ActHistoryEvent
from acts.permissions import (
    can_apply_ko_decision,
    can_contribute_to_act,
    get_visible_acts_queryset,
    is_ko,
    is_to,
)
from acts.services import send_to_ko
from calculator.permissions import can_manage_workup
from references.models import ActStatus
from tasks.models import Task
from tasks.services import active_users_for_role


def _user(username, role, *, first_name='', last_name=''):
    user = User.objects.create_user(
        username, password='pw-12345', first_name=first_name, last_name=last_name
    )
    profile = user.userprofile
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


def _fresh(user):
    """A new object: the lent roles are cached on the instance for a request."""
    return User.objects.get(pk=user.pk)


class SubstitutionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())
        cls.today = timezone.localdate()
        cls.technologist = _user(
            'subst_to', UserProfile.Role.TO, first_name='Пётр', last_name='Петров'
        )
        cls.designer = _user(
            'subst_ko', UserProfile.Role.KO, first_name='Иван', last_name='Иванов'
        )
        cls.otk = _user('subst_otk', UserProfile.Role.OTK)
        cls.admin = _user('subst_admin', UserProfile.Role.ADMIN)

    def lend(self, user, role, *, start=0, end=5, substitutes_for=None):
        return RoleSubstitution.objects.create(
            user=user,
            role=role,
            substitutes_for=substitutes_for,
            date_from=self.today + timedelta(days=start),
            date_to=self.today + timedelta(days=end),
        )

    def act(self, status_code, *, author=None):
        return Act.objects.create(
            created_by=author or self.otk,
            nomenclature='Катушка',
            status=ActStatus.objects.get(code=status_code),
        )


class RoleHelperTests(SubstitutionTestCase):
    def test_a_lent_role_is_added_to_the_own_one(self):
        self.lend(self.technologist, UserProfile.Role.KO)

        user = _fresh(self.technologist)
        self.assertEqual(get_user_roles(user), {UserProfile.Role.TO, UserProfile.Role.KO})
        self.assertTrue(is_to(user))
        self.assertTrue(is_ko(user))

    def test_only_the_period_counts_both_ends_included(self):
        self.lend(self.technologist, UserProfile.Role.KO, start=1, end=3)
        self.assertFalse(has_role(_fresh(self.technologist), UserProfile.Role.KO))

        RoleSubstitution.objects.all().delete()
        self.lend(self.technologist, UserProfile.Role.KO, start=-3, end=-1)
        self.assertFalse(has_role(_fresh(self.technologist), UserProfile.Role.KO))

        RoleSubstitution.objects.all().delete()
        self.lend(self.technologist, UserProfile.Role.KO, start=0, end=0)
        self.assertTrue(has_role(_fresh(self.technologist), UserProfile.Role.KO))

    def test_an_inactive_profile_holds_no_role_lent_or_own(self):
        self.lend(self.technologist, UserProfile.Role.KO)
        profile = self.technologist.userprofile
        profile.is_active = False
        profile.save(update_fields=['is_active'])

        self.assertEqual(get_user_roles(_fresh(self.technologist)), frozenset())

    def test_the_person_replaced_keeps_their_rights(self):
        self.lend(self.technologist, UserProfile.Role.KO, substitutes_for=self.designer)

        self.assertTrue(is_ko(_fresh(self.designer)))

    def test_administrator_is_never_lent(self):
        substitution = RoleSubstitution(
            user=self.technologist, role=UserProfile.Role.ADMIN,
            date_from=self.today, date_to=self.today,
        )
        with self.assertRaises(ValidationError):
            substitution.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            substitution.save()

    def test_the_period_must_be_ordered(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.lend(self.technologist, UserProfile.Role.KO, start=2, end=1)

    def test_the_queryset_answer_agrees_with_the_python_one(self):
        self.lend(self.technologist, UserProfile.Role.KO)
        self.lend(self.otk, UserProfile.Role.KO, start=3, end=4)

        holders = set(
            User.objects.filter(role_holders_q(UserProfile.Role.KO)).distinct()
        )

        self.assertEqual(holders, {self.designer, self.technologist})


class ActRightsTests(SubstitutionTestCase):
    def test_a_substitute_works_the_lent_queue_and_keeps_their_own(self):
        ko_act = self.act('KO_REVIEW')
        to_act = self.act('TO_ANALYSIS')
        self.lend(self.technologist, UserProfile.Role.KO)

        user = _fresh(self.technologist)
        self.assertTrue(can_apply_ko_decision(ko_act, user))
        self.assertTrue(can_contribute_to_act(ko_act, user))
        self.assertTrue(can_contribute_to_act(to_act, user))
        self.assertEqual(
            set(get_visible_acts_queryset(user)), {ko_act, to_act},
            '«Мои» is the union of both queues',
        )

    def test_the_right_ends_with_the_period(self):
        ko_act = self.act('KO_REVIEW')
        self.lend(self.technologist, UserProfile.Role.KO, start=-5, end=-1)

        user = _fresh(self.technologist)
        self.assertFalse(can_apply_ko_decision(ko_act, user))
        self.assertNotIn(ko_act, get_visible_acts_queryset(user))

    def test_the_decision_page_offers_the_ko_form(self):
        ko_act = self.act('KO_REVIEW')
        self.lend(self.technologist, UserProfile.Role.KO)
        self.client.force_login(self.technologist)

        page = self.client.get(reverse('acts:detail', args=[ko_act.pk]))

        self.assertTrue(page.context['available_actions']['ko_decision'])

    def test_other_modules_read_the_lent_role_too(self):
        self.assertFalse(can_manage_workup(_fresh(self.technologist)))
        self.lend(self.technologist, UserProfile.Role.PDO)

        self.assertTrue(can_manage_workup(_fresh(self.technologist)))


class RoutingAndHistoryTests(SubstitutionTestCase):
    def test_new_stage_tasks_and_recipients_include_the_substitute(self):
        self.lend(self.technologist, UserProfile.Role.KO)

        self.assertIn(self.technologist, active_users_for_role(UserProfile.Role.KO))

        act = self.act('CREATED_OTK')
        send_to_ko(act, self.otk)
        task = Task.objects.get(act=act, source_type=Task.SourceType.ACT_WORKFLOW)
        self.assertEqual(
            set(task.assignees.values_list('user_id', flat=True)),
            {self.designer.pk, self.technologist.pk},
        )

    def test_saving_a_substitution_in_admin_joins_the_open_stage_tasks(self):
        act = self.act('CREATED_OTK')
        send_to_ko(act, self.otk)
        task = Task.objects.get(act=act, source_type=Task.SourceType.ACT_WORKFLOW)
        self.assertFalse(task.assignees.filter(user=self.technologist).exists())
        superuser = User.objects.create_superuser('subst_root', password='pw-12345')
        self.client.force_login(superuser)

        response = self.client.post(
            reverse('admin:accounts_rolesubstitution_add'),
            {
                'user': self.technologist.pk,
                'role': UserProfile.Role.KO,
                'substitutes_for': self.designer.pk,
                'date_from': self.today.isoformat(),
                'date_to': (self.today + timedelta(days=7)).isoformat(),
                'reason': 'Отпуск',
            },
        )

        self.assertEqual(response.status_code, 302)
        substitution = RoleSubstitution.objects.get()
        self.assertEqual(substitution.created_by, superuser)
        self.assertTrue(task.assignees.filter(user=self.technologist).exists())
        self.assertTrue(task.assignees.filter(user=self.designer).exists())

    def test_admin_does_not_offer_the_administrator_role(self):
        superuser = User.objects.create_superuser('subst_root2', password='pw-12345')
        self.client.force_login(superuser)

        page = self.client.get(reverse('admin:accounts_rolesubstitution_add'))

        self.assertNotContains(page, 'value="admin"')

    def test_the_history_says_on_whose_behalf_a_step_was_taken(self):
        # A designer covering ОТК sends their own act to КО.
        self.lend(self.designer, UserProfile.Role.OTK, substitutes_for=self.otk)
        designer = _fresh(self.designer)
        act = self.act('CREATED_OTK', author=designer)

        send_to_ko(act, designer)

        event = ActHistoryEvent.objects.get(act=act, event_type='SENT_TO_KO')
        self.assertEqual(event.substitution_note, f'замещает {self.otk.username}')
        self.client.force_login(self.designer)
        page = self.client.get(reverse('acts:detail', args=[act.pk]), {'tab': 'history'})
        self.assertContains(page, f'(замещает {self.otk.username})')

    def test_acting_in_ones_own_role_leaves_no_note(self):
        act = self.act('CREATED_OTK')

        send_to_ko(act, self.otk)

        event = ActHistoryEvent.objects.get(act=act, event_type='SENT_TO_KO')
        self.assertEqual(event.substitution_note, '')

    def test_the_profile_menu_names_the_lent_role_and_its_end(self):
        substitution = self.lend(self.technologist, UserProfile.Role.KO)
        self.client.force_login(self.technologist)

        page = self.client.get(reverse('acts:list'))

        self.assertContains(page, f'ТО, замещает КО до {substitution.date_to:%d.%m}')
