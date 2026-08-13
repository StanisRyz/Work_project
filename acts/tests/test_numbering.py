"""Highest-risk behaviour of the manual act number and of the ПиР workshop."""

from datetime import date
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from acts.models import Act, ActDefect
from references.models import ActStatus, DefectType, Operation


class ActCreationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        cls.operation = Operation.objects.create(code='OPERATIONAL_CONTROL', name='Операционный контроль')
        cls.defect_type = DefectType.objects.create(
            code='SIZE_NONCONFORMITY', name='Несоответствие размеров'
        )
        cls.forbidden_defect_type = DefectType.objects.create(
            code='HIGH_ROUGHNESS', name='Повышенная шероховатость'
        )
        cls.otk_user = User.objects.create_user(username='num_otk', password='demo12345')
        cls.otk_user.userprofile.role = UserProfile.Role.OTK
        cls.otk_user.userprofile.save(update_fields=['role'])

    def setUp(self):
        self.client.force_login(self.otk_user)

    def _payload(self, suffix, order_number, **defect_overrides):
        defect = {
            'defects-0-workshop': ActDefect.Workshop.MP_SHOP,
            'defects-0-defect_type': self.defect_type.pk,
            'defects-0-operation': self.operation.pk,
            'defects-0-mp_type': 'OL',
            'defects-0-znp_number': '200-1',
            'defects-0-party_number': '100-100',
            'defects-0-checked_quantity': '100',
            'defects-0-nonconforming_quantity': '4',
            'defects-0-description': 'Описание дефекта',
            'defects-0-detected_at': timezone.localdate().isoformat(),
        }
        defect.update({f'defects-0-{name}': value for name, value in defect_overrides.items()})
        return {
            'number_suffix': suffix,
            'customer': 'Заказчик',
            'order_number': order_number,
            'nomenclature': 'Катушка',
            'kd_designation': 'КД-100',
            'defects-TOTAL_FORMS': '1',
            'defects-INITIAL_FORMS': '0',
            'defects-MIN_NUM_FORMS': '1',
            'defects-MAX_NUM_FORMS': '1000',
            **defect,
        }

    def test_the_server_formats_the_number_and_allows_two_acts_to_share_it(self):
        with mock.patch('acts.models.timezone.localdate', return_value=date(2026, 5, 20)):
            for order_number in ('100-1', '100-2'):
                response = self.client.post(reverse('acts:create'), self._payload('35/1', order_number))
                self.assertEqual(response.status_code, 302, response.context)

        first, second = Act.objects.order_by('pk')
        # The year and the zero padding come from the server, not the browser.
        self.assertEqual(first.number, 'АОК-2026-035/1')
        self.assertEqual(second.number, first.number)
        self.assertNotEqual(first.pk, second.pk)

    def test_a_pir_act_is_created_without_the_mp_only_fields(self):
        response = self.client.post(
            reverse('acts:create'),
            self._payload(
                '34',
                '300-1',
                workshop=ActDefect.Workshop.PIR_SHOP,
                # Stale МП values a switched form may still carry must not be saved.
                party_number='100-100',
                mp_type='OL',
                description='Описание дефекта',
            ),
        )

        self.assertEqual(response.status_code, 302, response.context)
        act = Act.objects.get(order_number='300-1')
        self.assertEqual(act.number, f'АОК-{timezone.localdate().year}-00034')
        defect = act.defects.get()
        self.assertEqual(defect.workshop, ActDefect.Workshop.PIR_SHOP)
        self.assertEqual(defect.znp_number, '200-1')
        self.assertEqual(defect.party_number, '')
        self.assertEqual(defect.mp_type, '')
        self.assertEqual(defect.description, '')
        self.assertIsNone(defect.operation)
        self.assertIsNone(act.operation)
        self.assertEqual(act.party_number, '')
        self.assertEqual(act.description, '')

    def test_a_defect_type_outside_the_pir_set_is_refused_by_the_backend(self):
        response = self.client.post(
            reverse('acts:create'),
            self._payload(
                '36',
                '300-2',
                workshop=ActDefect.Workshop.PIR_SHOP,
                defect_type=self.forbidden_defect_type.pk,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Act.objects.filter(order_number='300-2').exists())
        self.assertContains(response, 'недоступен для выбранного цеха')
