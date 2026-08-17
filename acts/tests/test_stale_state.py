from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import (
    Act,
    ActComment,
    ActCorrectiveAction,
    ActCorrectiveActionAssignee,
    ActDefect,
    ActHistoryEvent,
    ActRootAnalysis,
)
from acts.services import (
    ActWorkflowError,
    apply_ko_decision,
    approve_act,
    return_to_otk,
    send_to_ko,
)
from notifications.models import Notification, NotificationDelivery
from references.models import ActStatus, DefectType, Operation
from tasks.models import Task, TaskAssignee


class StaleActStateTests(TestCase):
    """A second request holding an outdated act must be refused, not applied."""

    @classmethod
    def setUpTestData(cls):
        cls.status_created, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        cls.status_ko, _ = ActStatus.objects.get_or_create(
            code='KO_REVIEW', defaults={'name': 'На рассмотрении КО'}
        )
        cls.status_to, _ = ActStatus.objects.get_or_create(
            code='TO_ANALYSIS', defaults={'name': 'На анализе ТО'}
        )
        cls.status_otk_review = ActStatus.objects.get(code='OTK_REVIEW')
        cls.status_archived = ActStatus.objects.get(code='ARCHIVED')
        # Codes the act form actually offers, so the edit view reaches its
        # locked write path instead of failing form validation first.
        cls.operation, _ = Operation.objects.get_or_create(
            code='OPERATIONAL_CONTROL', defaults={'name': 'Операционный контроль'}
        )
        cls.defect_type, _ = DefectType.objects.get_or_create(
            code='SIZE_NONCONFORMITY', defaults={'name': 'Несоответствие размеров'}
        )
        cls.department = Department.objects.create(code='STALE_TO', name='Технологический отдел')

        cls.otk_user = cls._create_user('stale_otk', UserProfile.Role.OTK)
        cls.ko_user = cls._create_user('stale_ko', UserProfile.Role.KO)
        cls.to_user = cls._create_user('stale_to', UserProfile.Role.TO)
        cls.to_user.userprofile.department = cls.department
        cls.to_user.userprofile.save(update_fields=['department'])

    @classmethod
    def _create_user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.save(update_fields=['role'])
        return user

    def _create_act(self, status, **overrides):
        values = {
            'created_by': self.otk_user,
            'nomenclature': 'Катушка',
            'status': status,
        }
        values.update(overrides)
        return Act.objects.create(**values)

    def _create_structure(self, act, assignees=None):
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment='Мероприятие',
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=5),
        )
        for user in assignees or [self.to_user]:
            ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=user)
        return action

    def test_transition_with_a_fresh_instance_succeeds(self):
        act = self._create_act(self.status_created)

        updated = send_to_ko(act, self.otk_user)

        self.assertEqual(updated.status.code, 'KO_REVIEW')
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.SENT_TO_KO
            ).count(),
            1,
        )

    def test_repeated_send_to_ko_with_a_stale_instance_is_refused(self):
        act = self._create_act(self.status_created)
        stale = Act.objects.select_related('status').get(pk=act.pk)

        send_to_ko(act, self.otk_user)

        # `stale` still believes the act is CREATED_OTK.
        self.assertEqual(stale.status.code, 'CREATED_OTK')
        with self.assertRaises(ActWorkflowError):
            send_to_ko(stale, self.otk_user)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.SENT_TO_KO
            ).count(),
            1,
        )

    def test_repeated_ko_decision_with_a_stale_instance_is_refused(self):
        act = self._create_act(self.status_ko)
        stale = Act.objects.select_related('status').get(pk=act.pk)

        apply_ko_decision(act, self.ko_user, [(None, Act.KoDecision.ALLOW_NO_REWORK, 'Решение')])

        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(
                stale, self.ko_user, [(None, Act.KoDecision.PROHIBIT_USE, 'Повтор')]
            )

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertEqual(act.ko_decision, Act.KoDecision.ALLOW_NO_REWORK)
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.SENT_TO_TO
            ).count(),
            1,
        )

    def test_repeated_return_to_otk_with_a_stale_instance_adds_no_second_comment(self):
        act = self._create_act(self.status_ko)
        stale = Act.objects.select_related('status').get(pk=act.pk)

        return_to_otk(act, self.ko_user, 'Уточните данные.')

        with self.assertRaises(ActWorkflowError):
            return_to_otk(stale, self.ko_user, 'Уточните данные.')

        self.assertEqual(ActComment.objects.filter(act=act).count(), 1)
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.RETURNED_TO_OTK
            ).count(),
            1,
        )

    def test_ko_decision_rejects_a_defect_belonging_to_another_act(self):
        act = self._create_act(self.status_ko)
        own_defect = ActDefect.objects.create(
            act=act, defect_type=self.defect_type, description='Свой',
            detected_at=timezone.localdate(),
        )
        other_act = self._create_act(self.status_ko)
        foreign_defect = ActDefect.objects.create(
            act=other_act, defect_type=self.defect_type, description='Чужой',
            detected_at=timezone.localdate(),
        )

        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(
                act,
                self.ko_user,
                [(foreign_defect, Act.KoDecision.ALLOW_NO_REWORK, 'Чужое решение')],
            )

        foreign_defect.refresh_from_db()
        act.refresh_from_db()
        self.assertEqual(foreign_defect.ko_decision, '')
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertEqual(own_defect.pk, act.defects.get().pk)

    def test_repeated_approval_creates_no_duplicate_tasks_history_or_notifications(self):
        act = self._create_act(
            self.status_otk_review, ko_decision_by=self.ko_user, to_analysis_by=self.to_user
        )
        self._create_structure(act)
        stale = Act.objects.select_related('status').get(pk=act.pk)

        approve_act(act, self.otk_user)

        tasks_after_first = Task.objects.filter(act=act).count()
        assignees_after_first = TaskAssignee.objects.filter(task__act=act).count()
        approvals_after_first = ActHistoryEvent.objects.filter(
            act=act, event_type=ActHistoryEvent.EventType.APPROVED
        ).count()
        notifications_after_first = Notification.objects.filter(related_act=act).count()
        deliveries_after_first = NotificationDelivery.objects.filter(
            notification__related_act=act
        ).count()

        self.assertEqual(tasks_after_first, 1)
        self.assertEqual(approvals_after_first, 1)

        with self.assertRaises(ActWorkflowError):
            approve_act(stale, self.otk_user)

        self.assertEqual(Task.objects.filter(act=act).count(), tasks_after_first)
        self.assertEqual(TaskAssignee.objects.filter(task__act=act).count(), assignees_after_first)
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.APPROVED
            ).count(),
            approvals_after_first,
        )
        self.assertEqual(
            Notification.objects.filter(related_act=act).count(), notifications_after_first
        )
        self.assertEqual(
            NotificationDelivery.objects.filter(notification__related_act=act).count(),
            deliveries_after_first,
        )

    def _edit_payload(self, defect):
        return {
            'number_suffix': '34',
            'customer': 'Заказчик',
            'order_number': '100-3',
            'nomenclature': 'Катушка-А',
            'kd_designation': 'КД-103',
            'defects-TOTAL_FORMS': '1',
            'defects-INITIAL_FORMS': '1',
            'defects-MIN_NUM_FORMS': '1',
            'defects-MAX_NUM_FORMS': '1000',
            'defects-0-id': defect.id,
            'defects-0-workshop': ActDefect.Workshop.MP_SHOP,
            'defects-0-defect_type': self.defect_type.id,
            'defects-0-operation': self.operation.id,
            'defects-0-mp_type': 'OL',
            'defects-0-znp_number': '1-1',
            'defects-0-party_number': '2-2',
            'defects-0-checked_quantity': '10',
            'defects-0-nonconforming_quantity': '1',
            'defects-0-description': 'Изменённое описание',
            'defects-0-detected_at': timezone.localdate().isoformat(),
        }

    def _create_editable_act_with_defect(self):
        act = self._create_act(self.status_created)
        defect = ActDefect.objects.create(
            act=act, defect_type=self.defect_type, operation=self.operation,
            znp_number='1-1', party_number='2-2', mp_type='OL',
            workshop=ActDefect.Workshop.MP_SHOP, checked_quantity=10,
            nonconforming_quantity=1, description='Исходное описание',
            detected_at=timezone.localdate(),
        )
        return act, defect

    def test_editing_an_already_transferred_act_is_not_reachable(self):
        act, defect = self._create_editable_act_with_defect()
        send_to_ko(act, self.otk_user)
        self.client.force_login(self.otk_user)

        response = self.client.post(
            reverse('acts:edit', args=[act.pk]), self._edit_payload(defect)
        )

        defect.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(defect.description, 'Исходное описание')

    def test_edit_submitted_during_a_concurrent_transfer_is_refused_under_lock(self):
        """The view read a CREATED_OTK act, but the row moved on before the write."""
        act, defect = self._create_editable_act_with_defect()
        stale = Act.objects.select_related('status').get(pk=act.pk)
        send_to_ko(act, self.otk_user)
        self.client.force_login(self.otk_user)

        # The entry guard sees the stale CREATED_OTK copy and lets the POST in;
        # only the locked re-read inside the atomic block can catch this.
        with mock.patch('acts.views._get_act_for_detail', return_value=stale):
            response = self.client.post(
                reverse('acts:edit', args=[act.pk]), self._edit_payload(defect), follow=True
            )

        act.refresh_from_db()
        defect.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertEqual(defect.description, 'Исходное описание')
        self.assertFalse(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.ACT_EDITED
            ).exists()
        )
        self.assertContains(response, 'редактирование недоступно')
