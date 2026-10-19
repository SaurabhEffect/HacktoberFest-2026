@echo off
rem Starts Hinglish Reel Captioner. The first run sets up a private Python environment (about a minute).
rem
rem   run.bat           real transcription (a Whisper model downloads the first time you caption something)
rem   run.bat --demo    no model download: plays a sample script so you can try the whole studio
rem
rem Any other option is passed through, e.g.  run.bat --model medium --port 8001
setlocal
cd /d "%~dp0"

if exist ".venv\.installed" goto run

echo First run: setting things up. This takes about a minute and needs an internet connection.
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo This app needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ and try again.
  exit /b 1
)
%PY% -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo ok> ".venv\.installed"

:run
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo The existing .venv does not contain Python 3.10 or newer.
  echo Remove the .venv folder and run this command again to recreate it.
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 echo Note: FFmpeg was not found, and captioning needs it to read your videos. Install it with: winget install Gyan.FFmpeg
".venv\Scripts\python.exe" -m reelcaptioner %*
goto end

:fail
echo.
echo Setup did not finish. Check that Python 3.10 or newer is installed (https://www.python.org/downloads/) and that you are online.
exit /b 1

:end
endlocal
