"""How a message of a card's «Чат» is written on screen.

Presentation only: nothing here reads a permission or decides who was
mentioned — that is `BoardCardCommentMention`, written by
`boards.services.post_card_comment()`.
"""

import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from accounts.templatetags.people import person_name


register = template.Library()


@register.filter
def with_mentions(comment):
    """The message's text, escaped, with «@Имя Фамилия» of everybody it
    really mentions wrapped in `.board-mention`, and its line breaks kept.

    The text is escaped first and only then searched, for the escaped names
    of the people stored on the message (`comment.mentions`, prefetched by
    the panel) — so `<script>` in a message or in somebody's name stays
    text, an «@» naming anybody else stays as typed, and a longer name wins
    over a shorter one it begins with. Nothing else of the text is touched.
    """
    text = escape(comment.text)
    names = sorted(
        {escape(person_name(mention.user)) for mention in comment.mentions.all()} - {''},
        key=len,
        reverse=True,
    )
    if names:
        pattern = re.compile('@(?:' + '|'.join(re.escape(name) for name in names) + r')(?!\w)')
        text = pattern.sub(lambda match: f'<span class="board-mention">{match.group(0)}</span>', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br>')
    return mark_safe(text)
