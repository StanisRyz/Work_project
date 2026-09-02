"""The «Вложения» branch: existing system files, seen from Documentation.

Nothing here stores a file, a folder or a row. `DocumentReference` is a read
model — a frozen value object built on demand from the attachment table that
already owns the file — so the Documentation module *points at* an act,
protocol or task attachment and can never hold a second copy of it:

    Documentation UI  →  DocumentReference (in memory)  →  ActAttachment.file

That is deliberate, and it is the reason no migration accompanies this branch.
A stored mirror table would have to be kept in step with three other apps on
every upload and delete, and the first missed hook would show a file that no
longer exists or hide one that does. A projection cannot drift.

Each source is one `AttachmentSource` subclass, registered in `SOURCES`. A
subclass says four things and nothing else: which records the user may read,
how a record is named and linked, where its attachment rows are, and which
function of the owning app authorises a download. Every one of those answers
is delegated — `documents` never re-implements another module's visibility or
ownership rule, so an act that becomes invisible in `acts` becomes invisible
here in the same request.

Adding a fourth source later is a subclass and a `SOURCES` entry. Search,
versions, audit history and approval are all layered on this interface rather
than on the individual apps, which is why the reference carries a stable
`(source, attachment_id)` identity and a `created_at` even though nothing
reads them yet.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Max, Q
from django.urls import reverse

from acts.models import Act, ActAttachment
from acts.permissions import (
    can_download_attachment as can_download_act_attachment,
    get_all_visible_acts_queryset,
)
from protocols.models import Protocol, ProtocolAttachment
from protocols.permissions import can_download_protocol_attachment
from protocols.selectors import get_readable_protocols_queryset
from tasks.models import Task, TaskAttachment
from tasks.permissions import can_download_task_attachment, get_readable_tasks_queryset


SYSTEM_AREA_LABEL = 'Вложения'


@dataclass(frozen=True)
class DocumentReference:
    """One system attachment, described for the Documentation browser.

    A projection of a row in `acts`, `protocols` or `tasks`; it owns nothing.
    `source` plus `attachment_id` is the stable identity a later feature
    (search index, version chain, audit entry) can key on without having to
    know which table the file actually lives in.
    """

    source: str
    source_label: str
    object_id: int
    object_label: str
    object_url: str
    attachment_id: int
    name: str
    size: int
    created_at: datetime | None
    download_url: str

    # Always true, and stated as data rather than inferred in a template: a
    # reference is read-only wherever it is rendered.
    is_system = True


@dataclass(frozen=True)
class ReferenceGroup:
    """One source record — an act, a protocol, a task — shown as a folder."""

    source: str
    object_id: int
    object_label: str
    object_url: str
    url: str
    count: int
    updated_at: datetime | None


class AttachmentSource:
    """One integrated module. Subclasses answer four questions, no more."""

    slug = ''
    label = ''
    # The record that owns the files (`Act`, `Protocol`, `Task`), the name of
    # the attachment's foreign key back to it, the attachment model itself,
    # and the name of its timestamp column — acts and protocols call it
    # `uploaded_at`, tasks `created_at`.
    record_model = None
    record_field = ''
    attachment_model = None
    timestamp_field = 'uploaded_at'

    # -- delegation to the owning app -------------------------------------

    def readable_records(self, user):
        """The records this user may read, as decided by the owning app."""
        raise NotImplementedError

    def record_label(self, record):
        raise NotImplementedError

    def record_url(self, record):
        raise NotImplementedError

    def can_download(self, attachment, user):
        """The owning app's own download rule, asked again for every file."""
        raise NotImplementedError

    def record_search_filter(self, query):
        """A `Q` over the *record* matching a search term.

        Only the identifiers and short descriptive text the module already
        shows — never a join into another app's tables. `search()` ORs this
        with a match on the filename itself.
        """
        raise NotImplementedError

    # -- the shared machinery ---------------------------------------------

    def _record_filter(self, user):
        """The readable ids as a subquery.

        The owning app's helper may carry `select_related`/`prefetch_related`
        sized for its own pages; only the rule is wanted here, so it is used
        as a values() subquery and never materialised.
        """
        return self.readable_records(user).values('pk')

    def groups(self, user):
        """Every readable record that has at least one attachment."""
        records = (
            self.record_model.objects.filter(pk__in=self._record_filter(user))
            .annotate(
                attachment_count=Count('attachments'),
                last_attachment_at=Max(f'attachments__{self.timestamp_field}'),
            )
            .filter(attachment_count__gt=0)
            .order_by('-last_attachment_at', '-pk')
        )
        return [
            ReferenceGroup(
                source=self.slug,
                object_id=record.pk,
                object_label=self.record_label(record),
                object_url=self.record_url(record),
                url=reverse('documents:system_record', args=[self.slug, record.pk]),
                count=record.attachment_count,
                updated_at=record.last_attachment_at,
            )
            for record in records
        ]

    def get_record(self, user, object_id):
        """The record itself, or None when this user may not read it."""
        return (
            self.record_model.objects.filter(pk__in=self._record_filter(user))
            .filter(pk=object_id)
            .first()
        )

    def build_reference(self, record, attachment):
        """One attachment row, described against the record that owns it."""
        return DocumentReference(
            source=self.slug,
            source_label=self.label,
            object_id=record.pk,
            object_label=self.record_label(record),
            object_url=self.record_url(record),
            attachment_id=attachment.pk,
            name=attachment.original_name,
            size=attachment.file_size,
            created_at=getattr(attachment, self.timestamp_field, None),
            download_url=reverse(
                'documents:system_download', args=[self.slug, attachment.pk]
            ),
        )

    def references(self, user, record):
        """Every attachment of one record, as read-only references."""
        attachments = self.attachment_model.objects.filter(
            **{self.record_field: record}
        ).order_by(f'-{self.timestamp_field}', '-pk')
        return [self.build_reference(record, attachment) for attachment in attachments]

    def search(self, user, query, limit=None):
        """References matching `query`, within what this user may read.

        Two ways to match, combined: the file's own name, and the record it
        belongs to — its identifier and the descriptive text the module shows
        beside it, spelled out by `record_search_filter()`. Searching for an
        act number therefore finds that act's photographs even though the
        number appears nowhere in their filenames.

        Still a projection: the same `DocumentReference` the browser builds,
        from the same rows. Nothing is indexed and nothing is stored.
        """
        readable = self._record_filter(user)
        matching_records = (
            self.record_model.objects.filter(pk__in=readable)
            .filter(self.record_search_filter(query))
            .values('pk')
        )
        attachments = (
            self.attachment_model.objects.filter(**{f'{self.record_field}__in': readable})
            .filter(
                Q(original_name__icontains=query)
                | Q(**{f'{self.record_field}__in': matching_records})
            )
            .select_related(self.record_field)
            .order_by(f'-{self.timestamp_field}', '-pk')
        )
        if limit is not None:
            attachments = attachments[:limit]
        return [
            self.build_reference(getattr(attachment, self.record_field), attachment)
            for attachment in attachments
        ]

    def resolve_attachment(self, user, attachment_id):
        """The attachment row for a download, or None.

        Two independent checks, because either alone would be a hole: the row
        must belong to a record this user may read, *and* the owning app's own
        download rule must say yes. Never trusts the URL.
        """
        attachment = (
            self.attachment_model.objects.filter(pk=attachment_id)
            .select_related(self.record_field)
            .first()
        )
        if attachment is None:
            return None
        if not self.record_model.objects.filter(
            pk__in=self._record_filter(user),
            pk=getattr(attachment, f'{self.record_field}_id'),
        ).exists():
            return None
        if not self.can_download(attachment, user):
            return None
        return attachment


