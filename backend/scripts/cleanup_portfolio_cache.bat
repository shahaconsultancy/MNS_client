@echo off
rem Weekly cleanup of the portfolio holdings cache, run by Windows Task Scheduler.
rem Without arguments it is a dry run. The scheduled task passes --apply.
rem Official files past retention are moved to the archive folder, never deleted.
setlocal
set "BACKEND=%~dp0.."
set "PYTHON=%~dp0..\..\myenv\Scripts\python.exe"
set "ARCHIVE=D:\mns\portfolio_archive"
set "LOGFILE=D:\mns\logs\portfolio_cache_cleanup.log"

cd /d "%BACKEND%" || exit /b 1
if not exist "%PYTHON%" (
    echo %DATE% %TIME% Python not found at %PYTHON%>> "%LOGFILE%"
    exit /b 1
)
"%PYTHON%" manage.py cleanup_portfolio_cache --archive-dir "%ARCHIVE%" --log-file "%LOGFILE%" %*
exit /b %ERRORLEVEL%
