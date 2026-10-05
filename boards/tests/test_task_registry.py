"""«Задачи» opens with a `BOARD` task in it: the registry, the page, the export."""

from django.test import TestCase
from django.urls import reverse

from tasks.models import Task

from .helpers import BoardFixtureMixin


class BoardTaskInRegistryTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.task = Task.objects.get(board_card=self.card('Позвонить заказчику'))
        self.client.force_login(self.member)

    def test_registry_lists_it(self):
        for tab in ('my', 'all'):
            with self.subTest(tab=tab):
                response = self.client.get(reverse('tasks:list'), {'tab': tab})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Позвонить заказчику')

    def test_registry_filters_by_board_source(self):
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source_type': 'BOARD'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Позвонить заказчику')

    def test_detail_page_opens(self):
        response = self.client.get(reverse('tasks:detail', args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Позвонить заказчику')
        self.assertContains(response, 'Карточка доски')

    def test_excel_export_opens(self):
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'export': 'xlsx'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response['Content-Type'])
