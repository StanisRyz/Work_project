"""«Утилизация МП»: what waits, who disposes, and that disposal is two POSTs."""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from acts.models import Act, ActDefect
from references.models import ActStatus, DefectType

from .models import DisposalAct, DisposalCommissionMember, DisposalItem
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

    def test_dispose_is_the_act_to_sign_first_and_a_confirmed_post_second(self):
        first = self._defect(znp='111')
        second = self._defect(znp='222')
        self.client.force_login(self.master)

        preview = self.client.post(reverse('disposal:confirm'), {'defect': [first.pk]})
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'Акт о списании брака')
        self.assertContains(preview, 'Форма ДП-СМК 07.04/1')
        self.assertContains(preview, 'Подтвердить утилизацию')
        self.assertContains(preview, '111')
        self.assertNotContains(preview, '222')
        # «Комиссия в составе» starts with the master; one more row on request.
        self.assertEqual(preview.context['member_rows'][0]['user'], str(self.master.pk))
        more = self.client.post(reverse('disposal:confirm'), {
            'defect': [first.pk], 'member': [self.master.pk], 'add_member_row': '1',
        })
        self.assertEqual([row['user'] for row in more.context['member_rows']], [str(self.master.pk), ''])
        self.assertFalse(DisposalAct.objects.exists())

        # Nobody in the commission: refused, the page comes back, nothing written.
        refused = self.client.post(reverse('disposal:confirm'), {
            'defect': [first.pk], 'member': [''], 'confirmed': '1',
        })
        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, 'Выберите хотя бы одного члена комиссии.', status_code=400)
        self.assertFalse(DisposalAct.objects.exists())

        self.otk.userprofile.position = 'Контролёр ОТК'
        self.otk.userprofile.save(update_fields=['position'])
        done = self.client.post(reverse('disposal:confirm'), {
            'defect': [first.pk], 'member': [self.master.pk, self.otk.pk, self.otk.pk, ''],
            'confirmed': '1',
        })
        disposal = DisposalAct.objects.get()
        self.assertRedirects(done, reverse('disposal:print', args=[disposal.pk]))
        self.assertEqual(disposal.disposed_by, self.master)
        item = disposal.items.get()
        self.assertEqual((item.defect, item.znp_number, item.quantity), (first, '111', 4))
        self.assertEqual(item.act_date, timezone.localtime(first.act.created_at).date())
        self.assertEqual(
            [(m.user, m.position) for m in disposal.commission.all()],
            [(self.master, 'Мастер МП и РЛ'), (self.otk, 'Контролёр ОТК')],
        )
        self.assertEqual(list(pending_defects()), [second])

        listing = self.client.get(reverse('disposal:list'), {'tab': 'done'})
        self.assertContains(listing, reverse('disposal:print', args=[disposal.pk]))
        self.client.force_login(self.otk)
        printed = self.client.get(reverse('disposal:print', args=[disposal.pk]))
        self.assertContains(printed, disposal.label.split(' №')[0])
        self.assertContains(printed, f'№ {disposal.pk} от')
        self.assertContains(printed, 'Контролёр ОТК')
        self.assertContains(printed, '(должность, ФИО подпись)')
        self.assertNotContains(printed, 'Подтвердить утилизацию')

    def test_a_commission_member_must_be_an_active_employee(self):
        defect = self._defect()
        self.otk.is_active = False
        self.otk.save(update_fields=['is_active'])
        with self.assertRaisesMessage(DisposalError, 'Выберите хотя бы одного члена комиссии.'):
            dispose_defects([defect.pk], actor=self.master, member_ids=[self.otk.pk])
        self.assertFalse(DisposalCommissionMember.objects.exists())

    def test_a_product_is_disposed_of_once_and_a_stale_selection_writes_nothing(self):
        first = self._defect(znp='111')
        second = self._defect(znp='222')
        dispose_defects([first.pk], actor=self.master, member_ids=[self.master.pk])
        with self.assertRaises(DisposalError):
            dispose_defects([first.pk, second.pk], actor=self.master, member_ids=[self.master.pk])
        with self.assertRaises(DisposalError):
            dispose_defects([], actor=self.master, member_ids=[self.master.pk])
        with self.assertRaises(DisposalError):
            dispose_defects([second.pk], actor=self.otk, member_ids=[self.otk.pk])
        self.assertEqual(DisposalItem.objects.count(), 1)
        self.assertEqual(DisposalAct.objects.count(), 1)

    def test_get_on_confirm_changes_nothing(self):
        self._defect()
        self.client.force_login(self.master)
        self.assertRedirects(
            self.client.get(reverse('disposal:confirm')), f"{reverse('disposal:list')}?tab=pending",
        )
        self.assertFalse(DisposalAct.objects.exists())
