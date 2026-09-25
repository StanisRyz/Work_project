"""The acts registry's conveniences: sorting, customer search, KPI shortcuts,
tab counts and the deadline in words. The list itself is still decided by
`acts.selectors.build_act_list_state()`; these tests pin the parameters it
accepts and that every shortcut opens a list as long as its number."""

from datetime import date, timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from acts.models import Act
from acts.selectors import build_act_list_state
from ecosystem.templatetags.registry import due_hint, due_tone, plural_ru
from references.models import ActStatus


class RegistryConvenienceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())
        cls.manager = User.objects.create_user('reg_manager', password='pw-12345')
        profile = cls.manager.userprofile
        profile.role = UserProfile.Role.MANAGER
        profile.save(update_fields=['role'])
        today = timezone.localdate()
        created = ActStatus.objects.get(code='CREATED_OTK')
        ko = ActStatus.objects.get(code='KO_REVIEW')
        cls.alpha = cls._act('АОК-2026-00003', 'Альфа', created, today - timedelta(days=2))
        cls.beta = cls._act('АОК-2026-00001', 'Бета', ko, today + timedelta(days=5))
        cls.gamma = cls._act('АОК-2026-00002', 'Гамма', ko, today - timedelta(days=1))

    @classmethod
    def _act(cls, number, customer, status, due):
        return Act.objects.create(
            number=number, customer=customer, nomenclature='Катушка',
            created_by=cls.manager, status=status, due_date=due,
        )

    def state(self, **params):
        return build_act_list_state(self.manager, {'scope': 'all', **params})

    def test_sorting_by_every_offered_column_both_ways(self):
        self.assertEqual(list(self.state(sort='number')['acts']), [self.beta, self.gamma, self.alpha])
        self.assertEqual(list(self.state(sort='-customer')['acts']), [self.gamma, self.beta, self.alpha])
        self.assertEqual(list(self.state(sort='due')['acts']), [self.alpha, self.gamma, self.beta])

    def test_an_unknown_sort_is_ignored_not_executed(self):
        state = self.state(sort='created_by__password')

        self.assertEqual(state['sort'], '')
        self.assertEqual(len(state['acts']), 3)

    def test_search_finds_the_customer(self):
        self.assertEqual(list(self.state(search='Гамм')['acts']), [self.gamma])

    def test_each_kpi_counts_the_list_its_link_opens(self):
        state = self.state()
        self.assertEqual(state['kpis']['ko_review'], 2)
        self.assertEqual(state['kpis']['overdue'], 2)

        ko_list = self.state(status=state['kpi_filters']['ko_review'])
        self.assertEqual(len(ko_list['acts']), 2)
        # The strip does not shrink under its own filter, so switching from
        # one counter to another still shows the right numbers.
        self.assertEqual(ko_list['kpis']['created_otk'], 1)
        self.assertEqual(len(self.state(due='overdue')['acts']), 2)

    def test_the_page_draws_the_shortcuts_counts_and_deadline_words(self):
        self.client.force_login(self.manager)

        page = self.client.get(reverse('acts:list'), {'scope': 'all'})

        self.assertContains(page, 'class="tab-count">3</span>')
        self.assertContains(page, f'href="?scope=all&amp;status={self.state()["kpi_filters"]["ko_review"]}"')
        self.assertContains(page, 'просрочен на 2 дня')
        self.assertContains(page, 'data-registry-filter')
        self.assertContains(page, 'data-registry-memory="acts:')


class DueHintTests(SimpleTestCase):
    today = date(2026, 9, 25)

    def test_phrasing(self):
        cases = {
            0: 'сегодня', 1: 'завтра', 2: 'через 2 дня', 5: 'через 5 дней',
            21: 'через 21 день', -1: 'просрочен на 1 день', -11: 'просрочен на 11 дней',
        }
        for offset, expected in cases.items():
            with self.subTest(offset=offset):
                self.assertEqual(due_hint(self.today + timedelta(days=offset), self.today), expected)

    def test_tone(self):
        self.assertEqual(due_tone(self.today - timedelta(days=1), self.today), 'overdue')
        self.assertEqual(due_tone(self.today + timedelta(days=1), self.today), 'soon')
        self.assertEqual(due_tone(self.today + timedelta(days=2), self.today), '')
        self.assertEqual(due_hint(None), '')

    def test_plural(self):
        self.assertEqual([plural_ru(n, 'день', 'дня', 'дней') for n in (1, 3, 11, 22, 25)],
                         ['день', 'дня', 'дней', 'дня', 'дней'])
