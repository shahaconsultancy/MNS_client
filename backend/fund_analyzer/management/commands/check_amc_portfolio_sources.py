import json

from django.core.management.base import BaseCommand

from fund_analyzer.source_monitor import health_report, run_checks


class Command(BaseCommand):
    help = (
        'Check every AMC monthly-portfolio source once and record its health with evidence. '
        'Run on a schedule outside web requests, e.g. daily: '
        'python manage.py check_amc_portfolio_sources --verify-files --recover --ingest --max-seconds 900'
    )

    def add_arguments(self, parser):
        parser.add_argument('--amc', help='Only this AMC, by its exact AMFI name.')
        parser.add_argument('--force', action='store_true', help='Ignore open circuit breakers (never access restrictions recorded in amc_sources.json).')
        parser.add_argument('--verify-files', action='store_true', help='Download the newest listed file per AMC once and check it opens.')
        parser.add_argument('--recover', action='store_true', help='For a page that no longer lists files, look for its new address in the AMC\'s own sitemap and validate it.')
        parser.add_argument('--ingest', action='store_true', help='Then fetch new months for funds that have been analysed before.')
        parser.add_argument('--max-seconds', type=float, help='Stop after this long. The next run carries on.')
        parser.add_argument('--report', action='store_true', help='Only print the recorded health; send no requests.')
        parser.add_argument('--json', action='store_true', help='Print JSON.')

    def handle(self, *args, **options):
        if options['report']:
            report = health_report()
            if options['json']:
                self.stdout.write(json.dumps(report, indent=1, default=str))
                return
            self.stdout.write(f"AMC source health {report['generated_at']}  {report['by_state']}")
            for row in report['sources']:
                failure = row['last_failure'] or {}
                evidence = row['evidence'] or {}
                self.stdout.write(f"  {row['display_state']:<24} {str(row['amc'])[:36]:<36} latest {row['latest_month'] or '-':<8} checked {str(row['last_checked_at'] or '-')[:16]}"
                                  + (f"  | {failure.get('category')} at {failure.get('stage')} HTTP {evidence.get('http_status') or '-'}" if row['state'] != 'WORKING' and failure else ''))
            return
        summary = run_checks(
            amc=options['amc'], force=options['force'], verify_files=options['verify_files'],
            recover=options['recover'], ingest=options['ingest'], max_seconds=options['max_seconds'],
        )
        if options['json']:
            self.stdout.write(json.dumps(summary, indent=1, default=str))
            return
        self.stdout.write(f"Checked {summary['amcs_checked']} AMC sources: {summary['by_state']}")
        for row in summary['sources']:
            months = ', '.join(row.get('months', [])[:3]) or '-'
            extra = ''
            if row.get('file_check'):
                check = row['file_check']
                extra = f"  file {check['month']} {'ok ' + check.get('kind', '') if check['ok'] else 'FAILED ' + check.get('error', '')}"
            if row.get('error'):
                extra += f"  | {row.get('category') or ''} {row['error'][:110]}"
            if (row.get('recovery') or {}).get('validated_url'):
                extra += f"  | moved -> {row['recovery']['validated_url']}"
            self.stdout.write(f"  {row['state']:<24} {row['amc'][:36]:<36} months {months:<26}{extra}")
        if 'ingestion' in summary:
            self.stdout.write('Ingestion: ' + json.dumps(summary['ingestion'].get('results') or summary['ingestion']))
