"""Run the browser-client smoke test as part of `manage.py test`.

RT-3 adds no npm, Jest, React or other JavaScript toolchain: the client is
exercised by a hand-rolled DOM/EventSource harness on plain Node. When Node is
not installed the test skips rather than failing, and the server-side suite
still covers everything Django owns.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


NODE = shutil.which('node')
TEST_SCRIPT = Path(settings.BASE_DIR) / 'realtime' / 'tests' / 'js' / 'realtime_client_test.js'


@unittest.skipUnless(NODE, 'Node.js недоступен: клиентский smoke test пропущен.')
class RealtimeClientSmokeTests(SimpleTestCase):
    def test_the_browser_client_passes_its_dom_smoke_test(self):
        completed = subprocess.run(
            [NODE, str(TEST_SCRIPT)],
            cwd=str(settings.BASE_DIR),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn('passed', output)
        self.assertNotIn('FAIL', output)
