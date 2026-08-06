from django.apps import AppConfig


class SyncConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sync'
    verbose_name = 'Data Sync'

    def ready(self):
        # Connect change-tracking signals (they no-op on standalone installs).
        from . import tracking
        tracking.connect()
