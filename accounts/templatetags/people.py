"""How a person is written on screen, in one place.

Django's `User.__str__()` is the login — «ryzhakov» — and a page that renders a
`User` object therefore shows an account name where a colleague's name belongs,
and quietly publishes who holds which account. Every template that names a
person uses `{{ user|person_name }}` instead, so the fallback rule exists once
rather than as a `get_full_name|default:username` expression copied around.

Presentation only: nothing here reads a permission, and the authentication
username is untouched.
"""

from django import template


register = template.Library()


@register.filter
def person_name(user):
    """The employee's full name, falling back to their login.

    An account with no first or last name still has to be identifiable, which
    is what the username fallback is for — never a blank cell or a «—» in
    place of a real person.
    """
    if user is None:
        return ''
    get_full_name = getattr(user, 'get_full_name', None)
    full_name = (get_full_name() or '').strip() if callable(get_full_name) else ''
    if full_name:
        return full_name
    get_username = getattr(user, 'get_username', None)
    return get_username() if callable(get_username) else str(user)


@register.filter
def person_initials(user):
    """Two letters for the avatar circle, derived from the displayed name.

    Built from `person_name()` so the circle and the label beside it can never
    disagree: a named employee gets the initials of their name, an account
    without one keeps the first two characters of its login.
    """
    name = person_name(user)
    parts = [part for part in name.split() if part]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return name[:2].upper()
