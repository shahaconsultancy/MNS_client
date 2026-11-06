# Money Navigating Solutions – Financial Planning Suite

- `frontend/` – React (Vite) app
- `backend/` – Django REST API (PostgreSQL, JWT auth)

## Frontend
```
cd frontend
npm install
npm run dev
```
Set `VITE_API_BASE_URL` in `frontend/.env` (see `.env.example`).

## Backend
```
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then edit DATABASE_URL
python manage.py makemigrations accounts
python manage.py migrate
python manage.py createsuperuser   # this is your advisor login (email + password)
python manage.py runserver
```

The Fund Analyzer uses Django's in-process cache for local development. Set
`REDIS_URL` (for example, `redis://localhost:6379/0`) in a deployed backend to
share cached AMFI/MFAPI responses across workers. Install the backend
requirements after updating them so the Redis cache backend is available.

### Highest Overlap monthly holdings

The analyzer identifies each scheme and AMC from AMFI's NAV list, then uses
verified monthly equity holdings from official AMC portfolio files. REIT/InvIT
units and derivatives are excluded. **Holdings use JSON files, not a database.**
They are read in this order:

1. The JSON cache, one file per AMC, portfolio and month:
   `portfolio_cache/<amc>/<portfolio>/<YYYY-MM>.json`. Direct and Regular plans of
   a scheme share one file. Months are kept, so an AMC outage or 403 doesn't
   affect data already cached.
