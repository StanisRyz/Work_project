"""What somebody reported as broken, and nothing else.

One model. A `BugReport` is the message a person typed into «Сообщить об
ошибке» in the topbar, the page they were on when they typed it, and who they
are — the facts of the report, stored so it survives the notification that
announced it.

There is no status, no assignee and no workflow here on purpose: this app owns
the *report*, while the work it creates is a real `tasks.Task` tracked in
«Задачи» like every other, and telling the responsible people about it is the
existing notification pipeline's job
(`notifications.services.notify_bug_reported()`). Both are reached only through
`bugs/services.py`. Who those people are is
`accounts.UserProfile.is_bug_responsible`, set in Django Admin — never a role
check and never a list in code.
"""

from django.conf import settings
from django.db import models


class BugReport(models.Model):
    """One «Сообщить об ошибке» submission."""

    # `PROTECT`, like every other authored row in the project: a deleted
    # account must not take the reports it filed with it.
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='bug_reports',
        verbose_name='Автор',
    )
    message = models.TextField('Описание проблемы')
    # Where the reporter was when they pressed the button — the single most
    # useful fact for reproducing a bug, and the reason the topbar button
    # carries the current path. A relative path only: it is filled from the
    # request the report was submitted with, never from user input.
    page_url = models.CharField('Страница', max_length=500, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Сообщение об ошибке'
        verbose_name_plural = 'Сообщения об ошибках'

    def __str__(self):
        return f'Ошибка №{self.pk}'

    @property
    def label(self):
        """The identifier a person recognises, written in one place.

        Read by the notification text, by the page heading and by the task
        registry's «Источник» column, so the report can never be named
        differently in the bell, in «Задачи» and on its own page.
        """
        return f'Сообщение об ошибке №{self.pk}'

    @property
    def task(self):
        """The `tasks.Task` this report raised, or `None`.

        At most one by the `unique_bug_report_task` constraint, so the page
        links to *that* task rather than to a search. `None` only when nobody
        was marked «Ответственный за ошибки» at the time — there was then
        nobody to raise it on.
        """
        return self.tasks.first()
