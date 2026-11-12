@echo off
setlocal
title Damage Report
rem Drag a walkthrough VIDEO onto this file  -> builds the whole report.
rem Drag a "... - damage report" FOLDER onto it -> rebuilds the PDF after you edit items.json.

set "VENV=%USERPROFILE%\.damage-report\venv"
set "PY=%VENV%\Scripts\python.exe"
set "TOOL=%~dp0tool\damage_report.py"

if not exist "%PY%" (
  echo First-time setup: installing the tools - this takes a few minutes...
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo Python 3 is needed. Install it from https://www.python.org/downloads/ and try again.
    pause
    exit /b 1
  )
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo Install failed - check your internet connection and try again.
    pause
    exit /b 1
  )
)

if "%~1"=="" (
  echo Drag your walkthrough video onto this file to make the report.
  echo Drag a "... - damage report" folder onto it to rebuild the PDF after editing items.json.
  pause
  exit /b 0
)

if exist "%~1\items.json" (
  "%PY%" -I "%TOOL%" report "%~1"
  if exist "%~1\Damage Report.pdf" start "" "%~1\Damage Report.pdf"
) else (
  "%PY%" -I "%TOOL%" all "%~1"
  if exist "%~dpn1 - damage report\Damage Report.pdf" start "" "%~dpn1 - damage report\Damage Report.pdf"
)
echo.
pause
