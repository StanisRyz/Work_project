"""Regression cover for the shared user-text rules (`static/css/text.css`).

Three tests, and deliberately not a rendering snapshot: a Django test cannot
measure a layout, and asserting the exact markup of a card would break on every
redesign. What it *can* assert — and what actually regressed in practice — is
the contract between a page and the shared stylesheet:

* the element that renders free-form text carries `.user-text`, so it wraps an
  unbroken token instead of widening its column or its card;
* the page loads `text.css` at all, which is what makes that class mean
  anything.

Both a screen page and the printable form are covered, because paper has no
scrollbar to fall back on: text that does not wrap there is simply lost off the
sheet.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActDefect
from protocols.models import QUALITY_PROTOCOL_TYPE_CODE, ProtocolAgendaItem, ProtocolType
from protocols.services import create_protocol
from references.models import ActStatus, DefectType


# One pasted token with no space in it — the case that has no wrap opportunity
# at all and used to run straight past the right edge of its container.
UNBROKEN = 'ЗНП' + '9' * 240

# The same in a sentence, so the assertions also cover ordinary long prose.
LONG_TEXT = 'Отклонение по всей длине партии. ' * 12 + UNBROKEN


class UserTextRenderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(code='TXT_TO', name='Технологический отдел')
        cls.otk_user = cls._employee('text_otk', UserProfile.Role.OTK)
        cls.defect_type = DefectType.objects.create(code='TXT_OTHER', name='Другое')
        cls.act = Act.objects.create(
            number='АОК-2026-00777',
            created_by=cls.otk_user,
            nomenclature=UNBROKEN,
            act_type=Act.Type.OPERATIONAL_CONTROL,
            status=ActStatus.objects.create(code='CREATED_OTK', name='Создан ОТК'),
        )
        ActDefect.objects.create(
            act=cls.act,
            defect_type=cls.defect_type,
            workshop=ActDefect.Workshop.MP_SHOP,
            description=LONG_TEXT,
            detected_at=timezone.localdate(),
        )

    @classmethod
    def _employee(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.department = cls.department
        profile.save(update_fields=['role', 'department'])
        return user

    def test_long_defect_description_wraps_inside_its_table_cell(self):
        """The defects table: the description cell opts into the shared rules.

        `.act-defects-table` is fixed-layout with a per-column width, so a cell
        that does not wrap spills its text over the neighbouring columns rather
        than widening them — which is exactly what a 240-character ЗНП did.
        """
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[self.act.pk]))

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('css/text.css', page)
        self.assertIn(f'<td class="user-text">{LONG_TEXT}</td>', page)
        # «Данные акта» is a card, not a table, and reads the same field family.
        self.assertIn(f'<dd class="user-text">{UNBROKEN}</dd>', page)

    def test_long_agenda_item_wraps_inside_its_protocol_card(self):
        """A detail card: one agenda item, rendered on the protocol page."""
        author = self._employee('text_author', UserProfile.Role.OTK)
        protocol = create_protocol(
            ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE), author
        )
        ProtocolAgendaItem.objects.create(protocol=protocol, text=LONG_TEXT, display_order=0)
        # A reader, not the author: the author sees the editor, and it is the
        # read-only card whose wrapping this covers.
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('protocols:detail', args=[protocol.pk]))

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('css/text.css', page)
        self.assertIn(f'<p class="user-text">{LONG_TEXT}</p>', page)

    def test_printable_act_loads_the_shared_text_rules(self):
        """The printable form: paper has a hard right edge and no scrollbar.

        `text.css` is what gives `.print-section table` its fixed layout and its
        cells their wrapping, so the printable form has to load it — it extends
        no base template and would otherwise get neither.
        """
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:print', args=[self.act.pk]))

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('css/text.css', page)
        self.assertIn(LONG_TEXT, page)
