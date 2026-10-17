"""«Утилизация МП»: what waits, who disposes, and that disposal is two POSTs."""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from acts.models import Act, ActDefect
from references.models import ActStatus, DefectType

from .models import DisposalAct, DisposalItem
from .permissions import can_dispose
from .selectors import pending_defects
from .services import DisposalError, dispose_defects


class DisposalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_to = ActStatus.objects.get_or_create(code='TO_ANALYSIS', defaults={'name': 'Анализ ТО'})[0]
        cls.status_ko = ActStatus.objects.get_or_create(code='KO_REVIEW', defaults={'name': 'КО'})[0]
        cls.defect_type = DefectType.objects.create(code='SCRAP', name='Обрыв')
        cls.master = cls._user('mas_mp', UserProfile.Role.MAS_MP_RL)
        cls.pir_master = cls._user('mas_pir', UserProfile.Role.MAS_PIR)
        cls.otk = cls._user('otk', UserProfile.Role.OTK)
        cls.admin = cls._user('admin', UserProfile.Role.ADMIN)

    @classmethod
    def _user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.save()
        return user

    def _defect(self, *, status=None, workshop=ActDefect.Workshop.MP_SHOP,
                decision=Act.KoDecision.PROHIBIT_USE, znp='100', act_round=1, defect_round=1):
        act = Act.objects.create(
            created_by=self.otk, nomenclature='МП 120/200-40', order_number='12345',
            number='АОК-2026-00001', status=status or self.status_to, ko_round=act_round,
        )
        return ActDefect.objects.create(
            act=act, defect_type=self.defect_type, workshop=workshop, znp_number=znp,
            party_number='7' if workshop == ActDefect.Workshop.MP_SHOP else '', detected_at='2026-10-01', checked_quantity=10,
            nonconforming_quantity=4, ko_decision=decision, ko_round=defect_round,
            ko_decision_by=self.otk, ko_decision_at=timezone.now(),
        )

    def test_waiting_is_every_mp_defect_the_current_ko_decision_prohibited(self):
        waiting = self._defect()
        self._defect(workshop=ActDefect.Workshop.PIR_SHOP, znp='200')
        self._defect(decision=Act.KoDecision.ALLOW_NO_REWORK, znp='300')
        self._defect(status=self.status_ko, znp='400')               # back in front of КО
        self._defect(act_round=2, defect_round=1, znp='500')        # an earlier round's decision
        self.assertEqual(list(pending_defects()), [waiting])

    def test_who_may_dispose(self):
        self.assertTrue(can_dispose(self.master))
        self.assertTrue(can_dispose(self.admin))
        self.assertFalse(can_dispose(self.pir_master))
        self.assertFalse(can_dispose(self.otk))

    def test_list_is_open_to_everyone_the_controls_are_not(self):
        defect = self._defect()
        self.client.force_login(self.otk)
        page = self.client.get(reverse('disposal:list'))
        self.assertContains(page, defect.znp_number)
        self.assertNotContains(page, 'data-disposal-item')
        self.assertEqual(self.client.post(reverse('disposal:confirm'), {'defect': [defect.pk]}).status_code, 403)
        self.client.force_login(self.master)
        self.assertContains(self.client.get(reverse('disposal:list')), 'data-disposal-item')

    def test_dispose_is_a_print_form_first_and_a_confirmed_post_second(self):
        first = self._defect(znp='111')
        second = self._defect(znp='222')
        self.client.force_login(self.master)

        preview = self.client.post(reverse('disposal:confirm'), {'defect': [first.pk]})
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'Подтвердить утилизацию')
        self.assertContains(preview, '111')
        self.assertNotContains(preview, '222')
        self.assertFalse(DisposalAct.objects.exists())

        done = self.client.post(reverse('disposal:confirm'), {'defect': [first.pk], 'confirmed': '1'})
        self.assertRedirects(done, f"{reverse('disposal:list')}?tab=done")
        disposal = DisposalAct.objects.get()
        self.assertEqual(disposal.disposed_by, self.master)
        item = disposal.items.get()
        self.assertEqual((item.defect, item.znp_number, item.quantity), (first, '111', 4))
        self.assertEqual(list(pending_defects()), [second])

        listing = self.client.get(reverse('disposal:list'), {'tab': 'done'})
        self.assertContains(listing, reverse('disposal:print', args=[disposal.pk]))
        self.client.force_login(self.otk)
        printed = self.client.get(reverse('disposal:print', args=[disposal.pk]))
        self.assertContains(printed, disposal.label)
        self.assertNotContains(printed, 'Подтвердить утилизацию')

    def test_a_product_is_disposed_of_once_and_a_stale_selection_writes_nothing(self):
        first = self._defect(znp='111')
        second = self._defect(znp='222')
        dispose_defects([first.pk], actor=self.master)
        with self.assertRaises(DisposalError):
            dispose_defects([first.pk, second.pk], actor=self.master)
        with self.assertRaises(DisposalError):
            dispose_defects([], actor=self.master)
        with self.assertRaises(DisposalError):
            dispose_defects([second.pk], actor=self.otk)
        self.assertEqual(DisposalItem.objects.count(), 1)
        self.assertEqual(DisposalAct.objects.count(), 1)

    def test_get_on_confirm_changes_nothing(self):
        self._defect()
        self.client.force_login(self.master)
        self.assertRedirects(
            self.client.get(reverse('disposal:confirm')), f"{reverse('disposal:list')}?tab=pending",
        )
        self.assertFalse(DisposalAct.objects.exists())
