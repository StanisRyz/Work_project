"""Highest-risk behaviour of the workshop-aware defect architecture.

`ActDefect` is the canonical source of defect data: an act no longer summarises
its first defect, every defect keeps only what its workshop collects, and the
registry searches all of them.
"""

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from acts.models import Act, ActDefect
from references.models import ActStatus, DefectType, Operation


class WorkshopAwareDefectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ActStatus.objects.get_or_create(code='CREATED_OTK', defaults={'name': 'Создан ОТК'})
        cls.operation = Operation.objects.create(
            code='OPERATIONAL_CONTROL', name='Операционный контроль'
        )
        cls.defect_type = DefectType.objects.create(
            code='SIZE_NONCONFORMITY', name='Несоответствие размеров'
        )
        cls.otk_user = User.objects.create_user(username='ws_otk', password='demo12345')
        cls.otk_user.userprofile.role = UserProfile.Role.OTK
        cls.otk_user.userprofile.save(update_fields=['role'])

    def setUp(self):
        self.client.force_login(self.otk_user)

    def _mixed_act_payload(self):
        """One act whose first defect is ПиР and whose second one is МП."""
        return {
            'number_suffix': '40',
            'customer': 'Заказчик',
            'order_number': '400-1',
            'nomenclature': 'Катушка',
            'kd_designation': 'КД-400',
            'defects-TOTAL_FORMS': '2',
            'defects-INITIAL_FORMS': '0',
            'defects-MIN_NUM_FORMS': '1',
            'defects-MAX_NUM_FORMS': '1000',
            'defects-0-workshop': ActDefect.Workshop.PIR_SHOP,
            'defects-0-defect_type': self.defect_type.pk,
            'defects-0-znp_number': '500-1',
            'defects-0-checked_quantity': '20',
            'defects-0-nonconforming_quantity': '2',
            'defects-0-detected_at': timezone.localdate().isoformat(),
            # Values ПиР does not collect, as a switched form would still carry.
            'defects-0-party_number': '900-900',
            'defects-0-operation': self.operation.pk,
            'defects-0-mp_type': 'OL',
            'defects-0-description': 'Не должно сохраниться',
            'defects-1-workshop': ActDefect.Workshop.MP_SHOP,
            'defects-1-defect_type': self.defect_type.pk,
            'defects-1-znp_number': '500-2',
            'defects-1-party_number': '600-600',
            'defects-1-operation': self.operation.pk,
            'defects-1-mp_type': 'PL',
            'defects-1-description': 'Описание дефекта МП',
            'defects-1-checked_quantity': '30',
            'defects-1-nonconforming_quantity': '3',
            'defects-1-detected_at': timezone.localdate().isoformat(),
        }

    def test_a_mixed_act_keeps_each_defect_independent_and_summarises_none(self):
        response = self.client.post(reverse('acts:create'), self._mixed_act_payload())

        self.assertEqual(response.status_code, 302, response.context)
        act = Act.objects.get(order_number='400-1')
        pir_defect, mp_defect = act.defects.order_by('created_at', 'pk')

        self.assertEqual(pir_defect.workshop, ActDefect.Workshop.PIR_SHOP)
        self.assertEqual(pir_defect.znp_number, '500-1')
        self.assertEqual(pir_defect.party_number, '')
        self.assertEqual(pir_defect.mp_type, '')
        self.assertEqual(pir_defect.description, '')
        self.assertIsNone(pir_defect.operation)

        self.assertEqual(mp_defect.workshop, ActDefect.Workshop.MP_SHOP)
        self.assertEqual(mp_defect.znp_number, '500-2')
        self.assertEqual(mp_defect.party_number, '600-600')
        self.assertEqual(mp_defect.operation, self.operation)
        self.assertEqual(mp_defect.mp_type, 'PL')
        self.assertEqual(mp_defect.description, 'Описание дефекта МП')

        # The first defect no longer decides anything about the act itself.
        self.assertEqual(act.znp_number, '')
        self.assertEqual(act.party_number, '')
        self.assertEqual(act.description, '')
        self.assertIsNone(act.operation)
        self.assertIsNone(act.defect_type)

    def test_the_database_refuses_mp_only_data_on_a_pir_defect(self):
        self.client.post(reverse('acts:create'), self._mixed_act_payload())
        act = Act.objects.get(order_number='400-1')

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ActDefect.objects.create(
                    act=act,
                    workshop=ActDefect.Workshop.PIR_SHOP,
                    defect_type=self.defect_type,
                    znp_number='500-3',
                    party_number='700-700',
                    detected_at=timezone.localdate(),
                )

    def test_the_registry_finds_an_act_by_data_of_a_non_first_defect(self):
        self.client.post(reverse('acts:create'), self._mixed_act_payload())
        act = Act.objects.get(order_number='400-1')

        by_party = self.client.get(reverse('acts:list'), {'search': '600-600'})
        by_second_znp = self.client.get(reverse('acts:list'), {'search': '500-2'})
        by_first_znp = self.client.get(reverse('acts:list'), {'search': '500-1'})

        for response in (by_party, by_second_znp, by_first_znp):
            self.assertContains(response, act.number)
            # The join must not duplicate the act.
            self.assertEqual(len(response.context['acts']), 1)
