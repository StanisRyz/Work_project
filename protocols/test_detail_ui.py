"""Regression cover for the accordion protocol page.

Two tests, and deliberately not a rendering snapshot: what can actually break
when the markup is restructured is the *contract between the page and the code
around it* — the editor's formset field names and hooks, and the live fragment
wrappers the realtime client replaces. Both are asserted here; how the cards
look is not.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile

from .models import (
    QUALITY_PROTOCOL_TYPE_CODE,
    Protocol,
    ProtocolAction,
    ProtocolActionAssignee,
    ProtocolAgendaItem,
    ProtocolType,
)
from .services import add_participant, add_speech, create_protocol, send_protocol_for_approval


def _employee(username, department, first_name, last_name):
    user = User.objects.create_user(
        username=username, password='demo12345', first_name=first_name, last_name=last_name
    )
    profile = user.userprofile
    profile.department = department
    profile.role = UserProfile.Role.OTK
    profile.position = 'Специалист'
    profile.save(update_fields=['department', 'role', 'position'])
    return user


class ProtocolDetailUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.quality = ProtocolType.objects.get(code=QUALITY_PROTOCOL_TYPE_CODE)
        cls.to = Department.objects.create(name='ТО', code='UI_TO')
        cls.author = _employee('ui_author', cls.to, 'Иван', 'Петров')
        cls.reviewer = _employee('ui_reviewer', cls.to, 'Пётр', 'Сидоров')
        cls.reader = _employee('ui_reader', cls.to, 'Ольга', 'Смирнова')

    def _build(self):
        protocol = create_protocol(self.quality, self.author)
        add_participant(
            protocol, self.reviewer, department=self.to, requires_approval=True, display_order=1
        )
        for order, text in enumerate(['Показатели качества', 'План улучшений']):
            ProtocolAgendaItem.objects.create(protocol=protocol, text=text, display_order=order)
        add_speech(protocol, protocol.participants.get(user=self.author), 'Доложил.')
        action = ProtocolAction.objects.create(
            protocol=protocol,
            task_text='Проверить оснастку',
            department=self.to,
            due_date=timezone.localdate() + timedelta(days=7),
            display_order=0,
        )
        ProtocolActionAssignee.objects.create(action=action, user=self.reviewer)
        return protocol

    def test_the_editor_sections_keep_every_hook_the_form_and_the_script_need(self):
        protocol = self._build()
        self.client.force_login(self.author)
        page = self.client.get(reverse('protocols:detail', args=[protocol.pk]))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()

        # The document card and its workflow indicator.
        self.assertIn('protocol-document-card', body)
        self.assertIn('protocol-workflow__step--current', body)

        # Every block is still a `[data-block]` with the row plumbing
        # `protocol_editor.js` drives and the formset names the server parses.
        for block in ('participants', 'agenda', 'speeches', 'actions'):
            self.assertIn(f'data-block="{block}"', body)
            self.assertIn(f'name="{block}-TOTAL_FORMS"', body)
        for hook in (
            'data-protocol-editor',
            'data-row-list',
            'data-row-template',
            'data-assignee-template',
            'data-add-row',
            'data-remove-row',
            'data-add-assignee',
            'data-section-count',
            'data-participant-user',
            'data-speaker-select',
            'data-employee-pair',
        ):
            self.assertIn(hook, body)

        # The two submit paths are unchanged: the form still posts the draft,
        # and the confirmation still retargets it at the submission endpoint.
        self.assertIn(f'action="{reverse("protocols:save_draft", args=[protocol.pk])}"', body)
        self.assertIn(
            f'data-confirm-form-action="{reverse("protocols:send_for_approval", args=[protocol.pk])}"',
            body,
        )

        # And the draft still saves through it, with the field names the
        # restructured cards render.
        response = self.client.post(
            reverse('protocols:save_draft', args=[protocol.pk]),
            {
                'participants-TOTAL_FORMS': '1',
                'participants-0-department': str(self.to.pk),
                'participants-0-user': str(self.reviewer.pk),
                'participants-0-requires_approval': 'on',
                'agenda-TOTAL_FORMS': '1',
                'agenda-0-text': 'Единственный вопрос',
                'speeches-TOTAL_FORMS': '1',
                'speeches-0-speaker': str(self.author.pk),
                'speeches-0-text': 'Доложил заново.',
                'actions-TOTAL_FORMS': '0',
            },
        )
        self.assertRedirects(response, reverse('protocols:detail', args=[protocol.pk]))
        self.assertEqual(
            list(protocol.agenda_items.values_list('text', flat=True)), ['Единственный вопрос']
        )
        self.assertEqual(protocol.actions.count(), 0)

    def test_the_read_only_page_renders_sections_and_keeps_the_live_wrappers(self):
        protocol = self._build()
        send_protocol_for_approval(protocol, self.author)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)

        self.client.force_login(self.reader)
        page = self.client.get(reverse('protocols:detail', args=[protocol.pk]))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()

        # Read-only cards, no editor anywhere.
        self.assertIn('protocol-participant-card--static', body)
        self.assertIn('protocol-task-card', body)
        self.assertNotIn('data-protocol-editor', body)

        # The approval section is the last one and carries the round.
        self.assertIn('protocol-section--approval', body)
        self.assertLess(
            body.index('data-live-protocol-content'), body.index('data-live-protocol-approval')
        )

        # The realtime client replaces exactly these wrappers, and each
        # fragment endpoint still answers with the markup that goes into them.
        for wrapper in (
            'data-live-protocol-config',
            'data-live-protocol-heading',
            'data-live-protocol-content',
            'data-live-protocol-approval',
        ):
            self.assertIn(wrapper, body)
        for name in ('heading_fragment', 'approval_fragment', 'content_fragment'):
            fragment = self.client.get(reverse(f'protocols:{name}', args=[protocol.pk]))
            self.assertEqual(fragment.status_code, 200, name)
            self.assertIn('protocol-', fragment.json()['html'])
