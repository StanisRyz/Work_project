from django.contrib.auth.models import User
from django.db import models

from accounts.models import Department
from acts.models import Act, ActCorrectiveAction, ActRootAnalysis
from references.models import TaskStatus


class Task(models.Model):
    source_action = models.OneToOneField(
        ActCorrectiveAction,
        on_delete=models.PROTECT,
        related_name='task',
        verbose_name='Исходное корректирующее мероприятие',
    )
    act = models.ForeignKey(Act, on_delete=models.PROTECT, related_name='tasks', verbose_name='Акт')
    root_analysis = models.ForeignKey(
        ActRootAnalysis,
        on_delete=models.PROTECT,
        related_name='tasks',
        verbose_name='Корневая причина',
    )
    task_text = models.TextField('Задача')
    department = models.ForeignKey(Department, on_delete=models.PROTECT, verbose_name='Подразделение')
    due_date = models.DateField('Срок')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_tasks', verbose_name='Создал')
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    status = models.ForeignKey(TaskStatus, on_delete=models.PROTECT, verbose_name='Статус')
    completed_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='completed_tasks', verbose_name='Завершил',
    )
    completed_at = models.DateTimeField('Завершена', null=True, blank=True)

    class Meta:
        ordering = ['due_date', 'created_at']
        verbose_name = 'Задача'
        verbose_name_plural = 'Задачи'

    def __str__(self):
        return f'Задача #{self.pk}: {self.task_text[:60]}'


class TaskAssignee(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='assignees', verbose_name='Задача')
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='task_assignments', verbose_name='Исполнитель')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['task', 'user'], name='unique_task_assignee')]
        verbose_name = 'Исполнитель задачи'
        verbose_name_plural = 'Исполнители задач'

    def __str__(self):
        return f'{self.task}: {self.user}'