2. Official AMC files, downloaded live when a month is missing, found by an adapter
   in `fund_analyzer/adapters.py`. HDFC, PPFAS, Nippon and SBI have dedicated
   adapters. Any other AMC is tried from the page AMFI lists for it at
   [Portfolio Disclosure](https://www.amfiindia.com/online-center/portfolio-disclosure).
   AMFI links to the AMCs; it does not host the files.
3. A file uploaded by staff through the import API below.

A file name is never trusted. Every workbook must name the scheme, report the
expected month, and contain valid ISINs and weights before it is cached. A cache
file is checked again when read, and a damaged one counts as missing. The card
uses the newest month common to the selected funds, within a lookback set by
`FUND_ANALYZER_PORTFOLIO_LOOKBACK_MONTHS` (default 6), and never pairs different
months. If nothing is cached and the source can't be read, it shows `Portfolio
holdings unavailable`.

**Fund types.** Every file is read for both equity shares and debt securities, and
each row is labelled. Weights are percentages of scheme NAV and are never rescaled
to 100%. Securities match only by ISIN, so two bonds from one issuer don't count as
common. Equity funds are compared on equity, debt funds on debt, and hybrid funds on
both. A pair uses what the two funds share, so Hybrid + Equity compares stocks and
Hybrid + Hybrid gives equity, debt and combined figures. An equity fund and a debt
fund share nothing, so every eligible security is compared. Components that weren't
compared are `null`, not zero.

JSON files saved before rows were labelled hold one kind only. When a pair needs the
other kind, the official file is read again from the saved `source_url`. If that
fails, the pair is listed in `unavailable_pairs` rather than shown as 0%.

The debt overlap covers direct debt securities only. Fund units (such as Corporate Debt
Market Development Fund, CDMDF), repo, cash and net current assets never count in
it. They are kept in `other_holdings` in the JSON file and shown in Fundamentals
with their own classification. Where two funds hold the same fund units, the
detailed overlap view adds a second figure labelled as including them.

A file is rejected if a stated weight doesn't match market value over grand total,
so `% to NAV` and `% to AUM` files are only compared when both are a share of the
same net assets. Securities can total more than 100% when net current assets are
negative, as liquid funds often show at month end, so Asset Allocation can show a
negative "other" figure.

**Cache folder.** Set `FUND_ANALYZER_HOLDINGS_CACHE_DIR`, for example
`D:\mns\portfolio_cache` on Windows or `/var/lib/mns/portfolio_cache` on a server.
The default is `backend/portfolio_cache`. Writes are atomic, so a crash or several
workers can't leave a half written file. Use a real disk in production: Vercel only
offers a temporary folder that is wiped between runs, so there every cold start
downloads again, and blocked AMCs have no cached copy to fall back on.

**Cleaning the cache.** Cleanup never runs during a request. Run it on a schedule:

```powershell
python manage.py cleanup_portfolio_cache                       # dry run: lists what would go, changes nothing
python manage.py cleanup_portfolio_cache --apply               # removes it
python manage.py cleanup_portfolio_cache --apply --archive-dir D:\mns\portfolio_archive
```

The policy:

| Kind | What happens |
|---|---|
| Always kept | A fund's newest official month, every month inside the official or backup lookback, staff uploads, anything a running job uses, and anything changed in the last hour. |
| Official months | Past `FUND_ANALYZER_CACHE_OFFICIAL_RETENTION_MONTHS` (12), moved to the archive folder. Never deleted outright, because an AMC may not offer them again. Without an archive folder they are kept. |
| Third-party months | Past `FUND_ANALYZER_CACHE_BACKUP_RETENTION_MONTHS` (6), deleted. |
| Unreadable month files | Moved to the archive for inspection, or reported. |
| Status files | Kept while their retry wait lasts. Removed once the wait is over and the last check is older than `FUND_ANALYZER_CACHE_STATUS_RETENTION_DAYS` (30). |
| Temporary files | Removed after `FUND_ANALYZER_CACHE_TEMP_FILE_HOURS` (24). |
| Finished jobs, empty folders | Removed. |

`FUND_ANALYZER_CACHE_ARCHIVE_DIR` sets a default archive folder outside the cache folder. A lock file
stops two cleanups running at once, and each item is checked again just before it is removed.

**Weekly schedule (Windows).** `backend\scripts\cleanup_portfolio_cache.bat` runs the command with the
project's virtual environment, the backend folder as working directory, the archive folder
`D:\mns\portfolio_archive` and the log `D:\mns\logs\portfolio_cache_cleanup.log`. Run it with no
arguments for a dry run. The scheduled task passes `--apply`.

The task `MNS Portfolio Cache Cleanup` runs it every Sunday at 03:00. If the PC is off then, it runs at
the next start. It stops after an hour, and a second copy never starts while one is running. It was
registered with PowerShell (run once):

```powershell
$bat = 'D:\PROJECT\money_navigation_solution\backend\scripts\cleanup_portfolio_cache.bat'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$bat`" --apply" -WorkingDirectory 'D:\PROJECT\money_navigation_solution\backend'
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At '03:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'MNS Portfolio Cache Cleanup' -Action $action -Trigger $trigger -Settings $settings
```

Check it with `Get-ScheduledTask 'MNS Portfolio Cache Cleanup' | Get-ScheduledTaskInfo`, run it now with
`Start-ScheduledTask 'MNS Portfolio Cache Cleanup'`, and remove it with
`Unregister-ScheduledTask 'MNS Portfolio Cache Cleanup'`. It runs only while the user is signed in. To run
it signed out, open Task Scheduler, choose the task, and select "Run whether user is logged on or not".

Each run appends to the log: date and time, mode, what was planned, every protected file and why, the
files deleted and archived, skipped and failed items, space reclaimed, and the duration.

**Filling the cache ahead of time (optional).** Holdings download when first
requested, so this isn't required, but it makes the first request faster:

```powershell
python manage.py ingest_portfolios --pause 1
python manage.py portfolio_coverage --full
```

It skips sources checked recently and waits longer after blocks and failures.
Useful options are `--amc`, `--scheme`, `--force`, `--months` and `--max-seconds`.

**Which month is used.** Official AMC files are always tried first, for at most
`FUND_ANALYZER_PORTFOLIO_LOOKBACK_MONTHS` completed months (default 6: October back
to May on 6 November 2026). The month is chosen in this order:

1. The newest month in which every selected fund has a valid official portfolio.
2. The newest month, within the backup window, in which every fund with official data
   uses it and only funds with none at all use the Charlie Alpha backup.
3. The newest month, within the backup window, in which the backup fills whichever fund
   lacks that specific month, even if it has official data for another month.
4. If no month suits every fund, each pair uses its newest shared month, official on
   both sides first.

The backup is off unless `FUND_ANALYZER_CHARLIE_ALPHA_ENABLED=true`, and may supply only
the newest `FUND_ANALYZER_CHARLIE_ALPHA_LOOKBACK_MONTHS` completed months (default 4:
October back to July). It lists a scheme's report dates first, then requests only the
month-end portfolios it needs. A pair never combines two different months. A backup
portfolio is saved as third-party JSON and never replaces an official month. If neither
source gives a matching month, the screen shows `Portfolio holdings unavailable for a
matching reporting month.`

**Monitoring.** `GET /api/overlap/coverage` (staff only) and `portfolio_coverage`
report verified schemes, the latest month per AMC, failures and funds needing
action. Statuses are `SOURCE_NOT_SUPPORTED`, `DISCLOSURE_NOT_PUBLISHED`,
`DOWNLOAD_BLOCKED`, `DOWNLOAD_FAILED`, `INVALID_WORKBOOK`, `SCHEME_NOT_FOUND`,
`PARSING_FAILED`, `NO_COMMON_MONTH` and `HOLDINGS_AVAILABLE`.

**Renamed or merged schemes.** If AMFI and the AMC use different names for a
scheme, add it to `fund_analyzer/scheme_aliases.json`:
`{"HDFC Mutual Fund": {"AMFI name": "Name in the AMC file"}}`.

**Adding an AMC.** If its AMFI-listed page has no plain Excel links, add an
`AmcAdapter` subclass that finds its files, and an entry in `AMC_SOURCES`. The
overlap formula and frontend do not change. Never bypass a login, CAPTCHA or
access block; use a staff upload for those sources.

An advisor with a staff account can import an official monthly `.xlsx`, `.xls`
or `.csv` disclosure file through the API. For example, after downloading the
HDFC Flexi Cap Fund September 2026 workbook from HDFC's disclosure page:

```powershell
curl.exe -X POST http://localhost:8000/api/overlap/import `
  -H "Authorization: Bearer YOUR_STAFF_ACCESS_TOKEN" `
  -F "scheme_code=118955" `
  -F "portfolio_date=2026-09-30" `
  -F "file=@C:\path\to\hdfc-flexi-cap-september-2026.xlsx"
```

Use the workbook's actual reporting date; the import checks it against the
date printed inside the file. A staff access token is returned by
`POST /api/auth/login/`. The import validates the scheme against AMFI and
saves the parsed equity holdings in the JSON cache, where a later download does
not replace them. The uploaded workbook itself is not kept.

## Deploying to Vercel
Create two Vercel projects from this repo: one with root directory `frontend`, one with `backend`.
Set the env vars from each `.env.example` in Vercel. Use a hosted PostgreSQL (Neon, Supabase, Vercel Postgres) for `DATABASE_URL`.
