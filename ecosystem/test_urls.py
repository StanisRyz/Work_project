"""The public URL hierarchy: canonical paths and the temporary legacy aliases.

Only the shape of the routing is checked here — the views themselves are
covered by each module's own tests.
"""

from django.test import TestCase
from django.urls import reverse


class CanonicalUrlTests(TestCase):
    """User-facing modules live under `/quality/…` and `/calculators/…`."""

    def test_named_urls_generate_the_canonical_paths(self):
        self.assertEqual(reverse('acts:list'), '/quality/acts/')
        self.assertEqual(reverse('tasks:list'), '/quality/tasks/')
        self.assertEqual(reverse('calculator:page'), '/calculators/winding/')
        self.assertEqual(reverse('plate_cutting:page'), '/calculators/plate-cutting/')

        # A nested child route moved with its module instead of being rewritten.
        self.assertEqual(reverse('acts:detail', args=[7]), '/quality/acts/7/')
        self.assertEqual(reverse('calculator:entry_list'), '/calculators/winding/entries/')


class LegacyUrlCompatibilityTests(TestCase):
    """The pre-hierarchy paths still work, and still only as an alias."""

    def test_a_legacy_path_redirects_to_its_canonical_location_with_the_query(self):
        response = self.client.get('/acts/list-fragment/?tab=archive')

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response['Location'], '/quality/acts/list-fragment/?tab=archive')

    def test_a_legacy_post_keeps_its_method(self):
        # 307, not 301/302: a state-changing POST must not become a GET.
        response = self.client.post('/calculator/entries/create/')

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response['Location'], '/calculators/winding/entries/create/')
