"""КО по цехам: КО МП, КО ЦПиР и (пока без цеха) КО ТР.

Each workshop КО decides the defects of its own цех and nothing else; an act
with defects of several цехов waits in `KO_REVIEW` until every defect has a
decision of the current round, then moves on to ТО in the same transaction as
the last decision. The retired general КО, руководитель and администратор still
decide every defect at once.
"""

from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.admin import UserProfileAdminForm
from accounts.models import UserProfile
from acts.models import Act, ActDefect, ActHistoryEvent
from acts.permissions import (
    can_apply_ko_decision,
    can_decide_defect,
    decidable_defects,
    get_visible_acts_queryset,
)
from acts.services import ActWorkflowError, apply_ko_decision, return_to_ko, send_to_ko
from acts import workshops
from references.models import ActStatus, DefectType
from tasks.models import Task


def _user(username, role):
    user = User.objects.create_user(username, password='pw-12345')
    profile = user.userprofile
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


DECIDE = Act.KoDecision.ALLOW_NO_DEVIATION_REWORK


class WorkshopKoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())
        cls.otk = _user('wko_otk', UserProfile.Role.OTK)
        cls.ko_mp = _user('wko_mp', UserProfile.Role.KO_MP)
        cls.ko_pir = _user('wko_pir', UserProfile.Role.KO_PIR)
        cls.ko_tr = _user('wko_tr', UserProfile.Role.KO_TR)
        cls.ko_general = _user('wko_general', UserProfile.Role.KO)
        cls.to = _user('wko_to', UserProfile.Role.TO)
        cls.defect_type = DefectType.objects.get(code='OTHER')

    def make_act(self, *workshop_codes):
        act = Act.objects.create(
            created_by=self.otk,
            nomenclature='Катушка',
            status=ActStatus.objects.get(code='CREATED_OTK'),
            due_date=timezone.localdate() + timedelta(days=3),
        )
        for code in workshop_codes:
            ActDefect.objects.create(
                act=act, workshop=code, defect_type=self.defect_type,
                detected_at=timezone.localdate(),
            )
        return send_to_ko(act, self.otk)

    def decide(self, act, user):
        decisions = [
            (defect, DECIDE, 'Решение', {}) for defect in decidable_defects(act, user)
        ]
        return apply_ko_decision(act, user, decisions)

    def routing_assignees(self, act):
        task = Task.objects.get(
            act=act, source_type=Task.SourceType.ACT_WORKFLOW, status__code='IN_PROGRESS'
        )
        return set(task.assignees.values_list('user_id', flat=True))

    # ------------------------------------------------------------ rights

    def test_each_workshop_ko_decides_only_its_own_defects(self):
        act = self.make_act(workshops.MP_SHOP, workshops.PIR_SHOP)
        mp, pir = act.defects.order_by('pk')

        self.assertTrue(can_decide_defect(mp, self.ko_mp))
        self.assertFalse(can_decide_defect(pir, self.ko_mp))
        self.assertEqual(decidable_defects(act, self.ko_pir), [pir])
        self.assertEqual(decidable_defects(act, self.ko_general), [mp, pir])
        self.assertFalse(can_apply_ko_decision(act, self.ko_tr), 'no ТР defects here')

    def test_the_queue_holds_only_acts_with_ones_own_undecided_defects(self):
        mp_only = self.make_act(workshops.MP_SHOP)
        pir_only = self.make_act(workshops.PIR_SHOP)

        self.assertEqual(set(get_visible_acts_queryset(self.ko_mp)), {mp_only})
        self.assertEqual(set(get_visible_acts_queryset(self.ko_pir)), {pir_only})
        self.assertEqual(set(get_visible_acts_queryset(self.ko_general)), {mp_only, pir_only})

    def test_routing_and_notifications_go_to_the_workshops_ko(self):
        act = self.make_act(workshops.MP_SHOP)

        self.assertEqual(
            self.routing_assignees(act), {self.ko_mp.pk, self.ko_general.pk},
            'КО ЦПиР and КО ТР have nothing to decide here',
        )
        self.assertTrue(act.history_events.filter(event_type='SENT_TO_KO').exists())
        recipients = set(
            self.ko_mp.notifications.filter(related_act=act).values_list('recipient_id', flat=True)
        )
        self.assertEqual(recipients, {self.ko_mp.pk})
        self.assertFalse(self.ko_pir.notifications.filter(related_act=act).exists())

    # ------------------------------------------------------------ the mixed act

    def test_a_mixed_act_moves_to_to_only_after_both_workshops_decided(self):
        act = self.make_act(workshops.MP_SHOP, workshops.PIR_SHOP)

        act = self.decide(act, self.ko_mp)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW', 'ЦПиР has not decided yet')
        self.assertNotIn(act, get_visible_acts_queryset(self.ko_mp), 'МП share is done')
        self.assertIn(act, get_visible_acts_queryset(self.ko_pir))
        self.assertEqual(
            self.routing_assignees(act), {self.ko_pir.pk, self.ko_general.pk},
            'the stage entry waits only for those still owing a decision',
        )

        act = self.decide(act, self.ko_pir)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertEqual(
            act.history_events.filter(event_type=ActHistoryEvent.EventType.SENT_TO_TO).count(), 1
        )

    def test_a_workshop_ko_cannot_decide_another_workshops_defect(self):
        act = self.make_act(workshops.MP_SHOP, workshops.PIR_SHOP)
        mp, pir = act.defects.order_by('pk')

        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(act, self.ko_mp, [(pir, DECIDE, '', {})])
        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(act, self.ko_mp, [(mp, DECIDE, '', {}), (pir, DECIDE, '', {})])

    def test_a_return_to_ko_starts_a_new_round(self):
        act = self.make_act(workshops.MP_SHOP, workshops.PIR_SHOP)
        self.decide(act, self.ko_mp)
        act = self.decide(act, self.ko_pir)
        act = return_to_ko(act, self.to, 'Уточнить решение')
        act.refresh_from_db()

        self.assertEqual(act.status.code, 'KO_REVIEW')
        act = self.decide(act, self.ko_mp)
        act.refresh_from_db()
        self.assertEqual(
            act.status.code, 'KO_REVIEW',
            'the ЦПиР decision of the previous round does not count',
        )

    def test_the_general_ko_still_decides_everything_at_once(self):
        act = self.make_act(workshops.MP_SHOP, workshops.PIR_SHOP)

        act = self.decide(act, self.ko_general)
        act.refresh_from_db()

        self.assertEqual(act.status.code, 'TO_ANALYSIS')

    # ------------------------------------------------------------ the page

    def test_the_page_offers_only_ones_own_defects_and_says_who_is_awaited(self):
        act = self.make_act(workshops.MP_SHOP, workshops.PIR_SHOP)
        mp, pir = act.defects.order_by('pk')
        self.client.force_login(self.ko_mp)

        page = self.client.get(reverse('acts:detail', args=[act.pk]))

        forms = [row['ko_form'] for row in page.context['defect_decision_rows']]
        self.assertIsNotNone(forms[0])
        self.assertIsNone(forms[1])
        self.assertContains(page, 'Ожидает решения КО ЦПиР')
        self.assertContains(page, 'Сохранить решения КО')

        response = self.client.post(
            reverse('acts:ko_decision', args=[act.pk]),
            {
                'form-TOTAL_FORMS': '1',
                'form-INITIAL_FORMS': '1',
                'form-MIN_NUM_FORMS': '0',
                'form-MAX_NUM_FORMS': '1000',
                'form-0-id': str(mp.pk),
                'form-0-ko_decision': DECIDE,
                'form-0-ko_comment': 'МП решено',
            },
            follow=True,
        )

        self.assertContains(response, 'Акт перейдёт в ТО после решения: КО ЦПиР')
        mp.refresh_from_db()
        self.assertEqual(mp.ko_decision, DECIDE)
        pir.refresh_from_db()
        self.assertEqual(pir.ko_decision, '')

    def test_the_last_decision_is_offered_as_the_transfer_to_to(self):
        act = self.make_act(workshops.MP_SHOP, workshops.PIR_SHOP)
        self.decide(act, self.ko_mp)
        self.client.force_login(self.ko_pir)

        page = self.client.get(reverse('acts:detail', args=[act.pk]))

        self.assertContains(page, 'Передать в ТО')
        self.assertNotContains(page, 'Сохранить решения КО')


