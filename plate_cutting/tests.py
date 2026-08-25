"""The agreed numbers, the page, and the saved package sets.

The arithmetic runs in the browser, so what a Python test can protect is the
source the browser reads — the seventeen coefficients and the page that hands
them over. The reference case from the specification is checked against those
same constants, not against a second copy of them.

The preset tests protect the other half: that a saved set keeps its packages
in order, that search and load hand back exactly what was stored, and that an
invalid package leaves nothing behind.
"""
import json
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .constants import HOLE_SECONDS, PLATE_LENGTH_RANGES
from .models import MAX_HOLE_COUNT, MAX_PLATE_COUNT, PlateCuttingPreset


class PlateCuttingConstantsTests(TestCase):
    def test_reference_case_matches_the_specification(self):
        """1–170 мм, 500 пластин, 1000 отверстий → 1320 с → 0,37 ч."""
        first = PLATE_LENGTH_RANGES[0]
        self.assertEqual((first.min_mm, first.max_mm), (1, 170))

        seconds = first.seconds * 500 + HOLE_SECONDS * 1000
        self.assertEqual(seconds, Decimal('1320'))
        self.assertEqual(round(seconds / 3600, 2), Decimal('0.37'))

    def test_bands_are_the_seventeen_agreed_contiguous_ranges(self):
        self.assertEqual(len(PLATE_LENGTH_RANGES), 17)
        self.assertEqual(PLATE_LENGTH_RANGES[-1].max_mm, 2890)
        for previous, current in zip(PLATE_LENGTH_RANGES, PLATE_LENGTH_RANGES[1:]):
            self.assertEqual(current.min_mm, previous.max_mm + 1)
            self.assertGreater(current.seconds, previous.seconds)