class ActAttachmentSource(AttachmentSource):
    slug = 'acts'
    label = 'Акты'
    record_model = Act
    record_field = 'act'
    attachment_model = ActAttachment
    timestamp_field = 'uploaded_at'

    def readable_records(self, user):
        return get_all_visible_acts_queryset(user)

    def record_label(self, record):
        return f'Акт {record.number}' if record.number else f'Акт #{record.pk}'

    def record_url(self, record):
        return reverse('acts:detail', args=[record.pk])

    def can_download(self, attachment, user):
        return can_download_act_attachment(attachment, user)

    def record_search_filter(self, query):
        # The act number is what people actually type; the nomenclature is the
        # other thing the registry shows next to it.
        return Q(number__icontains=query) | Q(nomenclature__icontains=query)


class ProtocolAttachmentSource(AttachmentSource):
    slug = 'protocols'
    label = 'Протоколы'
    record_model = Protocol
    record_field = 'protocol'
    attachment_model = ProtocolAttachment
    timestamp_field = 'uploaded_at'

    def readable_records(self, user):
        # Protocols are readable by every authenticated user, and the selector
        # takes no user for that reason; the authentication check is the one
        # `documents/views.py` already made.
        if not getattr(user, 'is_authenticated', False):
            return Protocol.objects.none()
        return get_readable_protocols_queryset()

    def record_label(self, record):
        return f'Протокол №{record.number}'

    def record_url(self, record):
        return reverse('protocols:detail', args=[record.pk])

    def can_download(self, attachment, user):
        return can_download_protocol_attachment(attachment, user)

    def record_search_filter(self, query):
        # A protocol is identified by its type plus a number that is unique
        # only within that type, so both are searchable. A bare number matches
        # numerically as well as inside the type name.
        condition = Q(protocol_type__name__icontains=query)
        digits = query.strip().lstrip('№').strip()
        if digits.isdigit():
            condition |= Q(number=int(digits))
        return condition


class TaskAttachmentSource(AttachmentSource):
    slug = 'tasks'
    label = 'Задачи'
    record_model = Task
    record_field = 'task'
    attachment_model = TaskAttachment
    timestamp_field = 'created_at'

    def readable_records(self, user):
        return get_readable_tasks_queryset(user)

    def record_label(self, record):
        return f'Задача №{record.pk}'

    def record_url(self, record):
        return reverse('tasks:detail', args=[record.pk])

    def can_download(self, attachment, user):
        return can_download_task_attachment(attachment, user)

    def record_search_filter(self, query):
        # A task has no business number of its own — it is identified by its
        # row id and described by its text.
        condition = Q(task_text__icontains=query)
        digits = query.strip().lstrip('#№').strip()
        if digits.isdigit():
            condition |= Q(pk=int(digits))
        return condition


# The registry the views walk. Order is the order the «Вложения» folder lists
# its subfolders in.
SOURCES = {
    source.slug: source
    for source in (ActAttachmentSource(), ProtocolAttachmentSource(), TaskAttachmentSource())
}


def get_source(slug):
    """The source adapter for a URL segment, or None for an unknown one."""
    return SOURCES.get(slug)