class RetiredKoRoleTests(TestCase):
    def test_admin_no_longer_offers_the_general_ko_for_a_new_assignment(self):
        user = User.objects.create_user('retired_new', password='pw-12345')
        form = UserProfileAdminForm(instance=user.userprofile)

        values = [value for value, _label in form.fields['role'].choices]
        self.assertNotIn(UserProfile.Role.KO, values)
        self.assertIn(UserProfile.Role.KO_MP, values)
        self.assertIn(UserProfile.Role.KO_TR, values)
        self.assertIn(UserProfile.Role.KO_PIR, values)

    def test_a_profile_that_holds_it_keeps_it_until_changed(self):
        user = User.objects.create_user('retired_old', password='pw-12345')
        profile = user.userprofile
        profile.role = UserProfile.Role.KO
        profile.save(update_fields=['role'])

        form = UserProfileAdminForm(instance=profile)

        self.assertIn(UserProfile.Role.KO, [value for value, _ in form.fields['role'].choices])

    def test_the_tr_workshop_is_a_template_not_offered_in_the_defect_form(self):
        self.assertNotIn(workshops.TR_SHOP, workshops.WORKSHOP_PROFILES)
        self.assertIn(workshops.TR_SHOP, workshops.PLANNED_WORKSHOP_PROFILES)
        self.assertNotIn(workshops.TR_SHOP, ActDefect.Workshop.values)
