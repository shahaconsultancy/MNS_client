import json

from django.core.management.base import BaseCommand

from fund_analyzer.portfolio_store import coverage_report
from fund_analyzer.providers import fetch_amfi_latest


class Command(BaseCommand):
    help = 'Print which eligible equity schemes have verified holdings.'

    def add_arguments(self, parser):
        parser.add_argument('--full', action='store_true', help='Include each fund needing administrator action.')

    def handle(self, *args, **options):
        report = coverage_report(fetch_amfi_latest())
        if not options['full']:
            report['funds_requiring_action'] = len(report['funds_requiring_action'])
        self.stdout.write(json.dumps(report, indent=2))
