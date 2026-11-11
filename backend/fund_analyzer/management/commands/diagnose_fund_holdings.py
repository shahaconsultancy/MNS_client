import json

from django.core.management.base import BaseCommand

from fund_analyzer.diagnostics import diagnose_scheme
from fund_analyzer.providers import fetch_amfi_latest


class Command(BaseCommand):
    help = 'Staff diagnostic: why each scheme\'s holdings are or are not available, stage by stage.'

    def add_arguments(self, parser):
        parser.add_argument('scheme_codes', nargs='+', help='AMFI scheme codes.')
        parser.add_argument('--live', action='store_true', help='Also run a fresh retrieval now (respects AMC circuit breakers and access restrictions).')
        parser.add_argument('--json', action='store_true', help='Print the full report as JSON.')

    def handle(self, *args, **options):
        records = fetch_amfi_latest()
        reports = [diagnose_scheme(code, live=options['live'], records=records) for code in options['scheme_codes']]
        if options['json']:
            self.stdout.write(json.dumps(reports, indent=1, default=str))
            return
        for report in reports:
            self.stdout.write(f"\n{report['scheme_code']}  {report.get('scheme_name') or ''}")
            if 'amc' not in report:
                self.stdout.write(f"  {report['final_reason']}")
                continue
            source, health, scheme = report['source'], report['amc_health'], report['scheme_status']
            self.stdout.write(f"  AMC            {report['amc']}  ({report['asset_class']})")
            self.stdout.write(f"  Source         {source['origin']} | {source['discovery_method']} | {source['access']}")
            self.stdout.write(f"                 {source['url']}")
            failure = health.get('last_failure') or {}
            evidence = failure.get('evidence') or {}
            self.stdout.write(f"  AMC health     {health['state']}  last check {health['last_checked_at'] or '-'}  latest month {health['latest_month'] or '-'}")
            if failure:
                self.stdout.write(f"                 last failure {failure.get('category')} at {failure.get('stage')} | HTTP {evidence.get('http_status') or '-'} {evidence.get('content_type') or ''} {('cdn=' + evidence['cdn']) if evidence.get('cdn') else ''}")
            if health.get('circuit_open_until'):
                self.stdout.write(f"                 circuit open until {health['circuit_open_until']}")
            self.stdout.write(f"  Scheme status  {scheme['status']} ({scheme['scope'] or '-'}, {scheme['category'] or '-'})  next check {scheme['next_check_at'] or '-'}  retry due now: {scheme['retry_due_now']}")
            self.stdout.write(f"  Stored months  {', '.join(m['month'] + (' (backup)' if m['third_party'] else '') for m in report['stored_months']) or 'none'}")
            self.stdout.write(f"  Backup         {'enabled' if report['backup']['enabled'] else 'disabled'}")
            for route in ('direct_listing', 'amfi_discovery'):
                step = (report.get('discovery') or {}).get(route)
                if step:
                    detail = step.get('reason') or step.get('detail') or (f"{step.get('links')} file(s)" if 'links' in step else '')
                    self.stdout.write(f"  {route:<14} {step['result']}{(' ' + step['category']) if step.get('category') else ''} | {detail} {('| ' + step['page']) if step.get('page') else ''}")
            for month in report['months']:
                if month['result'] == 'ok':
                    self.stdout.write(f"  {month['month']}        OK via {month.get('route', 'amc_page')} from {month.get('download_host', '-')} | {month['holdings']} holdings | {month['reporting_date']} | matched {month['matched']} | risk {month['riskometer'] or '-'}")
                else:
                    ev = month.get('evidence') or {}
                    self.stdout.write(f"  {month.get('month', 'listing')}  {month['category']} at {month['stage']} | HTTP {ev.get('http_status') or '-'} | {month['detail'][:160]}")
            self.stdout.write(f"  => {report['final_reason']}")
