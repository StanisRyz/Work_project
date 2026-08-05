from django.apps import AppConfig


class EcosystemConfig(AppConfig):
    """The project package itself, registered so its checks are discovered.

    It defines no models and therefore has no migrations. Its only job is to
    import the deployment system checks, which need to run inside the ordinary
    `python manage.py check` so an unsafe production configuration stops a
    deployment before the process starts serving.
    """

    name = 'ecosystem'
    verbose_name = 'Конфигурация развёртывания'

    def ready(self):
        from . import checks  # noqa: F401
