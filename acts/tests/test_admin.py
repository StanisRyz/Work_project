from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, SimpleTestCase

from acts.admin import ActDefectInline
from acts.models import Act


class ReadOnlyInlineAdminTests(SimpleTestCase):
    def test_inline_rejects_add_permission_with_parent_object(self):
        inline = ActDefectInline(Act, AdminSite())

        self.assertFalse(inline.has_add_permission(RequestFactory().get('/'), object()))
