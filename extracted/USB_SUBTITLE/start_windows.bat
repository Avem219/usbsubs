@echo off
setlocal
REM USB SUBTITLE launcher for Windows 10/11. No administrator rights needed.
set "HERE=%~dp0"
set "VENV=%LOCALAPPDATA%\USB_SUBTITLE\venv"
set "PY="
where py >nul 2>&1 && (py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=py -3")
if not defined PY (where python >nul 2>&1 && (python -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=python"))
if not defined PY (
  echo Python 3.9 or newer was not found.
  echo Install it from https://www.python.org/downloads/windows/  ^(tick "Add python.exe to PATH"^), then run this again.
  pause & exit /b 1
)
if not exist "%VENV%\Scripts\python.exe" (
  echo First run on this computer: creating environment ^(internet needed once^)...
  %PY% -m venv "%VENV%" || (echo Could not create environment. & pause & exit /b 1)
)
if not exist "%VENV%\.deps-ok" (
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
  "%VENV%\Scripts\python.exe" -m pip install -r "%HERE%app\requirements.txt" || (echo Dependency install failed. Internet is needed once. & pause & exit /b 1)
  echo ok> "%VENV%\.deps-ok"
)
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%HERE%app"
"%VENV%\Scripts\python.exe" -m usb_subtitle %*
if errorlevel 1 pause
