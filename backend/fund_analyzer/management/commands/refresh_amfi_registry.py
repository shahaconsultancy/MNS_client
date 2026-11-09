"""Download AMFI's NAVAll.txt now and replace the search registry if the feed validates.

    python manage.py refresh_amfi_registry          # scheduled use, e.g. daily from cron
    python manage.py refresh_amfi_registry --force  # accept a feed that failed the count checks
"""
from django.core.management.base import BaseCommand, CommandError

from fund_analyzer.scheme_registry import refresh_registry


class Command(BaseCommand):
    help = 'Refresh the searchable AMFI scheme registry from the official NAVAll.txt feed.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Use the feed even if its scheme count looks suspicious.')

    def handle(self, *args, **options):
        result = refresh_registry(force=options['force'], wait_for_lock=True)
        registry = result.get('registry') or {}
        self.stdout.write(f"{result['status']}: {registry.get('schemeCount')} schemes, {registry.get('amcCount')} AMCs, "
                          f"NAV date {registry.get('navDate')}, updated {registry.get('updatedAt')}")
        for warning in registry.get('warnings') or []:
            self.stdout.write(self.style.WARNING(warning))
        if result['status'] != 'refreshed':
            raise CommandError(result.get('detail') or result['status'])
