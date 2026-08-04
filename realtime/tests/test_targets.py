from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from realtime.targets import (
    RealtimeTarget,
    RealtimeTargetError,
    act_target,
    normalize_targets,
    user_target,
    user_targets,
)


class TargetContractTests(SimpleTestCase):
    def test_user_target_uses_the_documented_key_format(self):
        self.assertEqual(user_target(7).key, 'user:7')
        self.assertEqual(str(user_target(7)), 'user:7')

    def test_act_target_uses_the_documented_key_format(self):
        self.assertEqual(act_target(42).key, 'act:42')

    def test_none_is_ignored_rather_than_rejected(self):
        self.assertIsNone(user_target(None))
        self.assertIsNone(act_target(None))
        self.assertEqual(normalize_targets([None, user_target(3), None]), (user_target(3),))

    def test_empty_zero_and_negative_identifiers_are_refused(self):
        for invalid in (0, -1, -999):
            with self.subTest(identifier=invalid):
                with self.assertRaisesMessage(RealtimeTargetError, 'положительным'):
                    user_target(invalid)
        for invalid in ('', '7', 7.0, True):
            with self.subTest(identifier=invalid):
                with self.assertRaisesMessage(RealtimeTargetError, 'целым числом'):
                    user_target(invalid)

    def test_an_unknown_target_kind_is_refused(self):
        with self.assertRaisesMessage(RealtimeTargetError, 'Неизвестный тип target'):
            RealtimeTarget('department', 1)

    def test_duplicates_are_removed(self):
        targets = normalize_targets(
            [user_target(3), user_target(3), act_target(9), act_target(9)]
        )

        self.assertEqual(targets, (act_target(9), user_target(3)))

    def test_targets_are_sorted_deterministically(self):
        shuffled = [user_target(10), act_target(5), user_target(2), act_target(1)]

        first = normalize_targets(shuffled)
        second = normalize_targets(list(reversed(shuffled)))

        self.assertEqual(first, second)
        self.assertEqual(
            [target.key for target in first], ['act:1', 'act:5', 'user:2', 'user:10']
        )

    def test_a_single_target_may_be_passed_without_a_container(self):
        self.assertEqual(normalize_targets(user_target(4)), (user_target(4),))

    def test_none_and_empty_input_produce_no_targets(self):
        self.assertEqual(normalize_targets(None), ())
        self.assertEqual(normalize_targets([]), ())

    def test_non_target_values_are_refused(self):
        with self.assertRaisesMessage(RealtimeTargetError, 'Ожидался RealtimeTarget'):
            normalize_targets(['user:3'])

    def test_user_targets_maps_an_iterable_and_skips_none(self):
        self.assertEqual(
            user_targets([1, None, 2]), [user_target(1), user_target(2)]
        )


class TargetsFromModelsTests(TestCase):
    def test_a_model_instance_is_accepted_directly(self):
        user = User.objects.create_user(username='target_user', password='demo12345')

        self.assertEqual(user_target(user), user_target(user.pk))

    def test_an_unsaved_instance_is_ignored_rather_than_producing_a_broken_target(self):
        self.assertIsNone(user_target(User(username='unsaved')))
