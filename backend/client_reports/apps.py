from django.apps import AppConfig


class ClientReportsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'client_reports'
    label = 'clients'  # keeps the existing table and migration history
