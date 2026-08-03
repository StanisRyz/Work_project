from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    """Technical app for operational tooling only.

    It deliberately defines no models and therefore has no migrations: it only
    hosts the SQLite -> PostgreSQL migration-bundle services and their
    management commands.
    """

    name = 'maintenance'
    verbose_name = 'Обслуживание'
