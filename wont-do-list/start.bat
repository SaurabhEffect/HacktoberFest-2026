@echo off
rem Start The Won't-Do List and open it in your browser (Windows).
rem Anything you add is passed on, for example: start.bat --port 9000
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m wontdo serve --open %*
  goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python -m wontdo serve --open %*
  goto :done
)

echo The Won't-Do List needs Python 3.9 or newer: https://www.python.org/downloads/
pause

:done
