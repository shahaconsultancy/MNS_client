import json

from django.core.management.base import BaseCommand

from fund_analyzer.ingestion import run_ingestion


class Command(BaseCommand):
    help = 'Fetch, validate and store new official monthly portfolios. Safe to run on a schedule.'

    def add_arguments(self, parser):
        parser.add_argument('--amc', help='Only this AMC, by its exact AMFI name.')
        parser.add_argument('--scheme', action='append', dest='schemes', help='Only this scheme code. Can be repeated.')
        parser.add_argument('--force', action='store_true', help='Ignore waiting periods after recent checks.')
        parser.add_argument('--months', type=int, default=2, help='Newest months to keep stored for each portfolio.')
        parser.add_argument('--retries', type=int, default=2, help='Retries for temporary download failures.')
        parser.add_argument('--pause', type=float, default=1.0, help='Seconds to wait between file downloads.')
        parser.add_argument('--max-seconds', type=float, help='Stop after this long. The next run carries on.')

    def handle(self, *args, **options):
        summary = run_ingestion(
            amc=options['amc'], scheme_codes=options['schemes'], force=options['force'],
            months_to_keep=options['months'], retries=options['retries'], pause=options['pause'], max_seconds=options['max_seconds'],
        )
        self.stdout.write(json.dumps(summary, indent=2))