class PlateCuttingPageTests(TestCase):
    url = '/calculators/plate-cutting/'

    def test_url_name_resolves_to_the_agreed_route(self):
        self.assertEqual(reverse('plate_cutting:page'), self.url)

    def test_page_needs_a_login_and_then_renders_every_coefficient(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

        User.objects.create_user(username='cutter', password='demo12345')
        self.client.login(username='cutter', password='demo12345')
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_page'], 'plate_cutting')
        for band in PLATE_LENGTH_RANGES:
            self.assertContains(response, f'data-seconds="{band.seconds}"')
            self.assertContains(response, band.label)
        # The submenu leaf sits next to the winding calculator.
        self.assertContains(response, 'Калькулятор рубки пластин')

    def test_loading_a_set_asks_before_it_replaces_entered_packages(self):
        """A saved set may only overwrite the screen after an explicit «да».

        A source-level check rather than a browser one: the project has no
        JavaScript test runner and a reliability patch is not the place to add
        one. What it pins down is the shape of the fix — the page owns a real
        confirmation `<dialog>`, the load handler goes through it before
        `applyPreset()` replaces anything, and an untouched calculator is
        exempt — so a regression to «replace on click» is caught here instead
        of by a user losing eight filled packages.
        """
        User.objects.create_user(username='cutter', password='demo12345')
        self.client.login(username='cutter', password='demo12345')
        response = self.client.get(self.url)

        # The confirmation is the page's own modal, with both answers.
        for hook in ('data-replace-modal', 'data-replace-cancel', 'data-replace-accept'):
            self.assertContains(response, hook)

        script = (settings.BASE_DIR / 'static' / 'js' / 'plate_cutting.js').read_text(
            encoding='utf-8'
        )
        # Nothing is replaced until the confirmation resolves true, and the
        # picker comes back untouched when it does not.
        load_block = script.split("listEl.addEventListener('click'")[1]
        self.assertIn('await confirmReplace()', load_block)
        self.assertLess(
            load_block.index('confirmReplace()'), load_block.index('applyPreset('),
        )
        # An empty calculator still loads in one click.
        confirm_block = script.split('function confirmReplace()')[1].split('function applyPreset')[0]
        self.assertIn('if (!hasEnteredData()) return Promise.resolve(true);', confirm_block)
        # The answer is the page's own dialog, not a browser prompt.
        self.assertIn('replaceModal.showModal()', confirm_block)


class PlateCuttingPresetTests(TestCase):
    """Saving, searching and loading a set of packages."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='cutter', password='demo12345', first_name='Иван', last_name='Петров',
        )
        self.client.login(username='cutter', password='demo12345')

    def post_preset(self, payload):
        return self.client.post(
            reverse('plate_cutting:preset_create'),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_saving_keeps_every_package_in_the_order_it_was_sent(self):
        response = self.post_preset({
            'name': '  Январь  ',
            'packages': [
                {'range': '341', 'plates': 120, 'holes': 40},
                {'range': '1', 'plates': 500, 'holes': 1000},
                {'range': '2721', 'plates': 7, 'holes': 0},
            ],
        })

        self.assertEqual(response.status_code, 201)
        preset = PlateCuttingPreset.objects.get()
        self.assertEqual(preset.name, 'Январь')
        self.assertEqual(preset.author, self.user)
        self.assertEqual(
            [(p.display_order, p.range_value, p.plate_count, p.hole_count)
             for p in preset.packages.all()],
            [(0, '341', 120, 40), (1, '1', 500, 1000), (2, '2721', 7, 0)],
        )

    def test_search_is_case_insensitive_and_load_returns_the_saved_inputs(self):
        self.post_preset({'name': 'Февраль', 'packages': [{'range': '171', 'plates': 9, 'holes': 3}]})
        self.post_preset({
            'name': 'Мартовский заказ',
            'packages': [
                {'range': '511', 'plates': 200, 'holes': 15},
                {'range': '1', 'plates': 30, 'holes': 0},
            ],
        })

        found = self.client.get(reverse('plate_cutting:preset_list'), {'q': 'мАрт'})
        self.assertEqual(found.status_code, 200)
        presets = found.json()['presets']
        self.assertEqual([p['name'] for p in presets], ['Мартовский заказ'])
        self.assertEqual(presets[0]['author'], 'Иван Петров')
        self.assertEqual(presets[0]['package_count'], 2)

        detail = self.client.get(
            reverse('plate_cutting:preset_load', args=[presets[0]['id']])
        )
        self.assertEqual(detail.status_code, 200)
        preset = detail.json()['preset']
        self.assertEqual(preset['packages'], [
            {'range': '511', 'plates': 200, 'holes': 15},
            {'range': '1', 'plates': 30, 'holes': 0},
        ])
        # Inputs only: no calculated value is ever persisted or returned.
        for package in preset['packages']:
            self.assertEqual(set(package), {'range', 'plates', 'holes'})

    def test_an_invalid_package_saves_nothing_at_all(self):
        for packages in (
            [{'range': '1', 'plates': 10, 'holes': 0}, {'range': '999', 'plates': 5, 'holes': 0}],
            [{'range': '1', 'plates': 10, 'holes': 0}, {'range': '171', 'plates': 0, 'holes': 0}],
            [],
        ):
            with self.subTest(packages=packages):
                response = self.post_preset({'name': 'Набор', 'packages': packages})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()['detail'])

        self.assertFalse(PlateCuttingPreset.objects.exists())
        # A blank name is refused the same way.
        self.assertEqual(
            self.post_preset({'name': '   ', 'packages': [{'range': '1', 'plates': 1, 'holes': 0}]}).status_code,
            400,
        )
        self.assertFalse(PlateCuttingPreset.objects.exists())

    def test_a_count_above_the_limit_is_refused_before_the_insert(self):
        """The `integer` ceiling of PostgreSQL is never reached: it is a 400.

        Without the upper bound the same payload is stored on SQLite and
        aborts the transaction on PostgreSQL, i.e. the defect only appears in
        production. Both counters are checked; neither leaves a row behind.
        """
        for field, limit in (('plates', MAX_PLATE_COUNT), ('holes', MAX_HOLE_COUNT)):
            with self.subTest(field=field):
                package = {'range': '1', 'plates': 10, 'holes': 0}
                package[field] = limit + 1
                response = self.post_preset({'name': 'Набор', 'packages': [package]})

                self.assertEqual(response.status_code, 400)
                self.assertIn(str(limit), response.json()['detail'])

        self.assertFalse(PlateCuttingPreset.objects.exists())

    def test_malformed_numbers_are_a_validation_error_and_not_a_crash(self):
        """Strings `str.isdigit()` accepts but `int()` refuses used to be a 500."""
        for plates in ('--5', '²', '', '  ', 'пять', '1.5', None, [1]):
            with self.subTest(plates=plates):
                response = self.post_preset({
                    'name': 'Набор', 'packages': [{'range': '1', 'plates': plates, 'holes': 0}],
                })

                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()['detail'])

        self.assertFalse(PlateCuttingPreset.objects.exists())
